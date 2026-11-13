"""`aaagent ui`: the terminal dashboard (workers, tasks, live worker output).

Runs in the pane next to the chat, or on its own in any terminal. The screen is drawn with
curses; what it shows comes from status.py, and `handle_key` holds the key logic without
touching the terminal, so both can be tested without one.
"""

from __future__ import annotations

import curses
import dataclasses
import locale
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import dispatch, gitutil, session, status, tasks, worker
from .config import Config
from .status import Row
from .tasks import FlowError

FRAME_MS = 250          # redraw (spinner, worker output) this often
BUSY_SECONDS, IDLE_SECONDS = 1.0, 3.0
SPINNER = "|/-\\"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TAIL_BYTES = 16384

HELP = [
    ("↑ ↓  j k", "move"),
    ("enter", "task: show or hide its files · file: read it · worker: show its output"),
    ("d", "everything the selected task changed (diff)"),
    ("l", "switch whose output is shown below"),
    ("a", "abandon the selected task (its changes stay in the working tree)"),
    ("R", "reopen the previous chat in the chat pane"),
    ("r", "refresh now"),
    ("q", "close this view; from the list, quit the dashboard"),
    ("Ctrl-b o", "tmux: move between the chat and this dashboard (or click)"),
]

KEY_ENTER = (curses.KEY_ENTER, 10, 13)
KEY_BACK = (27, curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, ord("q"), ord("h"))


@dataclass
class View:
    """A scrollable text shown instead of the lists: a task file, a diff, or the help."""
    title: str
    lines: list[str]
    kind: str = "text"       # text | diff
    scroll: int = 0


@dataclass
class Confirm:
    prompt: str
    action: tuple


@dataclass
class UI:
    selected: str | None = None                     # id of the highlighted row
    expanded: set[str] = field(default_factory=set)  # task ids whose files are listed
    output_worker: str | None = None                # None: follow whichever worker is running
    view: View | None = None
    confirm: Confirm | None = None
    message: str = ""
    page: int = 10                                  # lines one page scrolls; set while drawing


def selectable(rows: list[Row]) -> list[Row]:
    return [r for r in rows if r.selectable]


def current(ui: UI, rows: list[Row]) -> Row | None:
    """The highlighted row. Falls back to the first unfinished task, then the first row."""
    choices = selectable(rows)
    for row in choices:
        if row.id == ui.selected:
            return row
    pick = next((r for r in choices if r.kind == "task" and not status.is_final(r.ref)), None)
    pick = pick or next((r for r in choices if r.kind == "task"), None) or (choices[0] if choices else None)
    ui.selected = pick.id if pick else None
    return pick


def _move(ui: UI, rows: list[Row], delta: int) -> None:
    choices = selectable(rows)
    row = current(ui, rows)
    if not row:
        return
    index = max(0, min(len(choices) - 1, choices.index(row) + delta))
    ui.selected = choices[index].id


