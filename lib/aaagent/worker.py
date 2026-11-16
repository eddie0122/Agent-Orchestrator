"""Worker daemon and the file queue it reads.

    .aaagent/queue/<worker>/<job>.json          pending job
    .aaagent/queue/<worker>/<job>.running.json  claimed by the daemon
    .aaagent/results/<job>.json                 finished job
    .aaagent/workers/<worker>.pid               daemon pid
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import backends, gitutil
from .backends import BOLD, DIM, GREEN, RED, RESET, color_enabled, paint
from .config import REVIEWER, Config, infer_backend

POLL_SECONDS = 1.0
# One colour per worker, picked by its position in the configured worker list.
COLOR_PALETTE = ["\033[36m", "\033[35m", "\033[33m", "\033[34m", "\033[32m", "\033[91m"]
# The reviewer is not a slot in that list: it keeps a fixed bright yellow.
REVIEWER_COLOR = "\033[93m"


def worker_color(cfg: Config, name: str) -> str:
    """The ANSI colour of a worker's `[name]` tag: its index in the worker list."""
    if name == REVIEWER:
        return REVIEWER_COLOR
    names = cfg.worker_names()
    if name in names:
        return COLOR_PALETTE[names.index(name) % len(COLOR_PALETTE)]
    return COLOR_PALETTE[0]


# -- paths ------------------------------------------------------------------

def queue_dir(project: Path, worker: str) -> Path:
    return project / ".aaagent" / "queue" / worker


def results_dir(project: Path) -> Path:
    return project / ".aaagent" / "results"


def workers_dir(project: Path) -> Path:
    return project / ".aaagent" / "workers"


def pid_file(project: Path, worker: str) -> Path:
    return workers_dir(project) / f"{worker}.pid"


def log_file(project: Path, worker: str) -> Path:
    return workers_dir(project) / f"{worker}.log"


def result_path(project: Path, job_id: str) -> Path:
    return results_dir(project) / f"{job_id}.json"


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


