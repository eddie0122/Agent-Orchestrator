"""Choosing the planner and worker models (`aaagent model`) and thinking levels (`aaagent thinking`)."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

from .config import (PLANNER_CHOICES, PLANNER_THINKING, PLANNER_MODEL_ALIASES, REVIEWER,
                     BACKENDS, WORKER_THINKING_FALLBACK, Config, file_worker_names,
                     project_config_path, seed_workers, remove_toml_section, set_toml_value,
                     user_config_path)
from .tasks import FlowError
PLANNER = "planner"
PLANNER_ALIASES = {"planner", "orchestrator", "claude"}
ORCHESTRATOR = "orchestrator"

# Worker names are generic slots; `add` only ever creates `workerN` names like these.
WORKER_NAME_RE = r"^[a-z][a-z0-9_-]*$"


def resolve_target(cfg: Config, name: str) -> str:
    """`planner`, `reviewer`, or the name of a configured worker."""
    if name in PLANNER_ALIASES:
        return PLANNER
    if name == REVIEWER:
        return REVIEWER
    if name in cfg.worker_names():
        return name
    raise FlowError(f"unknown target '{name}': use planner, reviewer or a worker "
                    f"({', '.join(cfg.worker_names())})")


def current(cfg: Config, target: str) -> str:
    if target == PLANNER:
        return cfg.planner_model
    if target == REVIEWER:
        return cfg.reviewer_model
    return cfg.worker_model(target)


def worker_choices(cfg: Config) -> list[str]:
    """Model ids from `opencode models`; empty when opencode cannot be asked."""
    try:
        proc = subprocess.run([cfg.opencode_bin, "models"], capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def claude_choices() -> list[str]:
    """Claude model names a worker can run on (headless Claude Code)."""
    return list(PLANNER_CHOICES)


def choices(cfg: Config, target: str) -> list[str]:
    """Suggested models for `target`, always including the one in use."""
    if target == PLANNER:
        out = list(PLANNER_CHOICES)
    elif target == REVIEWER:
        out = ["planner", *new_worker_choices(cfg)]
    else:
        out = new_worker_choices(cfg)
    now = current(cfg, target)
    if now not in out:
        out.insert(0, now)
    return out


def new_worker_choices(cfg: Config) -> list[str]:
    """Model ids a new worker could run: opencode's models, then Claude model names."""
    out = worker_choices(cfg)
    out += [name for name in claude_choices() if name not in out]
    return out


def set_model(project: Path, cfg_scope_project: bool, target: str, model: str) -> list[str]:
    """Save `model` for `target` and return the lines to show the user."""
    model = model.strip()
    if not model:
        raise FlowError("model name is empty")
    if target == REVIEWER and model.lower() in PLANNER_MODEL_ALIASES:
        model = "planner"
    path = project_config_path(project) if cfg_scope_project else user_config_path()
    if target == PLANNER:
        section = "orchestrator"
    elif target == REVIEWER:
        section = REVIEWER
    else:
        materialize_seed(project)
        section = f"workers.{target}"
    try:
        set_toml_value(path, section, "model", model)
        if target == REVIEWER:
            # The backend is inferred from the model: a stale explicit one would run the
            # new model on the wrong backend, so clear it when the model changes.
            set_toml_value(path, REVIEWER, "backend", "")
    except ValueError as exc:
        raise FlowError(str(exc)) from exc
    if target == REVIEWER:
        return _reviewer_model_notes(project, path, model)
    notes = [f"{target} model: {model}  (saved to {path})"]
    cfg = Config.load(project)
    if current(cfg, target) != model:
        notes.append(f"note: {project_config_path(project)} overrides this; "
                     "use --project to change it there")
    if target == PLANNER:
        notes.append(f"applies when the next chat starts; in a running chat type /model {model}")
    else:
        notes.append("applies from the next dispatch")
        notes.extend(backend_notes(cfg, target, model))
    return notes


def _clear_legacy_flow_reviewer(path: Path) -> str | None:
    """Empty a legacy `[flow] reviewer = "<worker>"` now that `[reviewer]` holds the model."""
    if not path.is_file():
        return None
    flow = tomllib.loads(path.read_text()).get("flow", {})
    value = str(flow.get("reviewer") or "").strip()
    if not value:
        return None
    try:
        set_toml_value(path, "flow", "reviewer", "")
    except ValueError:
        return None
    return f'note: flow.reviewer was "{value}"; cleared ([reviewer] holds the reviewer now)'