def handle_key(ui: UI, key: int, rows: list[Row], worker_names: tuple[str, ...] = (),
               can_resume: bool = False) -> tuple | None:
    """Apply one key press to the UI state. Returns an action for the caller to carry out:
    ("quit",) ("refresh",) ("open", path, title) ("diff", task_id) ("abandon", task_id)
    ("resume",) ("help",), or None when the key only changed what is shown.
    """
    if key in (-1, curses.KEY_RESIZE):
        return None

    if ui.confirm:
        confirm, ui.confirm = ui.confirm, None
        if key in (ord("y"), ord("Y")):
            return confirm.action
        ui.message = "cancelled"
        return None

    if ui.view:
        view = ui.view
        if key in KEY_BACK:
            ui.view = None
        elif key in (curses.KEY_UP, ord("k")):
            view.scroll -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            view.scroll += 1
        elif key in (curses.KEY_PPAGE, ord("b")):
            view.scroll -= ui.page
        elif key in (curses.KEY_NPAGE, ord(" "), ord("f")):
            view.scroll += ui.page
        elif key in (curses.KEY_HOME, ord("g")):
            view.scroll = 0
        elif key in (curses.KEY_END, ord("G")):
            view.scroll = 10 ** 9          # clamped when drawn
        view.scroll = max(0, view.scroll)
        return None

    ui.message = ""
    row = current(ui, rows)
    task_id = row.task if row else None

    if key == ord("q"):
        return ("quit",)
    if key == ord("r"):
        return ("refresh",)
    if key == ord("?"):
        return ("help",)
    if key in (curses.KEY_UP, ord("k")):
        _move(ui, rows, -1)
    elif key in (curses.KEY_DOWN, ord("j")):
        _move(ui, rows, 1)
    elif key in (curses.KEY_HOME, ord("g")):
        _move(ui, rows, -len(rows))
    elif key in (curses.KEY_END, ord("G")):
        _move(ui, rows, len(rows))
    elif key in KEY_ENTER or key == curses.KEY_RIGHT:
        if not row:
            return None
        if row.kind == "task":
            if row.task in ui.expanded and key != curses.KEY_RIGHT:
                ui.expanded.discard(row.task)
            else:
                ui.expanded.add(row.task)
        elif row.kind == "file":
            return ("open", row.ref["path"], f"{row.task} / {row.text}")
        elif row.kind == "worker":
            ui.output_worker = row.text
            ui.message = f"showing {row.text} output"
    elif key == curses.KEY_LEFT:
        if row and row.kind == "file":
            ui.selected = f"task:{row.task}"
        elif row and row.kind == "task":
            ui.expanded.discard(row.task)
    elif key == ord("l"):
        order = [None, *worker_names]
        at = order.index(ui.output_worker) if ui.output_worker in order else 0
        ui.output_worker = order[(at + 1) % len(order)]
        ui.message = f"showing {ui.output_worker} output" if ui.output_worker else "following the running worker"
    elif key == ord("d"):
        if task_id:
            return ("diff", task_id)
        ui.message = "select a task to see its changes"
    elif key == ord("a"):
        task = _task_of(row, rows)
        if not task:
            ui.message = "select a task to abandon"
        elif status.is_final(task):
            ui.message = f"{status.task_label(task)} is already {task['status']}"
        else:
            ui.confirm = Confirm(f"Abandon {status.task_label(task)}? Its changes stay. (y/n)",
                                 ("abandon", task["id"]))
    elif key == ord("R"):
        if can_resume:
            ui.confirm = Confirm("Close this chat and reopen the previous one? (y/n)", ("resume",))
        else:
            ui.message = "no previous chat to reopen here (use `aaagent -c` when starting)"
    return None


def _task_of(row: Row | None, rows: list[Row]) -> dict | None:
    if not row or not row.task:
        return None
    if row.kind == "task":
        return row.ref
    return next((r.ref for r in rows if r.kind == "task" and r.task == row.task), None)


# -- reading things to show -------------------------------------------------------

def tail(path: Path, count: int) -> list[str]:
    """The last `count` lines of a log, without reading the whole file."""
    if count <= 0:
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [ANSI.sub("", line).rstrip() for line in chunk.splitlines()]
    if size > TAIL_BYTES and lines:
        lines = lines[1:]            # the first line is probably cut in half
    return lines[-count:]


def output_worker(ui: UI, data: dict | None, task: dict | None = None) -> str | None:
    """Whose output the lower pane shows: the chosen worker, else the one that is working,
    else the worker of the selected task, else whoever wrote to their log last."""
    workers = (data or {}).get("workers") or []
    names = [w["name"] for w in workers]
    if ui.output_worker in names:
        return ui.output_worker
    running = next((w["name"] for w in workers if w.get("state") == "running"), None)
    if running:
        return running
    if task and task.get("worker") in names:
        return task["worker"]

    def mtime(name: str) -> float:
        try:
            return Path(next(w["log"] for w in workers if w["name"] == name)).stat().st_mtime
        except OSError:
            return 0.0
    return max(names, key=mtime) if names else None


def snapshot(project: Path, width: int = 80) -> str:
    """The dashboard's lists as plain text (`aaagent ui --snapshot`)."""
    data = status.status_data(project, Config.load(project))
    expanded = {t["id"] for t in data["tasks"] if not status.is_final(t)}
    return "\n".join(status.format_row(r, width) for r in status.rows(data, expanded))


# -- the screen ---------------------------------------------------------------------

