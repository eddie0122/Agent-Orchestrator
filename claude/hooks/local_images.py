#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: bring files from the user's computer into the project.

With VSCode Remote-SSH the chat runs on the server while the file the user dropped sits on
their own machine. The prompt then holds a path such as `/Users/hanul/Downloads/band.jpg` or
`C:\\Users\\hanul\\Downloads\\refresh-pages.js` that does not exist here. This hook finds such
paths, asks the aaagent VSCode extension (through `.aaagent/fetch/`) to copy them into
`.aaagent/attachments/`, and tells Claude where the copy is. Without the extension it tells
Claude that the file is on the user's computer and how the user can attach it instead.

It never blocks the chat: every failure is ignored and it exits 0.
"""

import json
import math
import os
import re
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp")
IMAGE_END = re.compile(r"\.(?:" + "|".join(EXTENSIONS) + r")$", re.IGNORECASE)
# Claude Code kills this hook after 30 s (`lib/aaagent/session.py`), so every fetch of one
# run shares one deadline well below that, even when `AAAGENT_FETCH_TIMEOUT` asks for more.
FETCH_TIMEOUT_MAX = 25.0
# Any file with an extension is worth copying; a bare directory (`/usr/bin`) is not.
FILE_END = re.compile(r"\.[A-Za-z0-9]{1,10}$")
# A Windows drive path: `c:/x` or `C:\\x`. Its backslashes are separators, not escapes.
DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")
# An unquoted path from a terminal drop: `/a b.png` arrives as `/a\ b.png`; the escapes are
# part of the token, so a whitespace that follows a backslash does not end it. Only ASCII
# whitespace ends a token: macOS screenshot names contain U+202F, which `\s` would match.
TOKEN = re.compile(r"(?<![^ \t\r\n])(?:~/|file://|[A-Za-z]:[\\/]|/)(?:\\.|[^ \t\r\n\\])*")


def _is_drive(text: str) -> bool:
    return bool(DRIVE.match(text))


def _has_extension(path: str) -> bool:
    """Whether the basename ends in `.` and 1-10 alphanumerics."""
    base = re.split(r"[\\/]", path)[-1]
    return bool(FILE_END.search(base))


def _clean(text: str) -> str:
    """A `file://` URL becomes the plain path; a drive path uses forward slashes."""
    if text.startswith("file://"):
        path = urllib.parse.unquote(text[len("file://"):])
    else:
        path = text
    if re.match(r"^/[A-Za-z]:[\\/]", path):
        path = path[1:]
    if _is_drive(path):
        path = path.replace("\\", "/")
    return path


def _candidate(text: str) -> bool:
    path = _clean(text)
    if not path or not _has_extension(path):
        return False
    return path.startswith(("/", "~/")) or _is_drive(path)


def _unescape(text: str) -> str:
    """`\\ ` -> ` ` and any other `\\x` -> `x`, as a shell-less drop writes them."""
    return re.sub(r"\\(.)", r"\1", text)


def _token_path(token: str) -> str:
    """The path an unquoted token names; a drive path keeps its separators as they are."""
    cleaned = _clean(token)
    if _is_drive(cleaned):
        return cleaned
    return _clean(_unescape(token))


def find_paths(prompt: str, project: Path | None = None) -> list[str]:
    """File paths mentioned in `prompt`, in order and deduplicated.

    Paths that already exist on this machine are left to Claude Code, and so is anything
    inside the project: only files that are somewhere else can need copying.
    """
    found: list[tuple[int, str]] = []
    masked = prompt
    for match in QUOTED.finditer(prompt):
        content = match.group(1) if match.group(1) is not None else match.group(2)
        if _candidate(content):
            found.append((match.start(), _clean(content)))
        # Blank the quoted text so its path is not found a second time as a bare token.
        masked = masked[:match.start()] + " " * (match.end() - match.start()) + masked[match.end():]
    for match in TOKEN.finditer(masked):
        path = _token_path(match.group(0))
        if _candidate(path):
            found.append((match.start(), path))

    out: list[str] = []
    for _, path in sorted(found, key=lambda item: item[0]):
        if path in out:
            continue
        if _is_drive(path):
            # A Windows path always belongs to the user's computer: `Path("c:/x").resolve()`
            # is relative to the cwd (= the project), so both checks would be wrong.
            out.append(path)
            continue
        expanded = Path(os.path.expanduser(path))
        try:
            if expanded.is_file():
                continue
        except OSError:
            pass
        if project is not None and _inside(expanded, project):
            continue
        out.append(path)
    return out


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (OSError, ValueError):
        return False


def _write_json(path: Path, value: dict) -> None:
    """Atomic, so the extension never reads a half-written file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value))
    os.replace(tmp, path)