# -- liveness ---------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def worker_pid(project: Path, worker: str) -> int | None:
    f = pid_file(project, worker)
    try:
        pid = int(f.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def start_background(project: Path, worker: str) -> int:
    """Start `aaagent worker <name>` detached, logging to .aaagent/workers/<name>.log."""
    existing = worker_pid(project, worker)
    if existing:
        return existing
    workers_dir(project).mkdir(parents=True, exist_ok=True)
    aaagent_bin = Path(__file__).resolve().parents[2] / "bin" / "aaagent"
    with log_file(project, worker).open("a") as log:
        proc = subprocess.Popen([sys.executable, str(aaagent_bin), "worker", worker, "--quiet-wait"],
                                cwd=project, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if worker_pid(project, worker):
            break
        time.sleep(0.1)
    return proc.pid


def stop(project: Path, worker: str) -> bool:
    pid = worker_pid(project, worker)
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


# -- queue ------------------------------------------------------------------

def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def enqueue(project: Path, job: dict) -> dict:
    job = dict(job)
    job.setdefault("id", new_job_id())
    job.setdefault("created", time.time())
    write_json(queue_dir(project, job["worker"]) / f"{job['id']}.json", job)
    return job


def running_job(project: Path, worker: str) -> dict | None:
    for p in queue_dir(project, worker).glob("*.running.json"):
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return None


def pending_jobs(project: Path, worker: str) -> list[Path]:
    q = queue_dir(project, worker)
    return sorted(p for p in q.glob("*.json") if not p.name.endswith(".running.json"))


def claim(project: Path, worker: str) -> tuple[dict, Path] | None:
    for p in pending_jobs(project, worker):
        claimed = p.with_name(p.name[:-5] + ".running.json")
        try:
            p.rename(claimed)
        except FileNotFoundError:
            continue
        return json.loads(claimed.read_text()), claimed
    return None


# -- running one job -------------------------------------------------------

def describe(event: dict, project: Path) -> str | None:
    """One readable line for a JSON event from `opencode run --format json`."""
    return backends.describe("opencode", event, project)


def run_job(project: Path, job: dict, cfg: Config, out=sys.stdout) -> dict:
    backend = job.get("backend") or infer_backend(job["model"])
    cmd = backends.command(job, cfg)

    log_path = Path(job["log"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    session, errors, tokens = job.get("session"), [], {"input": 0, "output": 0}
    timed_out = threading.Event()

    with log_path.open("w") as log, open(log_path.with_suffix(".stderr"), "w") as err_log:
        proc = subprocess.Popen(cmd, cwd=project, stdout=subprocess.PIPE, stderr=err_log,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True,
                                env=gitutil.git_env(project))

        def kill():
            timed_out.set()
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                time.sleep(5)
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        timer = threading.Timer(float(job.get("timeout", cfg.job_timeout_seconds)), kill)
        timer.daemon = True
        timer.start()
        try:
            for raw in proc.stdout:
                log.write(raw)
                log.flush()
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                seen = backends.read_event(backend, event)
                if seen["session"]:
                    session = seen["session"]
                if seen["error"]:
                    errors.append(seen["error"])
                tokens["input"] += seen["input"]
                tokens["output"] += seen["output"]
                line = backends.describe(backend, event, project)
                if line:
                    print(line, file=out, flush=True)
            code = proc.wait()
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            raise
        finally:
            timer.cancel()

    if timed_out.is_set():
        status, error = "timeout", f"killed after {job.get('timeout', cfg.job_timeout_seconds) / 60:.0f} minutes"
    elif code != 0 or errors:
        status, error = "error", "; ".join(errors) or f"{backend} exited with code {code}"
    else:
        status, error = "ok", None
    return {
        "id": job["id"], "task": job["task"], "phase": job["phase"], "round": job["round"],
        "worker": job["worker"], "model": job["model"], "status": status, "error": error,
        "exit_code": code, "session": session, "tokens": tokens, "backend": backend,
        "started": started, "finished": time.time(), "log": job["log"],
    }


# -- daemon -----------------------------------------------------------------

def serve(project: Path, worker: str, once: bool = False, quiet_wait: bool = False) -> int:
    cfg = Config.load(project)
    model = cfg.worker_label(worker)
    other = worker_pid(project, worker)
    if other and other != os.getpid():
        print(f"[{worker}] already running (pid {other}).")
        return 0
    workers_dir(project).mkdir(parents=True, exist_ok=True)
    queue_dir(project, worker).mkdir(parents=True, exist_ok=True)
    pid_file(project, worker).write_text(f"{os.getpid()}\n")

    def on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)

    # A job left as .running by a crashed daemon is put back in the queue.
    for stale in queue_dir(project, worker).glob("*.running.json"):
        stale.rename(stale.with_name(stale.name.replace(".running.json", ".json")))

    tag = paint(BOLD + worker_color(cfg, worker), f"[{worker}]")
    print(f"{tag} {model}", flush=True)
    waiting_shown = False
    job = claimed_path = None
    try:
        while True:
            claimed = claim(project, worker)
            if not claimed:
                if once:
                    return 0
                if not waiting_shown and not quiet_wait:
                    print(f"{tag} waiting for a plan…", flush=True)
                waiting_shown = True
                time.sleep(POLL_SECONDS)
                continue
            waiting_shown = False
            job, claimed_path = claimed
            print(f"{tag} {paint(BOLD, job['phase'])} {job['task']} round {job['round']}"
                  f"  ({time.strftime('%H:%M:%S')})", flush=True)
            try:
                result = run_job(project, job, cfg)
            except Exception as exc:  # noqa: BLE001 - report any launch failure to the waiter
                result = {"id": job["id"], "task": job["task"], "phase": job["phase"],
                          "round": job["round"], "worker": worker, "model": job["model"],
                          "status": "error", "error": f"could not run {job.get('backend') or 'opencode'}: {exc}",
                          "session": job.get("session"), "started": time.time(),
                          "finished": time.time(), "log": job["log"]}
            write_json(result_path(project, job["id"]), result)
            claimed_path.unlink(missing_ok=True)
            job = claimed_path = None
            mins = (result["finished"] - result["started"]) / 60
            mark = paint(GREEN, "done") if result["status"] == "ok" else paint(RED, result["status"])
            print(f"{tag} {mark} in {mins:.1f} min" + (f": {result['error']}" if result.get("error") else ""),
                  flush=True)
    except KeyboardInterrupt:
        if job is not None:
            write_json(result_path(project, job["id"]), {
                "id": job["id"], "task": job["task"], "phase": job["phase"], "round": job["round"],
                "worker": worker, "model": job["model"], "status": "error",
                "error": "worker was stopped during the job", "session": job.get("session"),
                "started": time.time(), "finished": time.time(), "log": job["log"]})
            claimed_path.unlink(missing_ok=True)
        print(f"\n{tag} stopped.")
        return 0
    finally:
        f = pid_file(project, worker)
        if f.is_file() and f.read_text().strip() == str(os.getpid()):
            f.unlink()
