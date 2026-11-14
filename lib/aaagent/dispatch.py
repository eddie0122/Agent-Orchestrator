"""`aaagent dispatch` and `aaagent wait`: queue the next phase of a task and wait for the worker."""

from __future__ import annotations

import json
import shlex
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


def scope_note(task: Task, phase: str) -> str:
    """What a scoped task's worker must know about the other tasks sharing the tree."""
    scope = task.scope()
    if not scope:
        return ""
    listed = ", ".join(scope)
    if phase in ("implement", "fix"):
        return (f"This task may only change these files: {listed}. Other tasks may be running in "
                "the same working tree at the same time: never touch, revert or format files "
                "outside that list, and if a test fails only because of files outside it, say so "
                "in the report instead of fixing it.")
    if phase == "review":
        return ("Changes outside the declared files are scope creep, unless they belong to another "
                "task (other tasks may be running at the same time; ignore files outside this "
                "list that the report does not claim).")
    if phase == "commit":
        return f"Stage only changed files inside this task's declared files: {listed}."
    return ""


def _review_pathspec(task: Task) -> str:
    """The scope as shell-quoted git pathspecs, or `.` for an undeclared task."""
    pathspecs = task.pathspecs()
    if not pathspecs:
        return "."
    return " ".join(shlex.quote(spec) for spec in pathspecs)


def build_prompt(task: Task, step: Step, cfg: Config, retry: bool = False) -> str:
    state = task.load()
    values = {"task_id": task.id, "plan": task.rel(task.plan), "round": step.round,
              "report": task.rel(task.report(step.round)), "scope_note": scope_note(task, step.phase)}
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
        values["pathspec"] = _review_pathspec(task)
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


def _other_active_tasks(project: Path, task: Task) -> list[Task]:
    others = []
    for task_id in tasks.active_tasks(project):
        if task_id == task.id:
            continue
        try:
            others.append(tasks.find(project, task_id))
        except FlowError:
            continue
    return others


def refresh_base(project: Path, task: Task) -> None:
    """Start the diff window at the first dispatch, not at planning time.

    A task planned while another task's changes were still uncommitted used to list those
    files as its own preexisting changes. Files that belong to another active task are not
    preexisting for this one either.
    """
    state = task.load()
    if task.latest_report() or state.get("started"):
        return
    other_scopes = [other.scope() for other in _other_active_tasks(project, task)]
    preexisting = [path for path in gitutil.dirty_paths(project)
                   if not any(scope is not None and tasks.in_scope(path, scope)
                              for scope in other_scopes)]
    base = gitutil.head(project)
    task.update(base_commit=base, preexisting_dirty=preexisting, started=tasks.now())
    task.log_event("base refreshed at the first dispatch", base_commit=base,
                   preexisting_dirty=preexisting)


def _busy_with(project: Path, worker_name: str, task_id: str) -> str | None:
    """The other task a worker is running or has queued, if any."""
    job = worker.running_job(project, worker_name)
    if job and job.get("task") != task_id:
        return job.get("task")
    for path in worker.pending_jobs(project, worker_name):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("task") != task_id:
            return data.get("task")
    return None


def pick_free_worker(project: Path, cfg: Config, preferred: str, task_id: str) -> tuple[str, str]:
    """A worker for a new implement round and the note that says why it was picked."""
    busy = _busy_with(project, preferred, task_id)
    if not busy:
        return preferred, ""
    for name in cfg.worker_names():
        if name == preferred:
            continue
        if not worker.running_job(project, name) and not worker.pending_jobs(project, name):
            return name, f"{preferred} is busy with {busy}"
    return preferred, f"all workers are busy; {preferred} takes this when it is free"


def _committing_task(project: Path, task: Task) -> str | None:
    """The id of another active task whose current job is a commit, if any."""
    for other in _other_active_tasks(project, task):
        job = other.load().get("current_job") or {}
        if job.get("phase") == "commit":
            return other.id
    return None


def enqueue_commit(project: Path, task: Task, step: Step, cfg: Config,
                   sleep=time.sleep, timeout: float | None = None,
                   retry_of: str | None = None) -> dict | None:
    """Enqueue a commit once no other active task is committing; None when it waited too long.

    The check and the enqueue happen under the active-task lock, so two dispatches cannot
    both decide that nobody is committing. Commits share the git index, so they must not
    interleave.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    printed = False
    while True:
        with tasks.active_lock(project):
            other = _committing_task(project, task)
            if other is None:
                return _enqueue(project, task, step, cfg, retry_of=retry_of)
        if not printed:
            print(f"waiting for {other}'s commit to finish…", flush=True)
            printed = True
        if deadline is not None and time.monotonic() >= deadline:
            return None
        sleep(1)


def changed_outside_scope(project: Path, task: Task, state: dict) -> list[str]:
    """Changed files that no active task declares and that were not dirty before this task."""
    if task.scope() is None:
        return []
    scopes = [other.scope() for other in [task, *_other_active_tasks(project, task)]]
    preexisting = state.get("preexisting_dirty") or []
    return [path for path in gitutil.dirty_paths(project)
            if path not in preexisting
            and not any(scope is not None and tasks.in_scope(path, scope) for scope in scopes)]


def dispatch(project: Path, ref: str, worker_override: str | None = None, force: bool = False) -> int:
    cfg = Config.load(project)
    task = tasks.find(project, ref)
    step = tasks.next_step(task, cfg, worker_override=worker_override, force=force)
    tasks.claim_active(project, task)
    refresh_base(project, task)
    plan_worker = tasks.frontmatter(task.plan).get("worker") or None
    if step.phase == "implement" and not worker_override and not plan_worker:
        chosen, why = pick_free_worker(project, cfg, step.worker, task.id)
        if chosen != step.worker:
            step.note = why
            step.worker = chosen
        elif why:
            print(f"note: {why}", flush=True)
    while True:
        note = f" ({step.reason}; switching worker)" if step.reason else ""
        if step.note:
            note = f" ({step.note})"
        print(f"{task.id}: {step.phase} round {step.round} → {step.worker} "
              f"({cfg.worker_label(step.worker)}){note}", flush=True)
        if step.phase == "commit":
            job = enqueue_commit(project, task, step, cfg)
            if job is None:
                print(f"{task.id}: could not start the commit", flush=True)
                return 1
        else:
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
            if phase == "commit":  # commits still never run at the same time
                return enqueue_commit(project, task, step, cfg, retry_of=job["id"])
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
        dirty = gitutil.dirty_paths(project, task.pathspecs())
        left = [p for p in dirty if p not in (state.get("preexisting_dirty") or [])]
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
    outside = changed_outside_scope(project, task, state)
    if outside:
        message = f"changed outside its declared files: {', '.join(outside)}"
        task.log_event(f"{phase} round {rnd} {message}")
        print(f"warning: {message}")
    print("  changes since task start:")
    print(gitutil.diff_stat(project, state.get("base_commit"), task.pathspecs()))
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
