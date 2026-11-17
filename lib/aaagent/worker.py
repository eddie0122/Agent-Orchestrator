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
# The most review jobs one daemon runs at once ([reviewer] parallel).
MAX_PARALLEL = 8
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


def slots(cfg: Config, worker: str) -> int:
    """How many jobs this queue runs at once: [reviewer] parallel for the reviewer (1..MAX_PARALLEL,
    default 1; invalid values count as 1), always 1 for workers."""
    if worker != REVIEWER:
        return 1
    section = cfg.data.get("reviewer", {})
    raw = section.get("parallel", 1) if isinstance(section, dict) else 1
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return min(max(n, 1), MAX_PARALLEL)


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

# Backends currently running, so a stopping daemon can kill all of them: job id -> process.
_procs: dict[str, subprocess.Popen] = {}
_procs_lock = threading.Lock()
# Serializes the event lines of concurrent jobs onto stdout.
_out_lock = threading.Lock()


def _register_proc(job_id: str, proc: subprocess.Popen) -> None:
    with _procs_lock:
        _procs[job_id] = proc


def _unregister_proc(job_id: str) -> None:
    with _procs_lock:
        _procs.pop(job_id, None)


def _kill_all() -> None:
    """SIGTERM the process group of every backend still running."""
    with _procs_lock:
        procs = list(_procs.values())
    for proc in procs:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


class _Prefixed:
    """A file-like view of `out` that prefixes every line with `prefix`.

    Used when several jobs run at once, so their event lines stay readable. Only `write` and
    `flush` are needed: `print(..., file=...)` uses nothing else.
    """

    def __init__(self, out, prefix: str):
        self.out = out
        self.prefix = prefix
        self._at_start = True

    def write(self, text: str) -> int:
        with _out_lock:
            for line in text.splitlines(keepends=True):
                if self._at_start and line.strip("\n"):
                    self.out.write(self.prefix)
                self.out.write(line)
                self._at_start = line.endswith("\n")
        return len(text)

    def flush(self) -> None:
        with _out_lock:
            self.out.flush()


def describe(event: dict, project: Path) -> str | None:
    """One readable line for a JSON event from `opencode run --format json`."""
    return backends.describe("opencode", event, project)


