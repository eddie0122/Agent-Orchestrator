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

from . import dispatch, gitutil, models, session, status, tasks, worker
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
    ("enter", "task: show or hide its files · session: show its tasks · file: read it · worker: show its output"),
    ("", "on a session's task: jump to it in TASKS"),
    ("d", "everything the selected task changed (diff)"),
    ("l", "switch whose output is shown below"),
    ("m", "change a model: the selected worker's or planner's, or pick which"),
    ("t", "change a thinking level (same)"),
    ("a", "abandon the selected task (its changes stay in the working tree)"),
    ("o", "open the selected session in the chat pane"),
    ("N", "start a new chat session"),
    ("R", "restore an earlier session (pick from a list)"),
    ("r", "refresh now"),
    ("q", "close this view; from the list, quit the dashboard"),
    ("Ctrl-b o", "tmux: move between the chat and this dashboard (or click)"),
]

KEY_ENTER = (curses.KEY_ENTER, 10, 13)
KEY_BACK = (27, curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, ord("q"), ord("h"))
TYPE_NEW = "\x00type"    # picker value of "type a name…"


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
class Picker:
    """A list of choices shown instead of the lists (a model, a thinking level, where to save)."""
    title: str
    items: list[tuple[str, str, str]]   # label, value, detail
    index: int = 0
    action: tuple = ()                  # selecting an item returns (*action, value)


@dataclass
class Prompt:
    """A line of text typed by the user; Enter returns (*action, text)."""
    title: str
    text: str = ""
    action: tuple = ()


@dataclass
class UI:
    selected: str | None = None                     # id of the highlighted row
    expanded: set[str] = field(default_factory=set)  # task ids whose files are listed
    output_worker: str | None = None                # None: follow whichever worker is running
    view: View | None = None
    confirm: Confirm | None = None
    picker: Picker | None = None
    prompt: Prompt | None = None
    message: str = ""
    page: int = 10                                  # lines one page scrolls; set while drawing