def _reviewer_model_notes(project: Path, path: Path, model: str) -> list[str]:
    """The lines `set_model` and `set_reviewer` show after writing `[reviewer].model`."""
    notes: list[str] = []
    cleared = _clear_legacy_flow_reviewer(path)
    cfg = Config.load(project)
    notes.append(f"reviewer: {reviewer_label(cfg)}  (saved to {path})")
    if cleared:
        notes.append(cleared)
    if current(cfg, REVIEWER) != model:
        notes.append(f"note: {project_config_path(project)} overrides this; "
                     "use --project to change it there")
    notes.append("applies from the next dispatch")
    if cfg.reviewer_is_planner:
        notes.append("a chat that is already open learns this from dispatch's messages; "
                     "new chats are told at the start")
        return notes
    notes.append("one dispatch now runs implement → review → fix until approval; "
                 "the planner still commits")
    notes.append("a chat that is already open learns this from dispatch's messages; "
                 "new chats are told at the start")
    notes.extend(backend_notes(cfg, REVIEWER, model))
    return notes


def backend_notes(cfg: Config, name: str, model: str) -> list[str]:
    """The opencode/Claude notes `aaagent model` and `aaagent workers add` share."""
    if cfg.worker_backend(name) == "claude":
        return [f"{name} now runs on Claude Code (claude -p), not opencode"]
    available = worker_choices(cfg)
    if available and model not in available:
        return [f"warning: {model} is not in `opencode models`; check the name"]
    return []


def pick_interactive(cfg: Config, target: str, input_fn=input, print_fn=print) -> str | None:
    """Ask for a model; None means keep the current one."""
    available = choices(cfg, target)
    now = current(cfg, target)
    for number, name in enumerate(available, 1):
        print_fn(f"  {number}. {name}" + ("  (current)" if name == now else ""))
    for _ in range(3):
        answer = input_fn("number or model name (enter = keep current): ").strip()
        if not answer:
            return None
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(available):
                return available[index - 1]
            print_fn(f"error: pick a number from 1 to {len(available)}")
            continue
        return answer
    return None


# -- who reviews each round ----------------------------------------------------

def reviewer_choices(cfg: Config) -> list[str]:
    """`orchestrator` (the chat reviews) and every configured worker.

    Kept for the dashboard's `v` picker and the VSCode Select Reviewer: picking a worker
    copies that worker's model into `[reviewer]`.
    """
    return [ORCHESTRATOR, *cfg.worker_names()]


def reviewer_label(cfg: Config) -> str:
    """`planner (the chat reviews each round)` or `reviewer (<model>[, thinking <level>])`."""
    if cfg.reviewer_is_planner:
        return f"{PLANNER} (the chat reviews each round)"
    return f"{REVIEWER} ({cfg.worker_label(REVIEWER)})"


def set_reviewer(project: Path, cfg_scope_project: bool, value: str) -> list[str]:
    """Set who reviews each round and return the lines to show the user.

    `planner` (or an alias) makes the chat review again; a configured worker's name
    copies that worker's model and thinking into `[reviewer]` (a snapshot, not a link);
    anything else is a model id for `[reviewer].model`.
    """
    value = (value or "").strip()
    if not value:
        raise FlowError("name the reviewer: planner, a worker, or a model")
    cfg = Config.load(project)
    if value in cfg.worker_names():      # a worker name wins over an alias
        path = project_config_path(project) if cfg_scope_project else user_config_path()
        model = cfg.worker_model(value)
        thinking = cfg.worker_thinking(value)
        backend = (cfg.data.get("workers", {}).get(value) or {}).get("backend")
        try:
            set_toml_value(path, REVIEWER, "model", model)
            # Always write both, even when empty: a level or backend left over from the
            # previous reviewer must not survive (an old `backend = "claude"` would run
            # an opencode model on Claude Code).
            set_toml_value(path, REVIEWER, "thinking", thinking)
            set_toml_value(path, REVIEWER, "backend", backend if backend in BACKENDS else "")
        except ValueError as exc:
            raise FlowError(str(exc)) from exc
        notes: list[str] = []
        cleared = _clear_legacy_flow_reviewer(path)
        if cleared:
            notes.append(cleared)
        notes.append(f"reviewer: {reviewer_label(Config.load(project))}  (saved to {path})")
        notes.append(f"reviewer now uses {value}'s model {model} "
                     f"(a copy; changing {value} later does not change the reviewer)")
        notes.append("applies from the next dispatch")
        notes.append("one dispatch now runs implement → review → fix until approval; "
                     "the planner still commits")
        notes.append("a chat that is already open learns this from dispatch's messages; "
                     "new chats are told at the start")
        return notes
    if value.lower() in PLANNER_MODEL_ALIASES:
        return set_model(project, cfg_scope_project, REVIEWER, "planner")
    return set_model(project, cfg_scope_project, REVIEWER, value)


