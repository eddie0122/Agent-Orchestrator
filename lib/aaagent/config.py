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

# How a worker job runs: in opencode, or in headless Claude Code (`claude -p`).
BACKENDS = ("opencode", "claude")

# Reserved name: the dedicated queue/daemon that runs review jobs when the reviewer
# is a model. No worker may be called this.
REVIEWER = "reviewer"

# Values of `[reviewer].model` that mean "the chat (the planner) reviews each round".
PLANNER_MODEL_ALIASES = ("planner", "orchestrator", "chat")


def infer_backend(model: str) -> str:
    """opencode model ids are `provider/model`; Claude model names have no slash."""
    return "opencode" if "/" in str(model) else "claude"


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


def _worker_tables(data: dict) -> dict:
    """The dict-valued entries of `[workers]` are workers; the rest are settings."""
    return {k: v for k, v in data.get("workers", {}).items() if isinstance(v, dict)}


def file_worker_names(path: Path) -> list[str]:
    """The worker names one TOML file defines (its dict-valued `[workers.*]` tables)."""
    return list(_worker_tables(_load(path)))


def seed_workers() -> dict[str, dict]:
    """The worker tables of config.default.toml: the starting set."""
    return {k: dict(v) for k, v in _worker_tables(_load(DEFAULT_CONFIG)).items()}