def typed_char(key: int) -> str | None:
    """The character a key press types, or None for arrows and other special keys."""
    if 32 <= key <= 126:
        return chr(key)
    if 127 < key < getattr(curses, "KEY_MIN", 257):
        ch = chr(key)
        return ch if ch.isprintable() else None
    return None


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
               has_chat_pane: bool = False) -> tuple | None:
    """Apply one key press to the UI state. Returns an action for the caller to carry out:
    ("quit",) ("refresh",) ("open", path, title) ("diff", task_id) ("abandon", task_id)
    ("restore",) ("open_session", session_id) ("new_chat",) ("help",) ("model", target)
    ("thinking", target), or None when the key only changed what is shown.
    """
    if key in (-1, curses.KEY_RESIZE):
        return None

    if ui.confirm:
        confirm, ui.confirm = ui.confirm, None
        if key in (ord("y"), ord("Y")):
            return confirm.action
        ui.message = "cancelled"
        return None

    if ui.prompt:
        prompt = ui.prompt
        if key in KEY_ENTER:
            text = prompt.text.strip()
            if not text:
                return None
            ui.prompt = None
            return (*prompt.action, text)
        if key == 27:
            ui.prompt = None
            ui.message = "cancelled"
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            prompt.text = prompt.text[:-1]
        else:
            char = typed_char(key)
            if char:
                prompt.text += char
        return None

    if ui.picker:
        picker = ui.picker
        last = max(0, len(picker.items) - 1)
        if key in (curses.KEY_UP, ord("k")):
            picker.index = max(0, picker.index - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            picker.index = min(last, picker.index + 1)
        elif key == curses.KEY_HOME:
            picker.index = 0
        elif key == curses.KEY_END:
            picker.index = last
        elif key in KEY_ENTER:
            value = picker.items[picker.index][1]
            ui.picker = None
            return (*picker.action, value)
        elif key in (ord("q"), 27, curses.KEY_LEFT, ord("h")):
            ui.picker = None
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
    if key == ord("m"):
        return ("model", _target_of(row))
    if key == ord("t"):
        return ("thinking", _target_of(row))
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
        elif row.kind == "session":
            key_id = f"session:{row.ref['id']}"
            if not row.ref.get("tasks"):
                ui.message = "no tasks in this session yet"
            elif key_id in ui.expanded and key != curses.KEY_RIGHT:
                ui.expanded.discard(key_id)
            else:
                ui.expanded.add(key_id)
        elif row.kind == "stask":
            ui.selected = f"task:{row.task}"
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
        elif row and row.kind == "session":
            ui.expanded.discard(f"session:{row.ref['id']}")
        elif row and row.kind == "stask":
            ui.selected = f"session:{row.id.split(':')[1]}"
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
    elif key == ord("o"):
        if not row or row.kind != "session":
            ui.message = "select a session to open"
        elif not has_chat_pane:
            ui.message = f"open it with: aaagent --session {row.ref['short']}"
        elif not row.ref.get("available", True):
            ui.message = "this session's conversation is gone"
        elif row.ref.get("current"):
            ui.message = "this session is already the chat"
        else:
            label = status.clip(status.session_label(row.ref), 40)
            ui.confirm = Confirm(f'Open session "{label}" in the chat pane? '
                                 "The current chat closes. (y/n)",
                                 ("open_session", row.ref["id"]))
    elif key == ord("N"):
        if has_chat_pane:
            ui.confirm = Confirm("Close this chat and start a new session? (y/n)", ("new_chat",))
        else:
            ui.message = ("a new session works in aaagent's tmux layout; otherwise quit the chat "
                          "and run `aaagent` again")
    elif key == ord("R"):
        if has_chat_pane:
            return ("restore",)
        ui.message = ("restore works in aaagent's tmux layout; otherwise use `aaagent sessions` "
                      "and `aaagent --session <id>`")
    return None


def _target_of(row: Row | None) -> str | None:
    """Whose model or thinking level a picker should change, from the selected row."""
    if row and row.kind == "planner":
        return models.PLANNER
    if row and row.kind == "worker":
        return row.text
    return None


def _task_of(row: Row | None, rows: list[Row]) -> dict | None:
    if not row or not row.task:
        return None
    if row.kind in ("task", "stask"):
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
            elif name == "new_chat":
                if not self.chat_pane:
                    self.ui.message = "new chat works in aaagent's tmux layout"
                else:
                    session.respawn_chat(self.project, self.chat_pane, None,
                                         os.environ.get("AAAGENT_TMUX_CLOSE") or "tmux kill-window")
                    self.ui.message = "started a new chat"
            elif name == "restore":
                earlier = [s for s in (self.data or {}).get("sessions") or []
                           if s.get("available") and not s.get("current")]
                if not earlier:
                    self.ui.message = "no earlier session to restore"
                else:
                    items = [(status.clip(status.session_label(s), 60), s["id"],
                              status.session_summary(s)) for s in earlier]
                    self.ui.picker = Picker("restore which session?", items, action=("open_session",))
            elif name == "open_session":
                if not self.chat_pane:
                    self.ui.message = "open a session works in aaagent's tmux layout"
                else:
                    session.respawn_chat(self.project, self.chat_pane, action[1],
                                         os.environ.get("AAAGENT_TMUX_CLOSE") or "tmux kill-window")
                    self.ui.message = f"opened session {action[1][:8]}"
                    self.load()
            elif name in ("model", "thinking"):
                if action[1]:
                    self.pick_values(name, action[1])
                else:
                    self.pick_targets(name)
            elif name == "model_pick":
                target, value = action[1], action[2]
                if value == TYPE_NEW:
                    self.ui.picker = None
                    self.ui.prompt = Prompt(f"model for {target}: ", action=("model_pick", target))
                else:
                    self.pick_scope(("model_save", target, value))
            elif name == "thinking_pick":
                self.pick_scope(("thinking_save", action[1], action[2]))
            elif name == "model_save":
                target, model, scope = action[1], action[2], action[3]
                notes = models.set_model(self.project, scope == "project", target, model)
                self.load()
                self.ui.message = "; ".join(notes)
            elif name == "thinking_save":
                target, level, scope = action[1], action[2], action[3]
                notes = models.set_thinking(self.project, scope == "project", target, level)
                self.load()
                self.ui.message = "; ".join(notes)
        except (FlowError, OSError, RuntimeError, ValueError) as exc:
            self.ui.message = str(exc)
        return True

    # -- model and thinking pickers -----------------------------------------------
    def pick_targets(self, kind: str) -> None:
        """Whose model or thinking level to change: the planner, or one of the workers."""
        cfg = Config.load(self.project)
        items = []
        for target in [models.PLANNER, *cfg.worker_names()]:
            if kind == "model":
                detail = models.current(cfg, target)
            else:
                detail = models.thinking_current(cfg, target) or "default"
            items.append((target, target, detail))
        title = "change the model of" if kind == "model" else "change the thinking level of"
        self.ui.picker = Picker(title, items, action=(kind,))

    def pick_values(self, kind: str, target: str) -> None:
        """The choices for `target`: models (with typing) or thinking levels."""
        self.ui.message = "loading models…" if kind == "model" else "loading thinking levels…"
        self.draw()
        cfg = Config.load(self.project)
        if kind == "model":
            now = models.current(cfg, target)
            names = models.choices(cfg, target)
            if now in names:
                names = [now, *(n for n in names if n != now)]
            items = [(name, name, "current" if name == now else "") for name in names]
            items.append(("type a name…", TYPE_NEW, ""))
            self.ui.picker = Picker(f"model for {target}", items, action=("model_pick", target))
        else:
            now = models.thinking_current(cfg, target)
            items = [("default", "default", "the model's own setting")]
            items += [(level, level, "current" if level == now else "")
                      for level in models.thinking_choices(cfg, target)]
            self.ui.picker = Picker(f"thinking for {target}", items, action=("thinking_pick", target))

    def pick_scope(self, action: tuple) -> None:
        """Where to save the choice: the user config (all projects) or this project's."""
        items = [("all projects", "user", "~/.config/aaagent/config.toml"),
                 ("this project only", "project", ".aaagent/config.toml")]
        self.ui.picker = Picker("save where?", items, action=action)

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
        if self.ui.picker:
            self.draw_picker(body_top, footer - body_top, width)
        elif self.ui.view:
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

    def draw_picker(self, top: int, height: int, width: int) -> None:
        """The choices of a model/thinking picker, in place of the lists."""
        picker = self.ui.picker
        body = height - 1
        self.rule(top, picker.title, width)
        if body <= 0:
            return
        first = 0
        if picker.index >= body:
            first = picker.index - body + 1
        for i, (label, _value, detail) in enumerate(picker.items[first:first + body]):
            line = status.format_row(Row(f"pick:{first + i}", "note", label, detail, indent=1), width - 1)
            attr = 0
            if first + i == picker.index:
                line += " " * max(0, width - 1 - status.text_width(line))
                attr = curses.A_REVERSE
            self.put(top + 1 + i, 0, line, attr)
        if first + body < len(picker.items):
            self.put(top + body, max(0, width - 4), " ↓ ", self.styles["dim"])

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
        if self.ui.prompt:
            prompt = self.ui.prompt
            text = " " + prompt.title + prompt.text + "_"
            self.put(y, 0, text, self.styles["title"])
            hints = "enter save  esc cancel"
            if width - status.text_width(text) - 2 > status.text_width(hints):
                self.put(y, width - status.text_width(hints) - 1, hints, self.styles["dim"])
            return
        if self.ui.picker:
            self.put(y, 0, " ↑↓ choose  enter select  q cancel", self.styles["dim"])
            return
        if self.ui.confirm:
            self.put(y, 0, " " + self.ui.confirm.prompt, self.styles["warn"])
            return
        if self.ui.view:
            hints = "↑↓ scroll  space page  q back"
        else:
            hints = "enter open  d diff  m model  t thinking  a abandon"
            if self.chat_pane:
                row = self.selected_row()
                if row and row.kind == "session":
                    hints += "  o open session"
                hints += "  N new session  R restore"
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

    def selected_row(self) -> Row | None:
        """The highlighted row, as the lists build them."""
        return next((r for r in status.rows(self.data, self.ui.expanded)
                     if r.id == self.ui.selected), None)

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
            action = handle_key(self.ui, key, rows, names, bool(self.chat_pane))
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
