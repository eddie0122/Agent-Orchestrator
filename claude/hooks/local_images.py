#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook: bring images from the user's computer into the project.

With VSCode Remote-SSH the chat runs on the server while the image the user dropped sits on
their own machine. The prompt then holds a path such as `/Users/hanul/Downloads/band.jpg` that
does not exist here. This hook finds such paths, asks the aaagent VSCode extension (through
`.aaagent/fetch/`) to copy them into `.aaagent/attachments/`, and tells Claude where the copy
is. Without the extension it tells Claude that the image is on the user's computer and how the
user can attach it instead.

It never blocks the chat: every failure is ignored and it exits 0.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp")
IMAGE_END = re.compile(r"\.(?:" + "|".join(EXTENSIONS) + r")$", re.IGNORECASE)
QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")
# An unquoted path from a terminal drop: `/a b.png` arrives as `/a\ b.png`; the escapes are
# part of the token, so a whitespace that follows a backslash does not end it. Only ASCII
# whitespace ends a token: macOS screenshot names contain U+202F, which `\s` would match.
TOKEN = re.compile(r"(?<![^ \t\r\n])(?:~/|file://|/)(?:\\.|[^ \t\r\n\\])*")


def _candidate(text: str) -> bool:
    return bool(text) and text.startswith(("/", "~/", "file://")) and bool(IMAGE_END.search(text))


def _clean(text: str) -> str:
    """A `file://` URL becomes the plain path, percent-decoded."""
    if text.startswith("file://"):
        return urllib.parse.unquote(text[len("file://"):])
    return text


def _unescape(text: str) -> str:
    """`\\ ` -> ` ` and any other `\\x` -> `x`, as a shell-less drop writes them."""
    return re.sub(r"\\(.)", r"\1", text)


def find_paths(prompt: str, project: Path | None = None) -> list[str]:
    """Image paths mentioned in `prompt`, in order and deduplicated.

    Paths that already exist on this machine are left to Claude Code, and so is anything
    inside the project: only images that are somewhere else can need copying.
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
        token = _unescape(match.group(0))
        if _candidate(token):
            found.append((match.start(), _clean(token)))

    out: list[str] = []
    for _, path in sorted(found, key=lambda item: item[0]):
        if path in out:
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


def _fetch_from_extension(project: Path, path: str) -> dict:
    """Ask the VSCode extension to copy the user's image; wait for its answer."""
    folder = project / ".aaagent" / "fetch"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        return {"ok": False, "error": str(err)}
    name = uuid.uuid4().hex
    request = folder / f"{name}.request.json"
    result = folder / f"{name}.result.json"
    try:
        _write_json(request, {"path": path, "created": time.time()})
    except OSError as err:
        return {"ok": False, "error": str(err)}
    try:
        timeout = float(os.environ.get("AAAGENT_FETCH_TIMEOUT", "15"))
    except ValueError:
        timeout = 15.0
    deadline = time.time() + timeout
    answer = None
    while True:
        try:
            answer = json.loads(result.read_text())
            break
        except (OSError, json.JSONDecodeError):
            pass
        if time.time() >= deadline:
            break
        time.sleep(0.1)
    for leftover in (request, result):
        try:
            leftover.unlink()
        except OSError:
            pass
    if not isinstance(answer, dict):
        return {"ok": False, "error": "the VSCode extension did not answer"}
    return answer


def _success_text(path: str, rel: str, project: Path) -> str:
    return (f"aaagent: the user's image {path} is on their own computer, not on this server. "
            f"It was copied to {rel} (absolute: {project / rel}). "
            f"Read that file with the Read tool to see the image; treat it as the image the "
            f"user attached.")


def _failure_text(path: str, error: str) -> str:
    return (f"aaagent: the user's message mentions the image {path}, which is on their own "
            f"computer, not on this server, so you cannot open it ({error}). Ask the user to "
            f"attach it instead: in VSCode use \"aaagent: Attach Image…\" (paste, Shift+drop or "
            f"Choose files), or copy it to the server (scp) and run `aaagent attach <file>`.")


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
    paragraphs = []
    for path in paths:
        answer = (_fetch_from_extension(project, path) if bridge
                  else {"ok": False, "error": "not on this server"})
        if answer.get("ok") and answer.get("rel"):
            paragraphs.append(_success_text(path, str(answer["rel"]), project))
        else:
            paragraphs.append(_failure_text(path, str(answer.get("error") or "unknown error")))
    output = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                     "additionalContext": "\n\n".join(paragraphs)}}
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a prompt hook must never break the chat
        sys.exit(0)
