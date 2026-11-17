"""`aaagent dispatch` and `aaagent wait`: queue the next phase of a task and wait for the worker."""

from __future__ import annotations

import json
import shlex
import time
from contextlib import nullcontext
from pathlib import Path

from . import backends, branching, gitutil, tasks, worker, worktrees
from .config import REVIEWER, ROOT, Config
from .tasks import FlowError, Step, Task

PROMPTS = ROOT / "prompts"
HEARTBEAT_SECONDS = 300
LESSONS_LIMIT = 6000


def _fill(template: str, values: dict) -> str:
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def lessons_note(project: Path) -> str:
    """The project's lessons (.aaagent/lessons.md) for a worker prompt, or "" when there are none."""
    try:
        text = (tasks.agent_dir(project) / "lessons.md").read_text(errors="replace")
    except OSError:
        return ""
    if not text.strip():
        return ""
    if len(text) > LESSONS_LIMIT:
        text = text[:LESSONS_LIMIT] + "\n(truncated; read the full file: .aaagent/lessons.md)"
    return ("\nPROJECT LESSONS (from earlier tasks in this project; follow them, and as a "
            "reviewer, check them):\n" + text + "\n")


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
    if step.phase in ("implement", "fix", "review"):
        text += lessons_note(task.project)
    return text


def _enqueue(project: Path, task: Task, step: Step, cfg: Config, retry_of: str | None = None) -> dict:
    state = task.load()
    workdir = worktrees.workdir(project, task)
    own_tree = str(workdir) != str(project)
    if own_tree:
        worktrees.sync_into(project, task, workdir)
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
    data = {
        "id": job_id, "task": task.id, "phase": step.phase, "round": step.round,
        "worker": step.worker, "model": cfg.worker_model(step.worker),
        "thinking": cfg.worker_thinking(step.worker), "agent": agent, "backend": backend,
        "session": session, "prompt": build_prompt(task, step, cfg, retry=retry_of is not None),
        "timeout": cfg.job_timeout_seconds, "retry_of": retry_of,
        "pre_head": gitutil.head(workdir),
        "log": str(task.logs / f"r{step.round}-{step.phase}-{step.worker}-{job_id}.jsonl"),
    }
    if own_tree:
        data["cwd"] = str(workdir)
    job = worker.enqueue(project, data)
    current = {k: job[k] for k in ("id", "phase", "round", "worker", "retry_of", "pre_head", "log")}
    if own_tree:
        current["cwd"] = job["cwd"]
    changes = {"status": "running", "current_job": current}
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


def _start_worktree(project: Path, cfg: Config, task: Task) -> None:
    """Create the task's own worktree and record it as the start of its diff window."""
    base = gitutil.head(project)
    path = worktrees.create(project, cfg, task, base)
    branch = worktrees.branch_for(task)
    task.update(base_commit=base, preexisting_dirty=[], started=tasks.now(),
                worktree=str(path), task_branch=branch)
    task.log_event("worktree created", path=str(path), branch=branch)
    print(f"{task.id}: own worktree {task.rel(path)} on branch {branch}", flush=True)


def refresh_base(project: Path, task: Task) -> None:
    """Start the diff window at the first dispatch, not at planning time.

    A task planned while another task's changes were still uncommitted used to list those
    files as its own preexisting changes. Files that belong to another active task are not
    preexisting for this one either.

    With `[parallel] isolation = "worktree"` the task gets its own worktree instead, and its
    tree is clean at the start by definition. Otherwise, in No Git mode, nothing else records
    the changes made outside aaagent, so they get a checkpoint of their own before the
    task's window opens.
    """
    state = task.load()
    if task.latest_report() or state.get("started"):
        return
    cfg = Config.load(project)
    if worktrees.enabled(project, cfg):
        _start_worktree(project, cfg, task)
        return
    reason = worktrees.why_disabled(project, cfg)
    if reason:
        print(f"note: [parallel] isolation = worktree is ignored ({reason}); the task works in "
              f"the shared tree", flush=True)
    other_scopes = [other.scope() for other in _other_active_tasks(project, task)]
    nogit = gitutil.no_git(project)
    with tasks.active_lock(project) if nogit else nullcontext():
        if nogit:
            outside = [path for path in gitutil.dirty_paths(project)
                       if not any(scope is not None and tasks.in_scope(path, scope)
                                  for scope in other_scopes)]
            if outside:
                gitutil.checkpoint(project, "aaagent: changes made outside aaagent",
                                   pathspecs=outside)
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


def _checkpoint(project: Path, task: Task, cfg: Config) -> int:
    """No Git mode: record the approved task as a checkpoint in the shadow repository.

    There is no commit job: aaagent commits on its own, so nothing can run git against the
    project's own repository. Returns the exit code for `aaagent dispatch`; on failure the
    task stays claimed (like a failed commit job) and the next dispatch tries again.
    """
    with tasks.active_lock(project):
        state = task.load()
        exclude = state.get("preexisting_dirty") or []
        try:
            commit = gitutil.checkpoint(project, f"{task.id}: approved", task.pathspecs(),
                                        exclude=exclude)
        except RuntimeError as exc:
            task.update(current_job=None, status="failed")
            task.log_event("checkpoint failed", error=str(exc))
            print(f"✗ checkpoint failed: {exc}")
            print(f"Task {task.id} is stopped. Fix the problem, then "
                  f"`aaagent dispatch {task.id}` again, or tell the user.")
            return 1
        left = gitutil.still_dirty(project, task.pathspecs(), exclude)
        task.update(current_job=None, status="committed", commit=commit)
        task.log_event("checkpoint", commit=commit)
        tasks.release_active(project, task)
    print(f"✓ checkpoint {commit[:7]} recorded for {task.id} "
          f"(no git: aaagent's own history in .aaagent/shadow.git)")
    if left:
        print(f"  ! still uncommitted after the checkpoint: {', '.join(left)}")
    print(f"Task {task.id} is complete. Tell the user what was done.")
    for line in branching.on_idle(project, cfg):
        print(line, flush=True)
    return 0


