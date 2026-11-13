"""Configuration loading: defaults < user config < project config."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # install root (repo folder)
DEFAULT_CONFIG = ROOT / "config.default.toml"
USER_CONFIG = Path(os.environ.get("AAAGENT_USER_CONFIG", Path.home() / ".config" / "aaagent" / "config.toml"))
AGENT_DIR_NAME = ".aaagent"


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
        data = _merge(data, _load(USER_CONFIG))
        if project is not None:
            data = _merge(data, _load(project / AGENT_DIR_NAME / "config.toml"))
        return cls(data)

    # -- workers -----------------------------------------------------------
    def worker_names(self) -> list[str]:
        return [k for k, v in self.data.get("workers", {}).items() if isinstance(v, dict)]

    def worker_model(self, name: str) -> str:
        workers = self.data.get("workers", {})
        if name not in workers or not isinstance(workers[name], dict):
            raise KeyError(f"unknown worker '{name}' (configured: {', '.join(self.worker_names())})")
        return workers[name]["model"]

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
    def track_agent_dir(self) -> bool:
        return bool(self.data.get("git", {}).get("track_agent_dir", False))