class Config:
    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def load(cls, project: Path | None = None) -> "Config":
        default = _load(DEFAULT_CONFIG)
        user = _load(user_config_path())
        proj = _load(project_config_path(project)) if project is not None else {}
        data = _merge(default, user)
        if project is not None:
            data = _merge(data, proj)
        # Workers are generic slots owned by the user/project config. While neither
        # defines any worker, the default file's starting set (the "seed") applies.
        # As soon as one does, the seed is dropped entirely, so a user can remove
        # every seed worker. Settings in `[workers]` (opencode_bin, agents, …) still
        # merge from all three files.
        own: dict = {}
        for source in (user, proj):
            for name, table in _worker_tables(source).items():
                own[name] = _merge(own[name], table) if name in own else table
        # `reviewer` is a reserved name: the review queue, never a worker slot.
        own.pop(REVIEWER, None)
        workers = _worker_tables(default) if not own else own
        workers.pop(REVIEWER, None)
        settings = {k: v for k, v in data.get("workers", {}).items() if not isinstance(v, dict)}
        data["workers"] = {**settings, **workers}
        # The default file documents `[reviewer]` with `model = "planner"`. That value
        # counts as "not set" while neither the user config nor the project config says
        # anything about `[reviewer].model`, so a legacy `[flow] reviewer = "<worker>"`
        # keeps working through `reviewer_model`'s fallback.
        if not any(isinstance(s.get("reviewer"), dict) and "model" in s["reviewer"]
                   for s in (user, proj)):
            reviewer = data.get("reviewer")
            if isinstance(reviewer, dict):
                reviewer.pop("model", None)
        return cls(data)

    # -- workers -----------------------------------------------------------
    def worker_names(self) -> list[str]:
        return [k for k, v in self.data.get("workers", {}).items()
                if isinstance(v, dict) and k != REVIEWER]

    # -- reviewer (its own role, not a worker slot) --------------------------
    def _reviewer_section(self) -> dict:
        section = self.data.get("reviewer", {})
        return section if isinstance(section, dict) else {}

    def _legacy_reviewer_worker(self) -> str | None:
        """A legacy `[flow] reviewer = "<worker>"` that names a configured worker."""
        name = str(self.flow.get("reviewer") or "").strip()
        if name and name not in PLANNER_MODEL_ALIASES and name in self.worker_names():
            return name
        return None

    @property
    def reviewer_model(self) -> str:
        """The reviewer's model, or "planner" when the chat reviews each round."""
        model = str(self._reviewer_section().get("model") or "").strip()
        if model.lower() in PLANNER_MODEL_ALIASES:
            return "planner"
        if model:
            return model
        legacy = self._legacy_reviewer_worker()
        return self.worker_model(legacy) if legacy else "planner"

    @property
    def reviewer_thinking(self) -> str:
        level = str(self._reviewer_section().get("thinking") or "").strip()
        if level:
            return level
        legacy = self._legacy_reviewer_worker()
        return self.worker_thinking(legacy) if legacy else ""

    @property
    def reviewer_backend(self) -> str:
        """`opencode` or `claude`: the explicit `backend`, else inferred from the model."""
        backend = self._reviewer_section().get("backend")
        if backend in BACKENDS:
            return backend
        legacy = self._legacy_reviewer_worker()
        if legacy:
            return self.worker_backend(legacy)
        return infer_backend(self.reviewer_model)

    @property
    def reviewer_is_planner(self) -> bool:
        """True when the chat (the planner) reviews each round itself."""
        return self.reviewer_model == "planner"

    @property
    def reviewer_worker(self) -> str | None:
        """The queue that runs review jobs: "reviewer", or None when the planner reviews."""
        return None if self.reviewer_is_planner else REVIEWER

    def _no_reviewer_job(self) -> KeyError:
        return KeyError(f"'{REVIEWER}' has no job: the planner reviews; there is no reviewer job")

    def worker_model(self, name: str) -> str:
        if name == REVIEWER:
            if self.reviewer_is_planner:
                raise self._no_reviewer_job()
            return self.reviewer_model
        workers = self.data.get("workers", {})
        if name not in workers or not isinstance(workers[name], dict):
            raise KeyError(f"unknown worker '{name}' (configured: {', '.join(self.worker_names())})")
        return workers[name]["model"]

    def worker_thinking(self, name: str) -> str:
        if name == REVIEWER:
            if self.reviewer_is_planner:
                raise self._no_reviewer_job()
            return self.reviewer_thinking
        workers = self.data.get("workers", {})
        if name not in workers or not isinstance(workers[name], dict):
            raise KeyError(f"unknown worker '{name}' (configured: {', '.join(self.worker_names())})")
        return str(workers[name].get("thinking") or "")

    def worker_label(self, name: str) -> str:
        """The worker's model, with its thinking level when one is set."""
        model = self.worker_model(name)
        level = self.worker_thinking(name)
        return f"{model}, thinking {level}" if level else model

    def worker_backend(self, name: str) -> str:
        """`opencode` or `claude`: the explicit `backend`, else inferred from the model name."""
        if name == REVIEWER:
            if self.reviewer_is_planner:
                raise self._no_reviewer_job()
            return self.reviewer_backend
        workers = self.data.get("workers", {})
        if name not in workers or not isinstance(workers[name], dict):
            raise KeyError(f"unknown worker '{name}' (configured: {', '.join(self.worker_names())})")
        backend = workers[name].get("backend")
        if backend in BACKENDS:
            return backend
        return infer_backend(self.worker_model(name))

    @property
    def worker_claude_bin(self) -> str:
        return self.data["workers"].get("claude_bin") or ""

    @property
    def opencode_bin(self) -> str:
        return os.environ.get("AAAGENT_OPENCODE_BIN") or self.data["workers"].get("opencode_bin") or "opencode"

    @property
    def implement_agent(self) -> str:
        return self.data["workers"].get("implement_agent", "aaagent-worker")

    @property
    def commit_agent(self) -> str:
        return self.data["workers"].get("commit_agent", "aaagent-committer")

    @property
    def review_agent(self) -> str:
        return self.data["workers"].get("review_agent", "aaagent-reviewer")

    # -- flow --------------------------------------------------------------
    @property
    def flow(self) -> dict:
        return self.data.get("flow", {})

    @property
    def reviewer(self) -> str:
        """Who reviews each round: "orchestrator" (the chat itself) or "reviewer" (its own job).

        The job reviewer's model lives in `[reviewer]`; a legacy `[flow] reviewer =
        "<worker>"` still names a worker whose model the reviewer copies.
        """
        return "orchestrator" if self.reviewer_is_planner else REVIEWER

    @property
    def default_worker(self) -> str:
        names = self.worker_names()
        configured = str(self.flow.get("default_worker") or "").strip()
        if configured in names:
            return configured
        return names[0] if names else configured

    @property
    def fallback_worker(self) -> str:
        names = self.worker_names()
        configured = str(self.flow.get("fallback_worker") or "").strip()
        default = self.default_worker
        if configured in names and configured != default:
            return configured
        if default not in names:
            return configured
        return names[(names.index(default) + 1) % len(names)]

    def alternate(self, worker: str) -> str | None:
        """The worker to hand over to when `worker` fails; the reviewer has no fallback."""
        if worker == REVIEWER:
            return None
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


def _parse_ok(text: str) -> dict | None:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _walk(data: dict, dotted: str) -> object:
    node: object = data
    for part in dotted.split("."):
        node = node[part]  # type: ignore[index]
    return node


def remove_toml_section(path: Path, section: str) -> None:
    """Delete `[section]` (a whole table, e.g. `workers.worker3`), keeping the rest.

    Raises ValueError and leaves the file untouched when the result would not parse
    or the section is still there afterwards.
    """
    if not path.is_file():
        raise ValueError(f"{path} does not exist")
    header_re = re.compile(rf"^\s*\[{re.escape(section)}\]\s*(#.*)?$")
    out: list[str] = []
    skipping = False
    found = False
    for line in path.read_text().splitlines():
        if not skipping and header_re.match(line):
            skipping = True
            found = True
            continue
        if skipping and line.lstrip().startswith("["):
            skipping = False
        if not skipping:
            out.append(line)
    if not found:
        raise ValueError(f"no [{section}] section in {path}; edit it by hand")
    trimmed: list[str] = []
    for line in out:
        if not line.strip() and trimmed and not trimmed[-1].strip():
            continue
        trimmed.append(line)
    new_text = "\n".join(trimmed) + "\n"
    parsed = _parse_ok(new_text)
    if parsed is not None:
        try:
            _walk(parsed, section)
            parsed = None  # still present: not ok
        except (KeyError, TypeError):
            pass
    if parsed is None:
        raise ValueError(f"could not remove [{section}] from {path}; edit it by hand")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(new_text)
    tmp.replace(path)


