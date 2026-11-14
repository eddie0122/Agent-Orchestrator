"""Configuration loading: defaults < user config < project config."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # install root (repo folder)
DEFAULT_CONFIG = ROOT / "config.default.toml"
USER_CONFIG = Path(os.environ.get("AAAGENT_USER_CONFIG", Path.home() / ".config" / "aaagent" / "config.toml"))
AGENT_DIR_NAME = ".aaagent"

# Suggested Claude models for `aaagent model`; any other name may still be typed.
PLANNER_CHOICES = ["opus", "sonnet", "haiku", "fable",
                   "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2"]

# Thinking levels: Claude Code's `--effort` values, and the fallback opencode variants.
PLANNER_THINKING = ["low", "medium", "high", "xhigh", "max"]
WORKER_THINKING_FALLBACK = ["low", "high", "max"]


def user_config_path() -> Path:
    """The user-wide config file (looked up at call time so tests can set the env var)."""
    return Path(os.environ.get("AAAGENT_USER_CONFIG", Path.home() / ".config" / "aaagent" / "config.toml"))


def project_config_path(project: Path) -> Path:
    return project / AGENT_DIR_NAME / "config.toml"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


class Config:
    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def load(cls, project: Path | None = None) -> "Config":
        data = _load(DEFAULT_CONFIG)
        data = _merge(data, _load(user_config_path()))
        if project is not None:
            data = _merge(data, _load(project_config_path(project)))
        return cls(data)

    # -- workers -----------------------------------------------------------
    def worker_names(self) -> list[str]:
        return [k for k, v in self.data.get("workers", {}).items() if isinstance(v, dict)]

    def worker_model(self, name: str) -> str:
        workers = self.data.get("workers", {})
        if name not in workers or not isinstance(workers[name], dict):
            raise KeyError(f"unknown worker '{name}' (configured: {', '.join(self.worker_names())})")
        return workers[name]["model"]

    def worker_thinking(self, name: str) -> str:
        workers = self.data.get("workers", {})
        if name not in workers or not isinstance(workers[name], dict):
            raise KeyError(f"unknown worker '{name}' (configured: {', '.join(self.worker_names())})")
        return str(workers[name].get("thinking") or "")

    def worker_label(self, name: str) -> str:
        """The worker's model, with its thinking level when one is set."""
        model = self.worker_model(name)
        level = self.worker_thinking(name)
        return f"{model}, thinking {level}" if level else model

    @property
    def opencode_bin(self) -> str:
        return os.environ.get("AAAGENT_OPENCODE_BIN") or self.data["workers"].get("opencode_bin") or "opencode"

    @property
    def implement_agent(self) -> str:
        return self.data["workers"].get("implement_agent", "aaagent-worker")

    @property
    def commit_agent(self) -> str:
        return self.data["workers"].get("commit_agent", "aaagent-committer")

    # -- flow --------------------------------------------------------------
    @property
    def flow(self) -> dict:
        return self.data.get("flow", {})

    @property
    def default_worker(self) -> str:
        return self.flow.get("default_worker", "deepseek")

    @property
    def fallback_worker(self) -> str:
        return self.flow.get("fallback_worker", "glm")

    def alternate(self, worker: str) -> str | None:
        """The worker to hand over to when `worker` fails."""
        alt = self.fallback_worker if worker != self.fallback_worker else self.default_worker
        return alt if alt != worker and alt in self.worker_names() else None

    @property
    def max_review_rounds(self) -> int:
        return int(self.flow.get("max_review_rounds", 3))

    @property
    def fallback_after_failed_reviews(self) -> int:
        return int(self.flow.get("fallback_after_failed_reviews", 2))

    @property
    def job_timeout_seconds(self) -> float:
        return float(self.flow.get("job_timeout_minutes", 30)) * 60

    # -- orchestrator ------------------------------------------------------
    @property
    def orchestrator(self) -> dict:
        return self.data.get("orchestrator", {})

    @property
    def planner_model(self) -> str:
        return str(self.orchestrator.get("model") or "opus")

    @property
    def planner_thinking(self) -> str:
        return str(self.orchestrator.get("thinking") or "")

    @property
    def track_agent_dir(self) -> bool:
        return bool(self.data.get("git", {}).get("track_agent_dir", False))


def set_toml_value(path: Path, section: str, key: str, value: str) -> None:
    """Set `key = value` in `[section]`, keeping comments and the rest of the file.

    The file is created when missing. When the result would not read back as the same
    value (for example a config that uses inline tables), ValueError is raised and the
    file is left untouched.
    """
    text = path.read_text() if path.is_file() else ""
    header_re = re.compile(rf"^\s*\[{re.escape(section)}\]\s*(#.*)?$")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    assignment = f"{key} = {json.dumps(value)}"
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if header_re.match(line)), None)
    if start is None:
        block = [f"[{section}]", assignment]
        lines = lines + [""] + block if text.strip() else block
    else:
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].lstrip().startswith("["):
                end = i
                break
        for i in range(start + 1, end):
            if key_re.match(lines[i]):
                lines[i] = assignment
                break
        else:
            last = start
            for i in range(start + 1, end):
                if lines[i].strip():
                    last = i
            lines.insert(last + 1, assignment)
    new_text = "\n".join(lines) + "\n"
    try:
        parsed: object = tomllib.loads(new_text)
        for part in section.split("."):
            parsed = parsed[part]  # type: ignore[index]
        ok = isinstance(parsed, dict) and parsed.get(key) == value
    except (tomllib.TOMLDecodeError, KeyError, TypeError):
        ok = False
    if not ok:
        raise ValueError(f"could not update {path}; edit it by hand")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(new_text)
    tmp.replace(path)
