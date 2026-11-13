"""Command line entry point for `aaagent`."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, attach, dispatch, gitutil, models, session, sessions, status, tasks, tui, worker
from .config import ROOT, USER_CONFIG, Config
from .tasks import FlowError

COMMANDS = {"start", "ui", "worker", "new", "dispatch", "wait", "status", "diff", "logs", "stop",
            "abandon", "attach", "doctor", "version", "help", "model", "thinking", "sessions"}
# Options of `start` that also work without the word: `aaagent -c`.
START_FLAGS = {"-c", "--continue", "--resume", "--no-tmux", "--no-workers", "--model", "--session"}


def project_root() -> Path:
    env = os.environ.get("AAAGENT_PROJECT")
    if env:
        return Path(env)
    root = gitutil.find_root(Path.cwd())
    if root is None:
        raise FlowError("not inside a git repository. Run `aaagent` from your project folder.")
    return root


# -- commands ----------------------------------------------------------------

def cmd_start(args) -> int:
    prompt = " ".join(args.prompt) if args.prompt else None
    return session.launch(Path.cwd(), prompt, use_tmux=not (args.no_tmux or args.no_workers),
                          manage_workers=not args.no_workers, resume=args.resume,
                          model=args.model, session_ref=args.session)


def cmd_model(args) -> int:
    project = project_root()
    cfg = Config.load(project)
    if args.json:
        print(json.dumps({
            "planner": {"model": cfg.planner_model, "choices": models.choices(cfg, models.PLANNER)},
            "workers": {name: {"model": cfg.worker_model(name)} for name in cfg.worker_names()},
            "worker_choices": models.worker_choices(cfg),
        }, indent=1))
        return 0
    if args.list:
        if not args.target:
            raise FlowError("--list needs a target: use `aaagent model --list planner|worker`")
        for name in models.choices(cfg, models.resolve_target(cfg, args.target)):
            print(name)
        return 0
    if args.target:
        return _set_or_pick(args, project, cfg, models.resolve_target(cfg, args.target))
    rows = [(models.PLANNER, cfg.planner_model)]
    rows += [(name, cfg.worker_model(name)) for name in cfg.worker_names()]
    width = max(len(name) for name, _ in rows)
    for name, model in rows:
        print(f"{name:<{width}}  {model}")
    if sys.stdin.isatty():
        answer = input(f"change which? (planner/{'/'.join(cfg.worker_names())}, enter = none): ")
        if answer.strip():
            target = models.resolve_target(cfg, answer.strip())
            return _set_or_pick(args, project, cfg, target)
    return 0


def _set_or_pick(args, project: Path, cfg, target: str) -> int:
    """With a model, set it; otherwise pick one from a list on a terminal."""
    if args.model:
        print("\n".join(models.set_model(project, args.project, target, args.model)))
        return 0
    if not sys.stdin.isatty():
        raise FlowError("no model given; use `aaagent model <target> <model>`")
    chosen = models.pick_interactive(cfg, target)
    if chosen is None:
        print("unchanged")
        return 0
    print("\n".join(models.set_model(project, args.project, target, chosen)))
    return 0


def cmd_thinking(args) -> int:
    project = project_root()
    cfg = Config.load(project)
    if args.json:
        print(json.dumps({
            "planner": {"model": cfg.planner_model, "thinking": cfg.planner_thinking,
                        "choices": models.thinking_choices(cfg, models.PLANNER)},
            "workers": {name: {"model": cfg.worker_model(name),
                               "thinking": cfg.worker_thinking(name),
                               "choices": models.thinking_choices(cfg, name)}
                        for name in cfg.worker_names()},
        }, indent=1))
        return 0
    if args.list:
        if not args.target:
            raise FlowError("--list needs a target: use `aaagent thinking --list planner|worker`")
        for level in models.thinking_choices(cfg, models.resolve_target(cfg, args.target)):
            print(level)
        return 0
    if args.target:
        return _set_or_pick_thinking(args, project, cfg, models.resolve_target(cfg, args.target))
    rows = [("target", "model", "thinking"),
            (models.PLANNER, cfg.planner_model, cfg.planner_thinking or "default")]
    rows += [(name, cfg.worker_model(name), cfg.worker_thinking(name) or "default")
             for name in cfg.worker_names()]
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        print(f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]}")
    if sys.stdin.isatty():
        answer = input(f"change which? (planner/{'/'.join(cfg.worker_names())}, enter = none): ")
        if answer.strip():
            target = models.resolve_target(cfg, answer.strip())
            return _set_or_pick_thinking(args, project, cfg, target)
    return 0


def _set_or_pick_thinking(args, project: Path, cfg, target: str) -> int:
    """With a level, set it; otherwise pick one from a list on a terminal."""
    if args.level:
        print("\n".join(models.set_thinking(project, args.project, target, args.level)))
        return 0
    if not sys.stdin.isatty():
        raise FlowError("no thinking level given; use `aaagent thinking <target> <level>`")
    chosen = models.pick_thinking_interactive(cfg, target)
    if chosen is None:
        print("unchanged")
        return 0
    print("\n".join(models.set_thinking(project, args.project, target, chosen)))
    return 0


def cmd_ui(args) -> int:
    project = project_root()
    session.init_project(project, Config.load(project))
    if args.snapshot:
        print(tui.snapshot(project, args.width))
        return 0
    return tui.run(project)


def cmd_worker(args) -> int:
    project = project_root()
    session.init_project(project, Config.load(project))
    return worker.serve(project, args.name, once=args.once, quiet_wait=args.quiet_wait)


def cmd_new(args) -> int:
    project = project_root()
    session.init_project(project, Config.load(project))
    task = tasks.create(project, " ".join(args.title))
    state = task.load()
    print(f"task: {task.id}")
    print(f"plan: {task.rel(task.plan)}")
    if state["preexisting_dirty"]:
        print("warning: these files already had uncommitted changes and will be kept out of the "
              f"task's commit: {', '.join(state['preexisting_dirty'])}")
    current = tasks.active_task(project)
    if current:
        print(f"note: task {current} is still in progress; it must be committed or abandoned "
              f"before {task.id} can be dispatched.")
    return 0


def cmd_dispatch(args) -> int:
    return dispatch.dispatch(project_root(), args.task, worker_override=args.worker, force=args.force)


def cmd_wait(args) -> int:
    return dispatch.wait(project_root(), args.task)


def cmd_status(args) -> int:
    project = project_root()
    cfg = Config.load(project)
    if args.json:
        print(json.dumps(status.status_data(project, cfg), indent=1))
        return 0
    print("workers:")
    for name in cfg.worker_names():
        pid = worker.worker_pid(project, name)
        job = worker.running_job(project, name)
        queued = len(worker.pending_jobs(project, name)) if worker.queue_dir(project, name).is_dir() else 0
        doing = f"running {job['phase']} {job['task']} r{job['round']}" if job else ("idle" if pid else "-")
        print(f"  {name:<9} {cfg.worker_label(name):<36} {'up (pid %d)' % pid if pid else 'stopped':<15} "
              f"{doing}{f', {queued} queued' if queued else ''}")
    all_tasks = tasks.all_tasks(project)
    if not args.all:
        all_tasks = [t for t in all_tasks if t.load().get("status") not in tasks.FINAL_STATUSES] or all_tasks[-3:]
    print("tasks:" if all_tasks else "tasks: none yet")
    for task in all_tasks:
        dispatch.print_status_line(task, cfg)
    return 0


def cmd_sessions(args) -> int:
    project = project_root()
    known = sessions.list_sessions(project, limit=None if args.all else 20)
    if args.json:
        print(json.dumps(known, indent=1))
        return 0
    if not known:
        print("no chat sessions yet; run `aaagent` to start one.")
        return 0
    for session in known:
        mark = "*" if session["current"] else " "
        when = (session["last_active"] or "")[:16].replace("T", " ")
        count = f"{len(session['tasks'])} tasks" if session["tasks"] else "no tasks"
        title = session["title"] or "(no messages yet)"
        if not session["available"]:
            title += " (gone)"
        print(f"{mark} {session['short']}  {when}  {count:<8}  {title}")
    print("\nreopen one with: aaagent --session <id>")
    return 0


def cmd_diff(args) -> int:
    project = project_root()
    task = tasks.find(project, args.task)
    base = task.load().get("base_commit")
    text = gitutil.diff_stat(project, base) if args.stat else gitutil.full_diff(project, base)
    print(text if text.strip() else "(no changes)")
    return 0


def cmd_logs(args) -> int:
    project = project_root()
    log = worker.log_file(project, args.worker)
    if not log.is_file():
        print(f"no log for {args.worker} yet; it has not run in this project.")
        return 1
    if args.follow:
        return subprocess.call(["tail", "-n", str(args.lines), "-f", str(log)])
    lines = log.read_text(errors="replace").splitlines()
    print("\n".join(lines[-args.lines:]))
    return 0


def cmd_stop(args) -> int:
    project = project_root()
    cfg = Config.load(project)
    for name in args.workers or cfg.worker_names():
        if name not in cfg.worker_names():
            raise FlowError(f"unknown worker '{name}' (configured: {', '.join(cfg.worker_names())})")
        if worker.stop(project, name):
            print(f"stopped {name}")
    return 0


def cmd_abandon(args) -> int:
    print(dispatch.abandon(project_root(), args.task))
    return 0


def cmd_attach(args) -> int:
    project = project_root()
    session.init_project(project, Config.load(project))
    data = sys.stdin.buffer.read() if "-" in args.files else None
    paths = attach.attach_files(project, args.files, data, args.name)
    for path in paths:
        print(f"attached {Path(os.path.relpath(path, project)).as_posix()}")
    mentions = "".join(attach.mention(project, path) for path in paths)
    if not args.no_send and attach.type_into_chat(project, mentions):
        print(f"typed into the chat input: {mentions.strip()}")
    else:
        print(f"paste this into the chat: {mentions.strip()}")
    return 0


def _check(ok: bool, label: str, hint: str = "", optional: bool = False) -> bool:
    mark = "ok  " if ok else ("warn" if optional else "FAIL")
    print(f"  {mark}  {label}" + ("" if ok or not hint else f"\n        → {hint}"))
    return ok


def cmd_doctor(args) -> int:
    root = gitutil.find_root(Path.cwd())
    cfg = Config.load(root)
    good = True
    print(f"aaagent {__version__} at {ROOT}")
    good &= _check(sys.version_info >= (3, 11), f"python {sys.version.split()[0]} (needs 3.11+)")
    good &= _check(bool(shutil.which("git")), "git")
    oc = shutil.which(cfg.opencode_bin) or (cfg.opencode_bin if Path(cfg.opencode_bin).is_file() else None)
    good &= _check(bool(oc), f"opencode ({oc or cfg.opencode_bin})", "install: curl -fsSL https://opencode.ai/install | bash")
    if oc:
        auth = subprocess.run([oc, "auth", "list"], capture_output=True, text=True, timeout=30).stdout
        good &= _check("OpenCode Go" in auth or "opencode" in auth.lower(), "opencode logged in to OpenCode Go",
                       "run: opencode auth login")
        models = subprocess.run([oc, "models"], capture_output=True, text=True, timeout=60).stdout.split()
        for name in cfg.worker_names():
            model = cfg.worker_model(name)
            good &= _check(model in models, f"worker {name}: {model}", "check `opencode models` and your config")
        agents_dir = Path.home() / ".config" / "opencode" / "agents"
        for agent in (cfg.implement_agent, cfg.commit_agent):
            good &= _check((agents_dir / f"{agent}.md").is_file(), f"opencode agent {agent}", "re-run install.sh")
    claude = session.find_claude(cfg)
    good &= _check(bool(claude), f"claude CLI ({claude or 'not found'})", "re-run install.sh")
    if claude:
        try:
            st = json.loads(subprocess.run([claude, "auth", "status"], capture_output=True,
                                           text=True, timeout=30).stdout or "{}")
        except (json.JSONDecodeError, subprocess.TimeoutExpired):
            st = {}
        good &= _check(bool(st.get("loggedIn")), f"claude logged in ({st.get('authMethod', '?')})",
                       f"run: {claude} auth login")
    has_tmux = bool(shutil.which("tmux"))
    _check(has_tmux, "tmux (optional: shows workers in side panes)", "sudo apt-get install tmux", optional=True)
    print(f"  info  user config: {USER_CONFIG}{'' if USER_CONFIG.is_file() else ' (not created; defaults in use)'}")
    print("all good." if good else "some checks failed.")
    return 0 if good else 1


# -- parser -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aaagent", description=(
        "aaagent: chat with Claude (planner/reviewer); opencode workers implement and commit.\n"
        "Run `aaagent` in a project folder to start, `aaagent \"<request>\"` to start with a request,\n"
        "or `aaagent -c` to reopen the last chat."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("start", help="open the chat and start workers (default)")
    s.add_argument("prompt", nargs="*")
    s.add_argument("--no-tmux", action="store_true", help="run workers in the background instead of panes")
    s.add_argument("--no-workers", action="store_true",
                   help="only open the chat; workers run elsewhere (used by the VSCode extension)")
    s.add_argument("-c", "--continue", "--resume", dest="resume", action="store_true",
                   help="reopen this project's last aaagent chat instead of starting a new one")
    s.add_argument("--session", metavar="ref",
                   help="reopen the chat session <ref> (an id or id prefix from `aaagent sessions`)")
    s.add_argument("--model", help="planner model for this chat only")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("ui", help="dashboard: workers, tasks and live worker output")
    s.add_argument("--snapshot", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--width", type=int, default=80, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_ui)

    s = sub.add_parser("worker", help="run a worker that waits for jobs")
    s.add_argument("name")
    s.add_argument("--once", action="store_true", help="exit when the queue is empty")
    s.add_argument("--quiet-wait", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_worker)

    s = sub.add_parser("new", help="create a task folder and an empty plan")
    s.add_argument("title", nargs="+")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("dispatch", help="send a task's next phase (implement/fix/commit) to a worker and wait")
    s.add_argument("task")
    s.add_argument("--worker", help="use this worker")
    s.add_argument("--force", action="store_true", help="allow a round beyond max_review_rounds")
    s.set_defaults(func=cmd_dispatch)

    s = sub.add_parser("wait", help="wait for a task's running job and record the result")
    s.add_argument("task")
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("status", help="workers and tasks")
    s.add_argument("--all", action="store_true", help="include committed and abandoned tasks")
    s.add_argument("--json", action="store_true", help="machine-readable output with every task")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("sessions", help="this project's chat sessions, newest first")
    s.add_argument("--all", action="store_true", help="show every session, not just the latest 20")
    s.add_argument("--json", action="store_true", help="machine-readable list")
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("diff", help="all changes since the task started")
    s.add_argument("task")
    s.add_argument("--stat", action="store_true")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("logs", help="background worker output")
    s.add_argument("worker")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-n", "--lines", type=int, default=60)
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("stop", help="stop this project's workers (all, or the ones named)")
    s.add_argument("workers", nargs="*")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("abandon", help="drop a task (keeps its uncommitted changes)")
    s.add_argument("task")
    s.set_defaults(func=cmd_abandon)

    s = sub.add_parser(
        "attach",
        help="copy images into .aaagent/attachments/ and mention them in the chat",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=("copy images (PNG, JPEG, GIF, WebP; up to 5 MB) into .aaagent/attachments/ "
                     "and add them to the chat"),
        epilog=("-          read one image from stdin, e.g. "
                "`xclip -selection clipboard -t image/png -o | aaagent attach -`\n"
                "--name     file name for stdin data (default pasted.png)\n"
                "--no-send  only print the @-mentions; do not type them into the chat pane"))
    s.add_argument("files", nargs="+", metavar="file", help="an image file, or - for stdin")
    s.add_argument("--name", help="file name for stdin data (default pasted.png)")
    s.add_argument("--no-send", action="store_true",
                   help="only print the @-mentions; do not type them into the chat pane")
    s.set_defaults(func=cmd_attach)

    s = sub.add_parser("doctor", help="check the installation")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("model", help="show or change the planner and worker models")
    s.add_argument("target", nargs="?", help="planner or a worker name")
    s.add_argument("model", nargs="?", help="model name (any name; not checked)")
    s.add_argument("--list", action="store_true", help="print the choices for <target>")
    s.add_argument("--json", action="store_true", help="machine-readable models and choices")
    s.add_argument("--project", action="store_true",
                   help="write <project>/.aaagent/config.toml instead of the user config")
    s.set_defaults(func=cmd_model)

    s = sub.add_parser("thinking", help="show or change the planner and worker thinking levels")
    s.add_argument("target", nargs="?", help="planner or a worker name")
    s.add_argument("level", nargs="?", help="thinking level, or default to clear it")
    s.add_argument("--list", action="store_true", help="print the choices for <target>")
    s.add_argument("--json", action="store_true", help="machine-readable thinking levels and choices")
    s.add_argument("--project", action="store_true",
                   help="write <project>/.aaagent/config.toml instead of the user config")
    s.set_defaults(func=cmd_thinking)

    s = sub.add_parser("version")
    s.set_defaults(func=lambda a: print(__version__) or 0)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "help":
        argv = ["--help"]
    if not argv or (argv[0] not in COMMANDS and not argv[0].startswith("-")) or argv[0] in START_FLAGS:
        argv = ["start", *argv]
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except FlowError as exc:
        print(f"aaagent: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