def rename_toml_section(path: Path, old: str, new: str) -> None:
    """Rename the `[old]` table header to `[new]`, keeping the file's comments.

    Raises ValueError and leaves the file untouched when `[old]` is missing, `[new]`
    already exists, or the result would not parse (for example an inline table).
    """
    if not path.is_file():
        raise ValueError(f"{path} does not exist")
    header_re = re.compile(rf"^(\s*)\[{re.escape(old)}\](\s*.*)$")
    out: list[str] = []
    found = False
    for line in path.read_text().splitlines():
        match = header_re.match(line)
        if match:
            out.append(f"{match.group(1)}[{new}]{match.group(2)}")
            found = True
        else:
            out.append(line)
    if not found:
        raise ValueError(f"no [{old}] section in {path}; edit it by hand")
    new_text = "\n".join(out) + "\n"
    parsed = _parse_ok(new_text)
    ok = False
    if parsed is not None:
        def table(dotted: str) -> object | None:
            try:
                return _walk(parsed, dotted)
            except (KeyError, TypeError):
                return None
        ok = isinstance(table(new), dict) and not isinstance(table(old), dict)
    if not ok:
        raise ValueError(f"could not rename [{old}] to [{new}] in {path}; edit it by hand")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(new_text)
    tmp.replace(path)


# -- one-time migration from model-named workers --------------------------------

# Workers used to be named after their models; they are generic slots now.
LEGACY_WORKER_NAMES = {"deepseek": "worker1", "glm": "worker2"}


def migrate_legacy_workers(project: Path | None) -> list[str]:
    """Rename `[workers.deepseek]`/`[workers.glm]` to worker1/worker2 everywhere.

    Covers both config files and (with a project) the unfinished tasks' state, and
    updates the `[flow]` values that name them. Idempotent: a second run finds
    nothing to do. Never raises: the caller decides what a failure means.
    """
    notes: list[str] = []
    paths = [user_config_path()]
    if project is not None:
        paths.append(project_config_path(project))
    for path in paths:
        if not path.is_file():
            continue
        names = set(file_worker_names(path))
        for old, new in LEGACY_WORKER_NAMES.items():
            if old in names and new not in names:
                try:
                    rename_toml_section(path, f"workers.{old}", f"workers.{new}")
                except ValueError:
                    continue
                notes.append(f"aaagent: renamed worker {old} → {new} in {path}")
    if project is not None:
        notes.extend(_migrate_task_states(project))
    still = set(Config.load(project).worker_names())
    for path in paths:
        if not path.is_file():
            continue
        flow = _load(path).get("flow", {})
        for key in ("default_worker", "fallback_worker", "reviewer"):
            value = str(flow.get(key) or "").strip()
            if value in LEGACY_WORKER_NAMES and value not in still:
                new = LEGACY_WORKER_NAMES[value]
                try:
                    set_toml_value(path, "flow", key, new)
                except ValueError:
                    continue
                notes.append(f"aaagent: flow.{key} {value} → {new} in {path}")
    return notes


def _migrate_task_states(project: Path) -> list[str]:
    """Rename legacy worker names in unfinished task state; stop daemons still using them."""
    from . import tasks as tasks_mod  # imported here to avoid an import cycle
    from . import worker as worker_mod

    notes: list[str] = []
    renamed: set[str] = set()
    for task in tasks_mod.all_tasks(project):
        try:
            state = task.load()
        except (OSError, json.JSONDecodeError):
            continue
        if state.get("status") in tasks_mod.FINAL_STATUSES:
            continue
        changed = False
        old_worker = state.get("worker")
        if old_worker in LEGACY_WORKER_NAMES:
            state["worker"] = LEGACY_WORKER_NAMES[old_worker]
            renamed.add(old_worker)
            changed = True
        sessions = state.get("sessions")
        if isinstance(sessions, dict):
            for key in list(sessions):
                base = key.split(":", 1)[0]
                if base in LEGACY_WORKER_NAMES:
                    sessions[LEGACY_WORKER_NAMES[base] + key[len(base):]] = sessions.pop(key)
                    renamed.add(base)
                    changed = True
        if changed:
            task.save(state)
    if renamed:
        notes.append("aaagent: renamed worker "
                     + ", ".join(f"{old} → {LEGACY_WORKER_NAMES[old]}" for old in sorted(renamed))
                     + " in unfinished tasks")
    cfg = Config.load(project)
    for old in sorted(LEGACY_WORKER_NAMES):
        if old in cfg.worker_names():  # still a real worker: leave its daemon alone
            continue
        if worker_mod.running_job(project, old) is None and not worker_mod.pending_jobs(project, old):
            worker_mod.stop(project, old)
    return notes