def _fetch_timeout() -> float:
    """How long all fetches of one hook run may take.

    `AAAGENT_FETCH_TIMEOUT` (default 15 s, a bad value falls back to it), clamped to
    `FETCH_TIMEOUT_MAX` so the hook always answers before Claude Code's 30 s limit
    (`lib/aaagent/session.py`) kills it and drops its output.
    """
    try:
        timeout = float(os.environ.get("AAAGENT_FETCH_TIMEOUT", "15"))
    except ValueError:
        timeout = 15.0
    if not math.isfinite(timeout):
        timeout = 15.0
    return max(0.0, min(timeout, FETCH_TIMEOUT_MAX))


def _no_answer() -> dict:
    return {"ok": False, "error": "the VSCode extension did not answer"}


def _fetch_all(project: Path, paths: list[str]) -> list[dict]:
    """Ask the VSCode extension to copy each of `paths`; all of them share one deadline.

    The extension handles each request file on its own, so all requests are written at once
    and polled together. Answers come back in the order of `paths`, with the no-answer dict
    for the ones that stay unanswered.
    """
    folder = project / ".aaagent" / "fetch"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        return [{"ok": False, "error": str(err)} for _ in paths]
    answers: list[dict | None] = [None] * len(paths)
    written: list[tuple[Path, Path]] = []
    pending: dict[int, Path] = {}
    for index, path in enumerate(paths):
        name = uuid.uuid4().hex
        request = folder / f"{name}.request.json"
        result = folder / f"{name}.result.json"
        try:
            _write_json(request, {"path": path, "created": time.time()})
        except OSError as err:
            answers[index] = {"ok": False, "error": str(err)}
            continue
        written.append((request, result))
        pending[index] = result
    try:
        deadline = time.time() + _fetch_timeout()
        while pending:
            for index, result in list(pending.items()):
                try:
                    answer = json.loads(result.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                answers[index] = answer if isinstance(answer, dict) else _no_answer()
                del pending[index]
            if not pending or time.time() >= deadline:
                break
            time.sleep(0.1)
    finally:
        # Never leave request files behind, also for the paths still unanswered.
        for request, result in written:
            for leftover in (request, result):
                try:
                    leftover.unlink()
                except OSError:
                    pass
    return [answer if answer is not None else _no_answer() for answer in answers]


def _success_text(path: str, rel: str, project: Path) -> str:
    return (f"aaagent: the user's file {path} is on their own computer, not on this server. "
            f"It was copied to {rel} (absolute: {project / rel}). "
            f"Read that file with the Read tool to see it; treat it as the file the "
            f"user attached.")


def _failure_text(path: str, error: str, image: bool) -> str:
    if image:
        return (f"aaagent: the user's message mentions the image {path}, which is on their own "
                f"computer, not on this server, so you cannot open it ({error}). Ask the user to "
                f"attach it instead: in VSCode use \"aaagent: Attach Image…\" (paste, Shift+drop or "
                f"Choose files), or copy it to the server (scp) and run `aaagent attach <file>`.")
    return (f"aaagent: the user's message mentions the file {path}, which is on their own "
            f"computer, not on this server, so you cannot open it ({error}). Ask the user to "
            f"copy the file to the server (scp) or paste its contents into the chat.")


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return 0
    if not isinstance(event, dict):
        return 0
    prompt = event.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return 0
    project = Path(os.environ.get("AAAGENT_PROJECT") or event.get("cwd") or os.getcwd())
    paths = find_paths(prompt, project)
    if not paths:
        return 0

    bridge = os.environ.get("AAAGENT_VSCODE") == "1"
    answers: list[dict | None] = _fetch_all(project, paths) if bridge else [None] * len(paths)
    paragraphs = []
    for path, answer in zip(paths, answers):
        image = bool(IMAGE_END.search(path))
        if answer is None:
            # Without the bridge only images and Windows paths are worth mentioning.
            if not (image or _is_drive(path)):
                continue
            answer = {"ok": False, "error": "not on this server"}
        elif answer.get("missing"):
            # The path is not a file on the user's computer either: nothing to report.
            continue
        if answer.get("ok") and answer.get("rel"):
            paragraphs.append(_success_text(path, str(answer["rel"]), project))
        else:
            paragraphs.append(_failure_text(path, str(answer.get("error") or "unknown error"), image))
    if not paragraphs:
        return 0
    output = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                     "additionalContext": "\n\n".join(paragraphs)}}
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a prompt hook must never break the chat
        sys.exit(0)
