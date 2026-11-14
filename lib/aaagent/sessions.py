"""The chat sessions of a project: which conversations created which tasks.

One session is one Claude conversation (the Opus chat); a session can create many tasks.
`.aaagent/sessions.json` keeps them, newest first is what the commands show:

    {"current": "<session id or null>",
     "sessions": [{"id", "transcript_path", "started", "last_active", "title"}]}

The file itself holds the sessions oldest first. It is written by
`claude/hooks/record_session.py` and read by `aaagent sessions`, `aaagent status` and
`aaagent --session`.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

MAX_SESSIONS = 100
TITLE_WIDTH = 80
PREFIX_MIN = 4
# Read only the head of a transcript when a title has to be recovered from it.
MAX_TRANSCRIPT_LINES = 400
MAX_TRANSCRIPT_BYTES = 1024 * 1024


def sessions_file(project: Path) -> Path:
    return project / ".aaagent" / "sessions.json"


def _chat_file(project: Path) -> Path:
    # session.py has chat_file() too, but it imports this module.
    return project / ".aaagent" / "chat.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _migrated(project: Path) -> dict:
    """Sessions seeded from chat.json, as it was written before sessions.json existed."""
    chat = _read_json(_chat_file(project)) or {}
    entries: dict[str, dict] = {}
    for entry in (chat.get("previous"), chat):
        if not isinstance(entry, dict) or not entry.get("session_id"):
            continue
        sid = entry["session_id"]
        entries[sid] = {"id": sid, "transcript_path": entry.get("transcript_path"),
                        "started": entry.get("at"), "last_active": entry.get("at"), "title": ""}
    current = chat.get("session_id")
    return {"current": current if isinstance(current, str) and current else None,
            "sessions": list(entries.values())}


def load(project: Path) -> dict:
    """The sessions file, or an empty history. Seeds itself from chat.json once, on migration."""
    path = sessions_file(project)
    if path.is_file():
        data = _read_json(path)
        if data is None:
            return {"current": None, "sessions": []}
        sessions = [s for s in data.get("sessions") or [] if isinstance(s, dict) and s.get("id")]
        current = data.get("current")
        return {"current": current if isinstance(current, str) and current else None,
                "sessions": sessions}
    return _migrated(project)


def _write(project: Path, data: dict) -> None:
    path = sessions_file(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _entry(data: dict, session_id: str) -> dict | None:
    return next((s for s in data["sessions"] if s.get("id") == session_id), None)


def _cap(data: dict) -> None:
    if len(data["sessions"]) > MAX_SESSIONS:
        data["sessions"] = data["sessions"][-MAX_SESSIONS:]


def record_start(project: Path, session_id: str, transcript_path: str | None,
                 now: str | None = None) -> None:
    """A chat started or resumed (Claude Code's SessionStart)."""
    if not session_id:
        return
    now = now or _now()
    data = load(project)
    entry = _entry(data, session_id)
    if entry is None:
        entry = {"id": session_id, "transcript_path": transcript_path,
                 "started": now, "last_active": now, "title": ""}
        data["sessions"].append(entry)
    else:
        entry["last_active"] = now
        if transcript_path:
            entry["transcript_path"] = transcript_path
        entry.setdefault("started", now)
        entry.setdefault("title", "")
    # Sessions recorded before titles existed (and chats resumed without their first prompt)
    # get theirs from the conversation itself, so it is stored and not re-read on every poll.
    _fill_title(entry)
    _title_other(data, entry)
    _cap(data)
    data["current"] = session_id
    _write(project, data)


def _title(prompt: str) -> str:
    """The prompt's first non-empty line, whitespace collapsed, cut to TITLE_WIDTH."""
    for line in prompt.splitlines():
        text = " ".join(line.split())
        if text:
            return text if len(text) <= TITLE_WIDTH else text[:TITLE_WIDTH - 1].rstrip() + "…"
    return ""


def _prompt_text(record: dict) -> str:
    """The prompt text of a transcript line, or "" when the line is not a real user prompt."""
    if record.get("type") != "user" or record.get("isMeta"):
        return ""
    message = record.get("message")
    if not isinstance(message, dict) or message.get("isMeta"):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            return part["text"]
    return ""


def title_from_transcript(path: str | Path | None) -> str:
    """The first real user prompt in a Claude Code transcript (JSON Lines), as a title."""
    if not path:
        return ""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_TRANSCRIPT_BYTES)
    except OSError:
        return ""
    lines = raw.decode("utf-8", "replace").splitlines()[:MAX_TRANSCRIPT_LINES]
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        text = _prompt_text(record).strip()
        # Command and system wrappers (`<command-name>/clear</command-name>`) are not titles.
        if not text or text.startswith(("<", "/")):
            continue
        return _title(text)
    return ""