def changed_outside_scope(project: Path, task: Task, state: dict) -> list[str]:
    """Changed files that no active task declares and that were not dirty before this task.

    A task with its own worktree is alone there: every dirty file outside its declared files
    belongs to it, and no other task's scope or earlier change can explain it away.
    """
    if task.scope() is None:
        return []
    workdir = worktrees.workdir(project, task)
    if str(workdir) != str(project):
        return [path for path in gitutil.dirty_paths(workdir)
                if not tasks.in_scope(path, task.scope())]
    scopes = [other.scope() for other in [task, *_other_active_tasks(project, task)]]
    preexisting = state.get("preexisting_dirty") or []
    return [path for path in gitutil.dirty_paths(project)
            if path not in preexisting
            and not any(scope is not None and tasks.in_scope(path, scope) for scope in scopes)]


def dispatch(project: Path, ref: str, worker_override: str | None = None, force: bool = False) -> int:
    cfg = Config.load(project)
    task = tasks.find(project, ref)
    step = tasks.next_step(task, cfg, worker_override=worker_override, force=force)
    first = not task.latest_report() and not task.load().get("started")
    line = None
    # Claiming and joining the batch happen under one lock, so a commit of another task cannot
    # close the batch between them. Claim first: a refused task never joins the batch, and a
    # task that joins is always really active.
    with tasks.active_lock(project):
        tasks.claim_active(project, task)
        if first:
            try:
                line = branching.start_if_needed(project, task, cfg)
            except BaseException:
                tasks.release_active(project, task)
                raise
    if line:
        print(line, flush=True)
    try:
        refresh_base(project, task)
    except BaseException:
        if first:
            tasks.release_active(project, task)
        raise
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
        if step.phase == "commit" and task.load().get("worktree"):
            raise FlowError(
                f"task {task.id} works in its own worktree; committing worktree tasks is not "
                f"supported yet (set [parallel] isolation = \"shared\" for now)")
        if step.phase == "commit" and gitutil.no_git(project):
            return _checkpoint(project, task, cfg)
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

    workdir = worktrees.workdir(project, task)
    if str(workdir) != str(project) and phase in ("implement", "fix", "review"):
        # The job wrote its report or review inside the worktree: bring it back before the
        # checks below look for it in the main task folder.
        cwd = Path(job["cwd"]) if job.get("cwd") else workdir
        worktrees.sync_back(project, task, cwd,
                            f"review-{rnd}.md" if phase == "review" else f"report-{rnd}.md")

    error = result.get("error") if result.get("status") != "ok" else None
    if not error and phase in ("implement", "fix") and not task.report(rnd).is_file():
        error = f"worker finished without writing {task.rel(task.report(rnd))}"
    if not error and phase == "review" and task.verdict(rnd) not in tasks.VERDICTS:
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
                  f"`verdict: approved`, `verdict: changes_requested` or `verdict: plan_problem`, "
                  f"and run `aaagent dispatch {task.id}`.")
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
        for line in branching.on_idle(project, cfg):
            print(line, flush=True)
        return None

    if phase == "review":
        verdict = task.verdict(rnd)
        task.update(current_job=None, status=verdict, review_failed_round=None,
                    plan_problem_digest=(tasks.plan_digest(task)
                                         if verdict == "plan_problem" else None))
        task.log_event(f"review round {rnd}: {verdict}", worker=who)
        print(f"✓ review round {rnd} by {who} ({cfg.worker_label(who)}) in {mins:.1f} min: "
              f"{verdict.replace('_', ' ')}")
        print(f"  review: {task.rel(task.review(rnd))}")
        if verdict == "approved":
            print(f"Next: the reviewer approved round {rnd}. Read the review and the report, look "
                  f"over `aaagent diff {task.id}`, then run `aaagent dispatch {task.id}` to commit. "
                  f"To overrule the approval, overwrite the review file with "
                  f"`verdict: changes_requested` and your required changes, then dispatch.")
        elif verdict == "plan_problem":
            print(f"Next: the reviewer found a problem in the plan (round {rnd}). Read "
                  f"{task.rel(task.review(rnd))}, revise {task.rel(task.plan)} (widen `files:` if "
                  f"needed, ask the user if the goal itself changes), then run "
                  f"`aaagent dispatch {task.id}` for a fix round against the revised plan.")
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
    print(gitutil.diff_stat(workdir, state.get("base_commit"), task.pathspecs()))
    if cfg.reviewer_worker:
        print(f"Next: the reviewer ({cfg.worker_label(REVIEWER)}) reviews round {rnd} "
              f"(`aaagent dispatch {task.id}` continues if this command was interrupted).")
    else:
        print(f"Next: read the report, inspect `aaagent diff {task.id}`, re-run the validation, then write "
              f"{task.rel(task.review(rnd))} with `verdict: approved`, `verdict: changes_requested` or "
              f"`verdict: plan_problem`, and run `aaagent dispatch {task.id}`.")
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
    lines = branching.on_idle(project, Config.load(project))
    if lines:
        text += "\n" + "\n".join(lines)
    return text