def run_job(project: Path, job: dict, cfg: Config, out=sys.stdout) -> dict:
    backend = job.get("backend") or infer_backend(job["model"])
    cmd = backends.command(job, cfg)
    # A task with its own worktree runs the backend there; its job dict names the directory.
    workdir = Path(job["cwd"]) if job.get("cwd") else project
    env = gitutil.git_env(project)
    if job.get("cwd"):
        # The Claude guard hook decides what a worker may edit from AAAGENT_PROJECT.
        env["AAAGENT_PROJECT"] = str(workdir)

    log_path = Path(job["log"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    session, errors, tokens = job.get("session"), [], {"input": 0, "output": 0}
    timed_out = threading.Event()

    with log_path.open("w") as log, open(log_path.with_suffix(".stderr"), "w") as err_log:
        proc = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=err_log,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True,
                                env=env)
        _register_proc(job["id"], proc)

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
                line = backends.describe(backend, event, workdir)
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
            _unregister_proc(job["id"])

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

def _run_claimed(project: Path, cfg: Config, worker: str, job: dict, claimed_path: Path,
                 tag: str, out=None, stopping: threading.Event | None = None,
                 result_lock: threading.Lock | None = None) -> dict | None:
    """Run one claimed job: its event lines, its result file, its done line.

    Returns the result, or None when the daemon stopped the job first: with `stopping` set the
    stop handler owns the result file and the claimed file, so the helper writes neither.
    """
    out = sys.stdout if out is None else out
    if stopping is not None and stopping.is_set():
        return None  # the daemon stopped before this job's backend even started
    print(f"{tag} {paint(BOLD, job['phase'])} {job['task']} round {job['round']}"
          f"  ({time.strftime('%H:%M:%S')})", file=out, flush=True)
    try:
        result = run_job(project, job, cfg, out=out)
    except Exception as exc:  # noqa: BLE001 - report any launch failure to the waiter
        result = {"id": job["id"], "task": job["task"], "phase": job["phase"],
                  "round": job["round"], "worker": worker, "model": job["model"],
                  "status": "error", "error": f"could not run {job.get('backend') or 'opencode'}: {exc}",
                  "session": job.get("session"), "started": time.time(),
                  "finished": time.time(), "log": job["log"]}
    lock = result_lock if result_lock is not None else threading.Lock()
    with lock:
        if stopping is not None and stopping.is_set():
            return None
        write_json(result_path(project, job["id"]), result)
        claimed_path.unlink(missing_ok=True)
    mins = (result["finished"] - result["started"]) / 60
    mark = paint(GREEN, "done") if result["status"] == "ok" else paint(RED, result["status"])
    print(f"{tag} {mark} in {mins:.1f} min"
          + (f": {result['error']}" if result.get("error") else ""), file=out, flush=True)
    return result


def _stopped_result(job: dict, worker: str) -> dict:
    """The result of a job the daemon killed while it was running."""
    return {"id": job["id"], "task": job["task"], "phase": job["phase"], "round": job["round"],
            "worker": worker, "model": job["model"], "status": "error",
            "error": "worker was stopped during the job", "session": job.get("session"),
            "started": time.time(), "finished": time.time(), "log": job["log"]}


def _serve_pool(project: Path, worker: str, cfg: Config, tag: str, n: int,
                once: bool, quiet_wait: bool) -> int:
    """The daemon loop for a queue with several slots: up to `n` jobs run at the same time."""
    running: dict[str, tuple[dict, Path, threading.Thread]] = {}
    stopping = threading.Event()
    result_lock = threading.Lock()
    waiting_shown = False

    def start(job: dict, claimed_path: Path) -> None:
        prefix = f"  [{str(job.get('task') or '').split('-', 1)[0]}] "
        thread = threading.Thread(
            target=_run_claimed,
            args=(project, cfg, worker, job, claimed_path, tag),
            kwargs={"out": _Prefixed(sys.stdout, prefix), "stopping": stopping,
                    "result_lock": result_lock},
            name=f"{worker}-{job['id']}", daemon=True)
        running[job["id"]] = (job, claimed_path, thread)
        thread.start()

    try:
        while True:
            for job_id, (_, _, thread) in list(running.items()):
                if not thread.is_alive():
                    thread.join()  # already finished: collect it
                    del running[job_id]
            while len(running) < n:
                claimed = claim(project, worker)
                if not claimed:
                    break
                start(*claimed)
            if not running:
                if once:
                    return 0
                if not waiting_shown and not quiet_wait:
                    print(f"{tag} waiting for a plan…", flush=True)
                waiting_shown = True
            else:
                waiting_shown = False
            # A short sleep: a finished job frees its slot promptly.
            time.sleep(0.2)
    except KeyboardInterrupt:
        stopping.set()
        _kill_all()
        with result_lock:
            for job, claimed_path, _ in list(running.values()):
                if result_path(project, job["id"]).exists():
                    continue  # the thread finished just before the stop: keep its result
                write_json(result_path(project, job["id"]), _stopped_result(job, worker))
                claimed_path.unlink(missing_ok=True)
        deadline = time.time() + 5
        for _, _, thread in running.values():
            try:
                thread.join(timeout=max(0.0, deadline - time.time()))
            except RuntimeError:  # never started: the interrupt came between claim and start
                pass
        _kill_all()  # a thread that launched its backend just after the first kill
        print(f"\n{tag} stopped.", flush=True)
        return 0


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
    n = slots(cfg, worker)
    print(f"{tag} {model}" + (f" ({n} at a time)" if n > 1 else ""), flush=True)
    waiting_shown = False
    job = claimed_path = None
    try:
        if n > 1:
            return _serve_pool(project, worker, cfg, tag, n, once, quiet_wait)
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
            _run_claimed(project, cfg, worker, job, claimed_path, tag)
            job = claimed_path = None
    except KeyboardInterrupt:
        if job is not None:
            write_json(result_path(project, job["id"]), _stopped_result(job, worker))
            claimed_path.unlink(missing_ok=True)
        print(f"\n{tag} stopped.")
        return 0
    finally:
        f = pid_file(project, worker)
        if f.is_file() and f.read_text().strip() == str(os.getpid()):
            f.unlink()
