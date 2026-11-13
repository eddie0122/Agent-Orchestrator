"""Choosing the planner and worker models (`aaagent model`)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import (PLANNER_CHOICES, Config, project_config_path, set_toml_value,
                     user_config_path)
from .tasks import FlowError

PLANNER = "planner"
PLANNER_ALIASES = {"planner", "orchestrator", "claude"}


def resolve_target(cfg: Config, name: str) -> str:
    """`planner` (or an alias) or the name of a configured worker."""
    if name in PLANNER_ALIASES:
        return PLANNER
    if name in cfg.worker_names():
        return name
    raise FlowError(f"unknown target '{name}': use planner or a worker "
                    f"({', '.join(cfg.worker_names())})")


def current(cfg: Config, target: str) -> str:
    return cfg.planner_model if target == PLANNER else cfg.worker_model(target)


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


def choices(cfg: Config, target: str) -> list[str]:
    """Suggested models for `target`, always including the one in use."""
    out = list(PLANNER_CHOICES) if target == PLANNER else worker_choices(cfg)
    now = current(cfg, target)
    if now not in out:
        out.insert(0, now)
    return out


def set_model(project: Path, cfg_scope_project: bool, target: str, model: str) -> list[str]:
    """Save `model` for `target` and return the lines to show the user."""
    model = model.strip()
    if not model:
        raise FlowError("model name is empty")
    path = project_config_path(project) if cfg_scope_project else user_config_path()
    if target == PLANNER:
        section = "orchestrator"
    else:
        section = f"workers.{target}"
    try:
        set_toml_value(path, section, "model", model)
    except ValueError as exc:
        raise FlowError(str(exc)) from exc
    notes = [f"{target} model: {model}  (saved to {path})"]
    cfg = Config.load(project)
    if current(cfg, target) != model:
        notes.append(f"note: {project_config_path(project)} overrides this; "
                     "use --project to change it there")
    if target == PLANNER:
        notes.append(f"applies when the next chat starts; in a running chat type /model {model}")
    else:
        notes.append("applies from the next dispatch")
        available = worker_choices(cfg)
        if available and model not in available:
            notes.append(f"warning: {model} is not in `opencode models`; check the name")
    return notes


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