class Dashboard:
    def __init__(self, scr, project: Path):
        self.scr = scr
        self.project = project
        self.ui = UI()
        self.data: dict | None = None
        self.error = ""
        self.loaded_at = 0.0
        self.frame = 0
        self.first_load = True
        self.chat_pane = os.environ.get("AAAGENT_CHAT_PANE") or None
        self.styles: dict[str, int] = {}

    # -- setup and data -----------------------------------------------------------
    def setup(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        self.scr.timeout(FRAME_MS)
        self.scr.keypad(True)
        colors = {}
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            for n, color in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED,
                                       curses.COLOR_CYAN), start=1):
                curses.init_pair(n, color, background)
                colors[color] = curses.color_pair(n)
        green, yellow = colors.get(curses.COLOR_GREEN, 0), colors.get(curses.COLOR_YELLOW, 0)
        red, cyan = colors.get(curses.COLOR_RED, 0), colors.get(curses.COLOR_CYAN, 0)
        self.styles = {
            "normal": 0, "header": curses.A_BOLD | cyan, "running": curses.A_BOLD | yellow,
            "ok": green, "warn": curses.A_BOLD | red, "dim": curses.A_DIM,
            "add": green, "del": red, "hunk": cyan, "title": curses.A_BOLD,
        }

    def load(self) -> None:
        try:
            new = status.status_data(self.project, Config.load(self.project))
        except Exception as exc:  # noqa: BLE001 - keep the last good screen and say why
            self.error = f"cannot read status: {exc}"
            self.loaded_at = time.time()
            return
        self.error = ""
        events = status.transitions(self.data, new)
        if events:
            self.ui.message = events[-1]["message"]
            if any(e["level"] != "status" for e in events):
                curses.beep()
        self.data = new
        self.loaded_at = time.time()
        if self.first_load:
            self.first_load = False
            if new.get("active_task"):
                self.ui.expanded.add(new["active_task"])

    def can_resume(self) -> bool:
        return bool(self.chat_pane and session.previous_chat(self.project))

    # -- actions ------------------------------------------------------------------
    def act(self, action: tuple) -> bool:
        """Carry out an action from handle_key. Returns False to quit."""
        name = action[0]
        try:
            if name == "quit":
                return False
            if name == "refresh":
                self.load()
                self.ui.message = "refreshed"
            elif name == "help":
                self.ui.view = View("keys", [f"{keys:<10} {what}" for keys, what in HELP])
            elif name == "open":
                text = Path(action[1]).read_text(errors="replace")
                self.ui.view = View(action[2], text.expandtabs(4).splitlines() or ["(empty)"])
            elif name == "diff":
                task = tasks.find(self.project, action[1])
                text = gitutil.full_diff(self.project, task.load().get("base_commit"))
                lines = text.expandtabs(4).splitlines() or ["(no changes)"]
                self.ui.view = View(f"{task.id} / changes since the task started", lines, "diff")
            elif name == "abandon":
                self.ui.message = dispatch.abandon(self.project, action[1]).splitlines()[0]
                self.load()
            elif name == "resume":
                previous = session.previous_chat(self.project)
                if not previous or not self.chat_pane:
                    self.ui.message = "no previous chat to reopen"
                else:
                    session.respawn_chat(self.project, self.chat_pane, previous,
                                         os.environ.get("AAAGENT_TMUX_CLOSE") or "tmux kill-window")
                    self.ui.message = "reopened the previous chat"
        except (FlowError, OSError, RuntimeError) as exc:
            self.ui.message = str(exc)
        return True

    # -- drawing ------------------------------------------------------------------
    def put(self, y: int, x: int, text: str, attr: int = 0, width: int | None = None) -> None:
        height, total = self.scr.getmaxyx()
        if y < 0 or y >= height or x >= total:
            return
        room = total - x if width is None else min(width, total - x)
        try:
            self.scr.addstr(y, x, status.clip(text, room), attr)
        except curses.error:
            pass        # writing the bottom-right cell raises even though it worked

    def rule(self, y: int, label: str, width: int) -> None:
        text = f"── {label} " if label else ""
        self.put(y, 0, text + "─" * max(0, width - status.text_width(text)), self.styles["dim"])

    def draw(self) -> None:
        self.frame += 1
        self.scr.erase()
        height, width = self.scr.getmaxyx()
        if height < 6 or width < 24:
            self.put(0, 0, "aaagent: window too small")
            self.scr.refresh()
            return
        name = Path((self.data or {}).get("project") or self.project).name
        self.put(0, 0, f" aaagent · {name}", self.styles["title"])
        body_top, footer = 1, height - 1
        if self.ui.view:
            self.draw_view(body_top, footer - body_top, width)
        else:
            self.draw_lists(body_top, footer - body_top, width)
        self.draw_footer(footer, width)
        self.scr.refresh()

    def row_text(self, row: Row) -> Row:
        """Animate the marker of whatever is working."""
        if row.style != "running":
            return row
        spin = SPINNER[self.frame % len(SPINNER)]
        return dataclasses.replace(row, detail=f"{spin} {row.detail}")

    def draw_lists(self, top: int, height: int, width: int) -> None:
        rows = status.rows(self.data, self.ui.expanded)
        selected = current(self.ui, rows)
        if self.error and not rows:
            self.put(top, 1, self.error, self.styles["warn"])
            return
        # The lists take what they need, but leave the worker output at least a third.
        out_height = max(4, height - len(rows) - 1) if height >= 10 else 0
        out_height = min(out_height, height - 3) if out_height else 0
        if out_height and len(rows) > height - out_height - 1:
            out_height = max(4, height // 3)
        list_height = height - (out_height + 1 if out_height else 0)

        first = 0
        if selected and rows.index(selected) >= list_height:
            first = rows.index(selected) - list_height + 1
        for i, row in enumerate(rows[first:first + list_height]):
            attr = self.styles.get(row.style, 0)
            line = status.format_row(self.row_text(row), width - 1)
            if selected and row.id == selected.id:
                line += " " * max(0, width - 1 - status.text_width(line))
                attr = (attr & ~curses.A_DIM) | curses.A_REVERSE
            self.put(top + i, 0, line, attr)
        if first + list_height < len(rows):
            self.put(top + list_height - 1, max(0, width - 4), " ↓ ", self.styles["dim"])

        if not out_height:
            return
        y = top + list_height
        who = output_worker(self.ui, self.data, _task_of(selected, rows))
        follow = "" if self.ui.output_worker else " (following)"
        self.rule(y, f"{who} output{follow}" if who else "worker output", width)
        lines = tail(worker.log_file(self.project, who), out_height) if who else []
        if not lines:
            self.put(y + 1, 1, "nothing yet", self.styles["dim"])
        for i, line in enumerate(lines):
            attr = self.styles["warn"] if "✗" in line or "error" in line.lower()[:12] else 0
            self.put(y + 1 + i, 0, line, attr, width - 1)

    def draw_view(self, top: int, height: int, width: int) -> None:
        view = self.ui.view
        body = height - 1
        self.ui.page = max(1, body - 1)
        wrapped: list[tuple[str, int]] = []
        for line in view.lines:
            attr = self.line_style(view.kind, line)
            for piece in status.wrap(line, width - 1):
                wrapped.append((piece, attr))
        view.scroll = max(0, min(view.scroll, max(0, len(wrapped) - body)))
        end = min(len(wrapped), view.scroll + body)
        where = f"{end}/{len(wrapped)}"
        self.rule(top, f"{view.title}  {where}", width)
        for i, (piece, attr) in enumerate(wrapped[view.scroll:end]):
            self.put(top + 1 + i, 0, piece, attr)

    def line_style(self, kind: str, line: str) -> int:
        if kind == "diff":
            if line.startswith(("+++", "---", "diff ", "index ")):
                return self.styles["title"]
            if line.startswith("+"):
                return self.styles["add"]
            if line.startswith("-"):
                return self.styles["del"]
            if line.startswith("@@"):
                return self.styles["hunk"]
            return 0
        return self.styles["title"] if line.startswith("#") else 0

    def draw_footer(self, y: int, width: int) -> None:
        if self.ui.confirm:
            self.put(y, 0, " " + self.ui.confirm.prompt, self.styles["warn"])
            return
        if self.ui.view:
            hints = "↑↓ scroll  space page  q back"
        else:
            hints = "enter open  d diff  l output  a abandon"
            if self.can_resume():
                hints += "  R prev chat"
            hints += "  ? keys"
        text = self.error or self.ui.message
        if text:
            attr = self.styles["warn"] if self.error else self.styles["running"]
            self.put(y, 0, " " + text, attr)
            used = status.text_width(text) + 3
            if width - used > status.text_width(hints):
                self.put(y, width - status.text_width(hints) - 1, hints, self.styles["dim"])
        else:
            self.put(y, 0, " " + hints, self.styles["dim"])

    # -- main loop ----------------------------------------------------------------
    def loop(self) -> int:
        self.setup()
        self.load()
        while True:
            interval = BUSY_SECONDS if status.is_busy(self.data) else IDLE_SECONDS
            if time.time() - self.loaded_at >= interval:
                self.load()
            self.draw()
            try:
                key = self.scr.getch()
            except curses.error:
                key = -1
            if key == -1:
                continue
            rows = status.rows(self.data, self.ui.expanded)
            names = tuple(w["name"] for w in (self.data or {}).get("workers") or [])
            action = handle_key(self.ui, key, rows, names, self.can_resume() if key == ord("R") else False)
            if action and not self.act(action):
                return 0


def run(project: Path) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("aaagent ui needs a terminal. For plain text use `aaagent status`.", file=sys.stderr)
        return 1
    locale.setlocale(locale.LC_ALL, "")
    try:
        return curses.wrapper(lambda scr: Dashboard(scr, project).loop())
    except KeyboardInterrupt:
        return 0