def _fill_title(entry: dict) -> None:
    if entry.get("title"):
        return
    title = title_from_transcript(entry.get("transcript_path"))
    if title:
        entry["title"] = title


def _title_other(data: dict, entry: dict) -> None:
    """Title the one other untitled chat (a migration carries the previous chat untitled)."""
    others = [s for s in data["sessions"] if s is not entry and not s.get("title")]
    others.sort(key=_when, reverse=True)
    for other in others[:1]:
        _fill_title(other)


def record_prompt(project: Path, session_id: str, prompt: str, now: str | None = None) -> None:
    """A prompt was sent to a chat (Claude Code's UserPromptSubmit); the first one titles it."""
    if not session_id:
        return
    now = now or _now()
    prompt = prompt or ""
    data = load(project)
    entry = _entry(data, session_id)
    if entry is None:
        entry = {"id": session_id, "transcript_path": None,
                 "started": now, "last_active": now, "title": ""}
        data["sessions"].append(entry)
    entry["last_active"] = now
    entry.setdefault("started", now)
    if not entry.get("title") and not prompt.lstrip().startswith("/"):
        title = _title(prompt)
        if title:
            entry["title"] = title
    _cap(data)
    _write(project, data)


def current_session(project: Path) -> str | None:
    """The session id of the chat open now, for linking new tasks to it."""
    return load(project).get("current")


def _when(entry: dict) -> float:
    stamp = entry.get("last_active") or entry.get("started") or ""
    try:
        return datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:
        return 0.0


def list_sessions(project: Path, limit: int | None = None) -> list[dict]:
    """Every known session, newest first, with the tasks it created."""
    from . import tasks  # imported here to avoid an import cycle (tasks.create links back)

    data = load(project)
    current = data.get("current")
    links: dict[str, list[str]] = {}
    for task in tasks.all_tasks(project):
        session_id = task.load().get("chat_session")
        if session_id:
            links.setdefault(session_id, []).append(task.id)
    out = []
    for entry in data["sessions"]:
        session_id = entry["id"]
        transcript = entry.get("transcript_path")
        available = bool(transcript and Path(transcript).is_file())
        title = entry.get("title") or ""
        if not title and available:
            # Recorded before titles existed: the conversation itself knows its first prompt.
            title = title_from_transcript(transcript)
        out.append({
            "id": session_id,
            "short": session_id[:8],
            "title": title,
            "started": entry.get("started"),
            "last_active": entry.get("last_active"),
            "transcript_path": transcript,
            "available": available,
            "current": session_id == current,
            "tasks": links.get(session_id, []),
        })
    out.sort(key=_when, reverse=True)
    return out[:limit] if limit is not None else out


def find(project: Path, ref: str) -> dict:
    """One session by full id or by an id prefix of at least PREFIX_MIN characters."""
    from . import tasks

    ref = (ref or "").strip()
    known = list_sessions(project)
    exact = [s for s in known if s["id"] == ref]
    if exact:
        match = exact[0]
    elif len(ref) < PREFIX_MIN:
        raise tasks.FlowError(f"no session matches '{ref}'; see aaagent sessions")
    else:
        matches = [s for s in known if s["id"].startswith(ref)]
        if not matches:
            raise tasks.FlowError(f"no session matches '{ref}'; see aaagent sessions")
        if len(matches) > 1:
            raise tasks.FlowError(
                f"'{ref}' matches several sessions: {', '.join(s['short'] for s in matches)}")
        match = matches[0]
    if not match["available"]:
        raise tasks.FlowError(
            f"session {match['short']} can no longer be reopened: its conversation file is gone")
    return match
