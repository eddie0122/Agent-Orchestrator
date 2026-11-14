"""`aaagent dispatch` and `aaagent wait`: queue the next phase of a task and wait for the worker."""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import backends, gitutil, tasks, worker
from .config import REVIEWER, ROOT, Config
from .tasks import FlowError, Step, Task

PROMPTS = ROOT / "prompts"
HEARTBEAT_SECONDS = 300


def _fill(template: str, values: dict) -> str:
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def build_prompt(task: Task, step: Step, cfg: Config, retry: bool = False) -> str:
    state = task.load()
    values = {"task_id": task.id, "plan": task.rel(task.plan), "round": step.round,
              "report": task.rel(task.report(step.round))}
    if step.phase == "fix":
        last_review = task.latest_review()
        values["review"] = task.rel(task.review(last_review))
        history = []
        for n in range(1, step.round):
            for p in (task.report(n), task.review(n)):
                if p.exists():
                    history.append(task.rel(p))
        values["history"] = ", ".join(history) or "(none)"
    if step.phase == "review":
        values["review"] = task.rel(task.review(step.round))
        values["base"] = state.get("base_commit") or "HEAD"
        history = []
        for n in range(1, step.round):
            for p in (task.report(n), task.review(n)):
                if p.exists():
                    history.append(task.rel(p))
        values["history"] = ", ".join(history) or "(none)"
        pre = state.get("preexisting_dirty") or []
        values["preexisting"] = ", ".join(pre) if pre else "(none)"
    if step.phase == "commit":
        values["review"] = task.rel(task.review(task.latest_review()))
        values["report"] = task.rel(task.report(step.round))
        pre = state.get("preexisting_dirty") or []
        values["preexisting"] = ", ".join(pre) if pre else "(none)"
        trailer = cfg.data.get("git", {}).get("commit_trailer", "")
        values["trailer"] = f"\n   - End the body with this trailer line: {trailer}" if trailer else ""
    text = _fill((PROMPTS / f"worker-{step.phase}.md").read_text(), values)
    if retry and step.phase in ("implement", "fix"):
        text += ("\nNOTE: a previous attempt at this round by another worker failed part-way. "
                 "Its partial changes may be in the working tree; check `git status` and "
                 "`git diff` and build on them rather than starting over.\n")
    return text


def _enqueue(project: Path, task: Task, step: Step, cfg: Config, retry_of: str | None = None) -> dict:
    state = task.load()
    backend = cfg.worker_backend(step.worker)
    # Review jobs keep their own session under the reviewer's name; every other phase
    # keeps one per worker.
    session = None if step.phase == "commit" else state.get("sessions", {}).get(step.worker)
    if not backends.session_fits(backend, session):
        session = None
    if step.phase == "commit":
        agent = cfg.commit_agent
    elif step.phase == "review":
        agent = cfg.review_agent
    else:
        agent = cfg.implement_agent
    job_id = worker.new_job_id()
    job = worker.enqueue(project, {
        "id": job_id, "task": task.id, "phase": step.phase, "round": step.round,
        "worker": step.worker, "model": cfg.worker_model(step.worker),
        "thinking": cfg.worker_thinking(step.worker), "agent": agent, "backend": backend,
        "session": session, "prompt": build_prompt(task, step, cfg, retry=retry_of is not None),
        "timeout": cfg.job_timeout_seconds, "retry_of": retry_of,
        "pre_head": gitutil.head(project),
        "log": str(task.logs / f"r{step.round}-{step.phase}-{step.worker}-{job_id}.jsonl"),
    })
    changes = {"status": "running", "current_job": {
        k: job[k] for k in ("id", "phase", "round", "worker", "retry_of", "pre_head", "log")}}
    if step.phase != "review":
        changes["worker"] = step.worker
        if step.phase == "implement" or step.switched:
            changes["worker_since_round"] = step.round
    task.update(**changes)
    task.log_event(f"queued {step.phase} round {step.round}", worker=step.worker, job=job["id"],
                   reason=step.reason or None)
    worker.start_background(project, step.worker)
    return job


def dispatch(project: Path, ref: str, worker_override: str | None = None, force: bool = False) -> int:
    cfg = Config.load(project)
    task = tasks.find(project, ref)
    step = tasks.next_step(task, cfg, worker_override=worker_override, force=force)
    tasks.claim_active(project, task)
    while True:
        note = f" ({step.reason}; switching worker)" if step.reason else ""
        print(f"{task.id}: {step.phase} round {step.round} → {step.worker} "
              f"({cfg.worker_label(step.worker)}){note}", flush=True)
        _enqueue(project, task, step, cfg)
        code = wait(project, task.id)
        step = _auto_step(task, cfg) if code == 0 else None
        if step is None:
            return code


