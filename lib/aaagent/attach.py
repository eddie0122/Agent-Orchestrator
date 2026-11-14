"""`aaagent attach`: copy images into `.aaagent/attachments/` and add them to the chat.

Claude Code reads an image when the message mentions it as `@path`. The chat may run on a
server over SSH, so a clipboard image has to reach the server first: `aaagent attach` copies
the file into the project and types its `@`-mention into the tmux chat pane when there is one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .tasks import FlowError

IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "gif": "image/gif", "webp": "image/webp"}
MAX_BYTES = 5 * 1024 * 1024


def attachments_dir(project: Path) -> Path:
    return project / ".aaagent" / "attachments"


def sniff(data: bytes) -> str | None:
    """The image type of `data`, from its magic bytes. None when it is not an image."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def safe_name(name: str, kind: str, now: time.struct_time | None = None) -> str:
    """`<YYYYmmdd-HHMMSS>-<stem>.<ext>`, safe for any filesystem and easy to read."""
    stamp = time.strftime("%Y%m%d-%H%M%S", now or time.localtime())
    original = Path(name)
    stem = re.sub(r"[^A-Za-z0-9._-]", "-", original.stem)
    stem = re.sub(r"-+", "-", stem).strip("-")[:40].rstrip("-") or "image"
    ext = original.suffix.lstrip(".").lower()
    return f"{stamp}-{stem}.{ext if ext in IMAGE_TYPES else kind}"


def _validate(data: bytes, name: str) -> str:
    """The image type of `data`, or FlowError when the file cannot be attached."""
    if not data:
        raise FlowError(f"{name} is empty")
    if len(data) > MAX_BYTES:
        raise FlowError(f"{name} is {len(data) / (1024 * 1024):.1f} MB; "
                        f"the limit is {MAX_BYTES // (1024 * 1024)} MB")
    kind = sniff(data)
    if kind is None:
        raise FlowError(f"{name} is not a PNG, JPEG, GIF or WebP image")
    return kind


def save(project: Path, data: bytes, name: str) -> Path:
    """Validate `data` and write it into the attachments folder."""
    kind = _validate(data, name)
    folder = attachments_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    base = safe_name(name, kind)
    target = folder / base
    count = 1
    while target.exists():
        target = folder / f"{Path(base).stem}-{count}{Path(base).suffix}"
        count += 1
    target.write_bytes(data)
    return target


def mention(project: Path, path: Path) -> str:
    """The `@`-mention Claude Code needs, relative to the project and with a trailing space."""
    rel = Path(os.path.relpath(path, project)).as_posix()
    return f'@"{rel}" ' if " " in rel else f"@{rel} "


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def attach_files(project: Path, sources: list[str], stdin_bytes: bytes | None = None,
                 name: str | None = None) -> list[Path]:
    """Copy every source into the attachments folder. `-` reads `stdin_bytes`."""
    folder = attachments_dir(project)
    saved = []
    for source in sources:
        if source == "-":
            saved.append(save(project, stdin_bytes or b"", name or "pasted.png"))
            continue
        path = Path(source)
        if not path.is_file():
            raise FlowError(f"{source}: no such file")
        data = path.read_bytes()
        if _inside(path, folder):
            _validate(data, path.name)
            saved.append(path)
        else:
            saved.append(save(project, data, path.name))
    return saved


def chat_pane_file(project: Path) -> Path:
    """Where `aaagent` records the tmux pane of the chat (only in the tmux layout)."""
    return project / ".aaagent" / "chat-pane"


def type_into_chat(project: Path, text: str) -> bool:
    """Type `text` into the running chat's input, literally and without Enter.

    False when there is no chat pane to type into, or tmux fails; never raises.
    """
    pane_file = chat_pane_file(project)
    if not shutil.which("tmux") or not pane_file.is_file():
        return False
    try:
        pane = pane_file.read_text().strip()
        if not pane:
            return False
        shown = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{pane_id}"],
                               capture_output=True, text=True)
        if shown.returncode != 0 or shown.stdout.strip() != pane:
            return False
        sent = subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text],
                              capture_output=True, text=True)
        return sent.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