# -- thinking levels -----------------------------------------------------------

def thinking_current(cfg: Config, target: str) -> str:
    """The thinking level in use for `target`; "" means the tool's own default."""
    if target == PLANNER:
        return cfg.planner_thinking
    if target == REVIEWER:
        return cfg.reviewer_thinking
    return cfg.worker_thinking(target)


def worker_thinking_choices(cfg: Config, model: str) -> list[str]:
    """The variant ids `model` offers, from `opencode api GET /api/model`.

    Any failure (opencode missing, timeout, bad JSON, no matching model) falls back to
    WORKER_THINKING_FALLBACK.
    """
    fallback = list(WORKER_THINKING_FALLBACK)
    try:
        proc = subprocess.run([cfg.opencode_bin, "api", "GET", "/api/model"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    if proc.returncode != 0:
        return fallback
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return fallback
    for entry in data.get("data") or []:
        if not isinstance(entry, dict):
            continue
        if f"{entry.get('providerID')}/{entry.get('id')}" == model:
            variants = [v.get("id") for v in entry.get("variants") or []
                        if isinstance(v, dict) and v.get("id")]
            return variants or fallback
    return fallback


def thinking_choices(cfg: Config, target: str) -> list[str]:
    """Suggested thinking levels for `target`."""
    if target == PLANNER:
        return list(PLANNER_THINKING)
    if target == REVIEWER:
        if cfg.reviewer_is_planner:
            return []
        if cfg.reviewer_backend == "claude":
            return list(PLANNER_THINKING)
        return worker_thinking_choices(cfg, cfg.reviewer_model)
    if cfg.worker_backend(target) == "claude":
        return list(PLANNER_THINKING)
    return worker_thinking_choices(cfg, cfg.worker_model(target))


def set_thinking(project: Path, cfg_scope_project: bool, target: str, level: str) -> list[str]:
    """Save `level` for `target` ("" or `default` = unset) and return the lines to show."""
    level = level.strip()
    if level == "default":
        level = ""
    path = project_config_path(project) if cfg_scope_project else user_config_path()
    if target == PLANNER:
        section = "orchestrator"
    elif target == REVIEWER:
        if Config.load(project).reviewer_is_planner:
            raise FlowError("the planner reviews; change the planner's thinking instead")
        section = REVIEWER
    else:
        materialize_seed(project)
        section = f"workers.{target}"
    try:
        set_toml_value(path, section, "thinking", level)
    except ValueError as exc:
        raise FlowError(str(exc)) from exc
    notes = [f"{target} thinking: {level or 'default'}  (saved to {path})"]
    cfg = Config.load(project)
    if thinking_current(cfg, target) != level:
        notes.append(f"note: {project_config_path(project)} overrides this; "
                     "use --project to change it there")
    if target == PLANNER:
        notes.append("applies when the next chat starts")
    else:
        notes.append("applies from the next dispatch")
    if level:
        available = thinking_choices(cfg, target)
        if level not in available:
            notes.append(f"warning: {level} is not one of: {', '.join(available)}")
    return notes


def pick_thinking_interactive(cfg: Config, target: str, input_fn=input,
                              print_fn=print) -> str | None:
    """Ask for a thinking level; None means keep the current one, "default" clears it."""
    available = thinking_choices(cfg, target)
    now = thinking_current(cfg, target)
    print_fn("  0. default" + ("  (current)" if not now else ""))
    for number, name in enumerate(available, 1):
        print_fn(f"  {number}. {name}" + ("  (current)" if name == now else ""))
    for _ in range(3):
        answer = input_fn("number or level (enter = keep current): ").strip()
        if not answer:
            return None
        if answer.isdigit():
            index = int(answer)
            if index == 0:
                return "default"
            if 1 <= index <= len(available):
                return available[index - 1]
            print_fn(f"error: pick a number from 0 to {len(available)}")
            continue
        return answer
    return None


# -- adding and removing workers -----------------------------------------------

def next_worker_name(cfg: Config) -> str:
    """The lowest `worker<N>` (N ≥ 1) that is not configured."""
    taken = set(cfg.worker_names())
    n = 1
    while f"worker{n}" in taken:
        n += 1
    return f"worker{n}"


def materialize_seed(project: Path) -> None:
    """Write the default file's starting workers into the user config, once.

    While neither the user config nor the project config defines any worker, the
    default file's set (the "seed") applies. Writing it out before the first edit
    keeps it from disappearing by accident: a project-only `[workers.worker1]`
    would otherwise make the whole seed vanish.
    """
    if file_worker_names(user_config_path()):
        return
    proj = project_config_path(project)
    if proj.is_file() and file_worker_names(proj):
        return
    path = user_config_path()
    for name, table in seed_workers().items():
        for key in ("model", "thinking", "backend"):
            if key in table:
                set_toml_value(path, f"workers.{name}", key, str(table[key]))


def add_worker(project: Path, cfg_scope_project: bool, model: str,
               thinking: str = "") -> tuple[str, list[str]]:
    """Add the next free `workerN` slot with `model`; returns its name and the notes."""
    model = (model or "").strip()
    if not model:
        raise FlowError("no model given; use aaagent workers add <model>")
    materialize_seed(project)
    name = next_worker_name(Config.load(project))
    path = project_config_path(project) if cfg_scope_project else user_config_path()
    try:
        set_toml_value(path, f"workers.{name}", "model", model)
        level = thinking.strip()
        if level:
            set_toml_value(path, f"workers.{name}", "thinking", level)
    except ValueError as exc:
        raise FlowError(str(exc)) from exc
    notes = [f"added {name}: {model}  (saved to {path})",
             "applies from the next dispatch; it gets its own pane/terminal "
             "the next time aaagent starts"]
    notes.extend(backend_notes(Config.load(project), name, model))
    return name, notes


def remove_worker(project: Path, cfg_scope_project: bool, name: str) -> list[str]:
    """Remove a worker from the config files; returns the lines to show the user."""
    from . import tasks as tasks_mod  # imported here to avoid import cycles
    from . import worker as worker_mod

    name = (name or "").strip()
    if not re.fullmatch(WORKER_NAME_RE, name):
        raise FlowError(f"invalid worker name '{name}': use a name like worker1 "
                        "(letters, digits, - and _)")
    cfg = Config.load(project)
    names = cfg.worker_names()
    if name not in names:
        raise FlowError(f"unknown worker '{name}' (configured: {', '.join(names)})")
    if len(names) == 1:
        raise FlowError(f"cannot remove {name}: it is the only worker; add another one first")
    if worker_mod.running_job(project, name):
        raise FlowError(f"{name} has a job running; stop it first (`aaagent stop {name}`)")
    if worker_mod.pending_jobs(project, name):
        raise FlowError(f"{name} still has queued jobs; stop it first (`aaagent stop {name}`)")
    for task in tasks_mod.all_tasks(project):
        state = task.load()
        if state.get("status") in tasks_mod.FINAL_STATUSES:
            continue
        if (state.get("current_job") or {}).get("worker") == name:
            raise FlowError(f"task {task.id} has an unfinished job on {name}; "
                            "wait for it before removing the worker")

    materialize_seed(project)
    chosen = project_config_path(project) if cfg_scope_project else user_config_path()
    other = user_config_path() if cfg_scope_project else project_config_path(project)
    notes: list[str] = []
    for path in (chosen, other):
        if name in file_worker_names(path):
            try:
                remove_toml_section(path, f"workers.{name}")
            except ValueError as exc:
                raise FlowError(str(exc)) from exc
            notes.append(f"removed {name}  (from {path})")
    if name in Config.load(project).worker_names():
        raise FlowError(f"could not remove {name} from the config files; edit them by hand")
    for path in (chosen, other):
        notes.extend(clear_flow_references(path, name))
    if worker_mod.stop(project, name):
        notes.append(f"stopped the {name} daemon")
    return notes


def clear_flow_references(path: Path, name: str) -> list[str]:
    """Point `[flow]` away from a removed worker; one note per changed value."""
    if not path.is_file():
        return []
    flow = tomllib.loads(path.read_text()).get("flow", {})
    notes = []
    for key, cleared, why in (("default_worker", "", "the first worker is the default"),
                              ("fallback_worker", "", "the next worker after the default "
                                                      "is the fallback"),
                              ("reviewer", "orchestrator", "the chat reviews again")):
        if str(flow.get(key) or "") == name:
            try:
                set_toml_value(path, "flow", key, cleared)
            except ValueError as exc:
                raise FlowError(str(exc)) from exc
            notes.append(f"flow.{key} was {name}; cleared ({why})")
    return notes