def _auto_step(task: Task, cfg: Config) -> Step | None:
    """With a reviewing worker: the review after a report, and the fix after a rejected review."""
    if not cfg.reviewer_worker:
        return None
    state = task.load()
    if state.get("status") not in ("awaiting_review", "changes_requested"):
        return None
    if state.get("review_failed_round") == task.latest_report():
        return None
    try:
        step = tasks.next_step(task, cfg)
    except FlowError as exc:
        print(f"Task {task.id} stops here: {exc}")
        return None
    return step if step.phase in ("review", "fix") else None


def _result_or_none(project: Path, job_id: str) -> dict | None:
    path = worker.result_path(project, job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None  # being written


def _job_present(project: Path, job: dict) -> bool:
    q = worker.queue_dir(project, job["worker"])
    return (q / f"{job['id']}.json").exists() or (q / f"{job['id']}.running.json").exists()


def wait(project: Path, ref: str) -> int:
    cfg = Config.load(project)
    task = tasks.find(project, ref)
    while True:
        job = task.load().get("current_job")
        if not job:
            print(f"{task.id}: no job running.")
            print_status_line(task, cfg)
            return 0
        started, last_beat = time.time(), time.time()
        result = None
        while result is None:
            result = _result_or_none(project, job["id"])
            if result is not None:
                break
            if not _job_present(project, job):
                time.sleep(0.5)
                result = _result_or_none(project, job["id"])
                if result is None:
                    result = {"status": "error", "error": "job disappeared from the queue",
                              "session": None, "started": started, "finished": time.time()}
                break
            if not worker.worker_pid(project, job["worker"]):
                worker.start_background(project, job["worker"])
            if time.time() - last_beat >= HEARTBEAT_SECONDS:
                print(f"  … still working ({(time.time() - started) / 60:.0f} min)", flush=True)
                last_beat = time.time()
            time.sleep(1)
        retry = _finalize(project, task, cfg, job, result)
        if retry is None:
            return 0 if task.load().get("status") != "failed" else 1


def _finalize(project: Path, task: Task, cfg: Config, job: dict, result: dict) -> dict | None:
    """Record the result. Returns the retry job if one was queued, else None."""
    state = task.load()
    phase, rnd, who = job["phase"], job["round"], job["worker"]
    if result.get("session") and phase != "commit":
        state.setdefault("sessions", {})[who] = result["session"]
    tok = state.setdefault("tokens", {}).setdefault(who, {"input": 0, "output": 0})
    for k in ("input", "output"):
        tok[k] += int((result.get("tokens") or {}).get(k, 0))
    task.save(state)

    res_file = worker.result_path(project, job["id"])
    if res_file.is_file():
        res_file.replace(task.logs / f"{job['id']}.result.json")

    error = result.get("error") if result.get("status") != "ok" else None
    if not error and phase in ("implement", "fix") and not task.report(rnd).is_file():
        error = f"worker finished without writing {task.rel(task.report(rnd))}"
    if not error and phase == "review" and task.verdict(rnd) not in ("approved", "changes_requested"):
        error = (f"reviewer finished without writing {task.rel(task.review(rnd))} "
                 "with a valid verdict")
    new_head = gitutil.head(project)
    if not error and phase == "commit" and new_head == job.get("pre_head"):
        error = "worker finished without creating a commit"
    mins = (result.get("finished", time.time()) - result.get("started", time.time())) / 60

    if error:
        if phase == "review" and task.review(rnd).is_file():
            task.review(rnd).replace(task.path / f"review-{rnd}.invalid.md")
        task.log_event(f"{phase} round {rnd} failed", worker=who, error=error)
        print(f"✗ {phase} round {rnd} by {who} failed after {mins:.1f} min: {error}")
        print(f"  log: {task.rel(Path(job['log']))}")
        if phase == "review":
            # A review never falls back to a worker: retry once on the reviewer itself,
            # then hand the round back to the planner.
            if not job.get("retry_of"):
                print(f"→ retrying on the reviewer ({cfg.worker_label(who)})", flush=True)
                step = Step(phase, rnd, who, reason=f"{who} failed")
                task.update(current_job=None, status="retrying")
                return _enqueue(project, task, step, cfg, retry_of=job["id"])
            task.update(current_job=None, status="awaiting_review", review_failed_round=rnd)
            task.log_event(f"review round {rnd} handed to the planner", worker=who)
            print(f"The reviewer could not review round {rnd}. Review it yourself: read the report, "
                  f"inspect `aaagent diff {task.id}`, write {task.rel(task.review(rnd))} with "
                  f"`verdict: approved` or `verdict: changes_requested`, and run "
                  f"`aaagent dispatch {task.id}`.")
            return None
        alt = cfg.alternate(who)
        if alt and not job.get("retry_of"):
            print(f"→ retrying with {alt} ({cfg.worker_label(alt)})", flush=True)
            step = Step(phase, rnd, alt, switched=True, reason=f"{who} failed")
            task.update(current_job=None, status="retrying")
            return _enqueue(project, task, step, cfg, retry_of=job["id"])
        task.update(current_job=None, status="failed")
        print(f"Task {task.id} is stopped. Read the log above, then fix the plan and "
              f"`aaagent dispatch {task.id}` again, or tell the user.")
        return None

    if phase == "commit":
        task.update(current_job=None, status="committed", commit=new_head)
        task.log_event("committed", worker=who, commit=new_head)
        tasks.release_active(project, task)
        print(f"✓ committed by {who} in {mins:.1f} min:")
        print("  " + gitutil.git(project, "log", "-1", "--format=%h %s", check=False).strip())
        left = [p for p in gitutil.dirty_paths(project) if p not in (state.get("preexisting_dirty") or [])]
        if left:
            print(f"  ! still uncommitted after the commit: {', '.join(left)}")
        print(f"Task {task.id} is complete. Tell the user what was done.")
        return None

    if phase == "review":
        verdict = task.verdict(rnd)
        task.update(current_job=None, status=verdict, review_failed_round=None)
        task.log_event(f"review round {rnd}: {verdict}", worker=who)
        print(f"✓ review round {rnd} by {who} ({cfg.worker_label(who)}) in {mins:.1f} min: "
              f"{verdict.replace('_', ' ')}")
        print(f"  review: {task.rel(task.review(rnd))}")
        if verdict == "approved":
            print(f"Next: the reviewer approved round {rnd}. Read the review and the report, look "
                  f"over `aaagent diff {task.id}`, then run `aaagent dispatch {task.id}` to commit. "
                  f"To overrule the approval, overwrite the review file with "
                  f"`verdict: changes_requested` and your required changes, then dispatch.")
        return None

    report = task.report(rnd)
    report_status = tasks.frontmatter(report).get("status", "?")
    task.update(current_job=None, status="awaiting_review")
    task.log_event(f"{phase} round {rnd} done", worker=who, report_status=report_status)
    print(f"✓ {phase} round {rnd} done by {who} in {mins:.1f} min. Report status: {report_status}")
    print(f"  report: {task.rel(report)}")
    print("  changes since task start:")
    print(gitutil.diff_stat(project, state.get("base_commit")))
    if cfg.reviewer_worker:
        print(f"Next: the reviewer ({cfg.worker_label(REVIEWER)}) reviews round {rnd} "
              f"(`aaagent dispatch {task.id}` continues if this command was interrupted).")
    else:
        print(f"Next: read the report, inspect `aaagent diff {task.id}`, re-run the validation, then write "
              f"{task.rel(task.review(rnd))} with `verdict: approved` or `verdict: changes_requested`, "
              f"and run `aaagent dispatch {task.id}`.")
    return None


def status_line(task: Task, cfg: Config) -> str:
    state = task.load()
    status = state.get("status", "?")
    try:
        step = tasks.next_step(task, cfg)
        nxt = f"next: {step.phase} r{step.round} → {step.worker}"
    except FlowError as exc:
        nxt = f"next: {exc}"
    return f"{task.id:<28} {status:<16} worker={state.get('worker') or '-':<9} {nxt}"


def print_status_line(task: Task, cfg: Config) -> None:
    print(status_line(task, cfg))


def abandon(project: Path, ref: str) -> str:
    """Drop a task; its uncommitted changes stay. Returns what to tell the user."""
    task = tasks.find(project, ref)
    if task.load().get("status") == "running":
        raise FlowError(f"task {task.id} has a job running; wait for it (`aaagent wait {task.id}`) first.")
    task.update(status="abandoned")
    task.log_event("abandoned")
    tasks.release_active(project, task)
    left = gitutil.dirty_paths(project)
    text = f"task {task.id} abandoned."
    if left:
        text += f"\nuncommitted changes left in the working tree: {', '.join(left)}"
    return text
