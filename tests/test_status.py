"""The dashboard's view logic (status.py) and key handling (tui.handle_key), without a terminal."""

import curses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import status, tui  # noqa: E402


def task(**over):
    base = {"id": "004-health", "number": 4, "title": "health", "status": "planning", "round": 0,
            "worker": None, "verdict": None, "plan_written": True, "job": None, "next": None,
            "blocked": None, "attention": None, "commit": None, "files": []}
    base.update(over)
    return base


FILES = [{"name": "plan.md", "path": "/p/plan.md", "kind": "plan"},
         {"name": "report-1.md", "path": "/p/report-1.md", "kind": "report", "status": "done"},
         {"name": "review-1.md", "path": "/p/review-1.md", "kind": "review", "verdict": "changes_requested"}]


def data(tasks=None, workers=None):
    return {"project": "/p", "active_task": None,
            "workers": workers if workers is not None else [
                {"name": "deepseek", "state": "idle", "queued": 0, "job": None, "log": "/none"},
                {"name": "glm", "state": "stopped", "queued": 0, "job": None, "log": "/none"}],
            "tasks": tasks or []}


class SummaryTest(unittest.TestCase):
    """Same cases as vscode/test/model.test.js, so both UIs say the same thing."""

    def test_visible_tasks(self):
        ts = [task(id="001-a", number=1, status="committed"), task(id="002-b", number=2, status="committed"),
              task(id="003-c", number=3, status="abandoned"), task(id="004-d", number=4, status="awaiting_review")]
        self.assertEqual([t["number"] for t in status.visible_tasks(ts, 1)], [4, 3])
        self.assertEqual([t["number"] for t in status.visible_tasks(ts)], [4, 3, 2, 1])

    def test_task_summary(self):
        s = status.task_summary
        self.assertEqual(s(task(plan_written=False)), "planning")
        self.assertEqual(s(task(next={"phase": "implement"})), "plan ready")
        self.assertEqual(s(task(status="running", job={"phase": "implement", "worker": "deepseek"})),
                         "implementing · deepseek")
        self.assertEqual(s(task(status="awaiting_review", round=2)), "in review (round 2)")
        self.assertEqual(s(task(status="committed", commit="abcdef123456")), "committed abcdef1")
        self.assertEqual(s(task(status="failed", attention="failed")), "failed: needs you")
        self.assertEqual(s(task(status="x", round=1, next={"phase": "fix"})), "changes requested (round 1)")
        self.assertEqual(s(task(status="x", next={"phase": "commit"})), "approved, ready to commit")

    def test_worker_summary(self):
        w = {"name": "deepseek", "state": "running", "queued": 1,
             "job": {"task": "004-health", "phase": "fix", "round": 2}}
        self.assertEqual(status.worker_summary(w), "fixing 004 r2 · 1 queued")
        self.assertEqual(status.worker_summary({"state": "stopped", "queued": 0}), "stopped")
        self.assertEqual(status.worker_summary({"state": "idle", "queued": 0}), "waiting for a plan")

    def test_transitions(self):
        before = {"tasks": [task(status="running"), task(id="005-x", number=5, status="running")]}
        after = {"tasks": [task(status="committed", commit="1234567890"),
                           task(id="005-x", number=5, status="awaiting_review", round=1)]}
        self.assertEqual(status.transitions(None, after), [])
        ev = status.transitions(before, after)
        self.assertEqual([e["level"] for e in ev], ["info", "status"])
        self.assertIn("committed (1234567)", ev[0]["message"])
        failed = {"tasks": [task(status="failed", attention="failed"), after["tasks"][1]]}
        self.assertEqual(status.transitions(before, failed)[0]["level"], "warning")
        self.assertEqual(status.transitions(failed, failed), [])

    def test_is_busy(self):
        self.assertFalse(status.is_busy(data([task()])))
        self.assertTrue(status.is_busy(data([task(status="running")])))
        self.assertTrue(status.is_busy(data(workers=[{"name": "x", "state": "idle", "queued": 1}])))


class RowsTest(unittest.TestCase):
    def test_collapsed_and_expanded(self):
        d = data([task(status="awaiting_review", round=1, files=FILES)])
        rows = status.rows(d)
        self.assertEqual([r.kind for r in rows], ["header", "worker", "worker", "header", "task"])
        self.assertTrue(rows[-1].text.startswith("▸ 004 health"))
        rows = status.rows(d, {"004-health"})
        self.assertEqual([r.text for r in rows if r.kind == "file"], ["plan.md", "report-1.md", "review-1.md"])
        self.assertEqual(rows[-1].detail, "changes requested")
        self.assertTrue(all(r.task == "004-health" for r in rows if r.kind == "file"))

    def test_no_tasks(self):
        self.assertEqual(status.rows(data())[-1].kind, "note")
        self.assertEqual(status.rows(None), [])


class TextTest(unittest.TestCase):
    def test_clip_counts_wide_characters(self):
        self.assertEqual(status.clip("hello", 10), "hello")
        self.assertEqual(status.clip("hello world", 6), "hello…")
        self.assertEqual(status.clip("한글제목입니다", 7), "한글제…")
        self.assertLessEqual(status.text_width(status.clip("한글제목입니다", 6)), 6)
        self.assertEqual(status.clip("abc", 0), "")

    def test_wrap(self):
        self.assertEqual(status.wrap("aaa bbb ccc", 7), ["aaa bbb", "ccc"])
        self.assertEqual(status.wrap("abcdefghij", 4), ["abcd", "efgh", "ij"])
        self.assertTrue(all(status.text_width(p) <= 4 for p in status.wrap("한글한글한글", 4)))
        self.assertEqual(status.wrap("x", 0), [""])

    def test_format_row_puts_detail_on_the_right(self):
        row = status.Row("w", "worker", "deepseek", "waiting for a plan", indent=1)
        line = status.format_row(row, 40)
        self.assertEqual(status.text_width(line), 40)
        self.assertTrue(line.startswith(" deepseek") and line.endswith("waiting for a plan"))
        self.assertLessEqual(status.text_width(status.format_row(row, 12)), 12)


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.data = data([task(status="awaiting_review", round=1, files=FILES),
                          task(id="003-old", number=3, title="old", status="committed", commit="abc1234")])
        self.ui = tui.UI()

    def press(self, key, can_resume=False, has_chat_pane=False):
        rows = status.rows(self.data, self.ui.expanded)
        k = ord(key) if isinstance(key, str) else key
        return tui.handle_key(self.ui, k, rows, ("deepseek", "glm"), can_resume, has_chat_pane)

    def test_starts_on_the_open_task_and_moves(self):
        rows = status.rows(self.data)
        self.assertEqual(tui.current(self.ui, rows).id, "task:004-health")
        self.press("j")
        self.assertEqual(self.ui.selected, "task:003-old")
        self.press("j")
        self.assertEqual(self.ui.selected, "task:003-old", "stops at the end")
        self.press("g")
        self.assertEqual(self.ui.selected, "worker:deepseek")

    def test_enter_expands_then_opens_a_file(self):
        self.press(10)
        self.assertIn("004-health", self.ui.expanded)
        self.press("j")
        self.assertEqual(self.ui.selected, "file:004-health:plan.md")
        self.assertEqual(self.press(10), ("open", "/p/plan.md", "004-health / plan.md"))
        self.press(curses.KEY_LEFT)
        self.assertEqual(self.ui.selected, "task:004-health")
        self.press(10)
        self.assertNotIn("004-health", self.ui.expanded)

    def test_diff_works_from_a_file_row(self):
        self.press(10)
        self.press("j")
        self.assertEqual(self.press("d"), ("diff", "004-health"))

    def test_abandon_asks_first(self):
        self.assertIsNone(self.press("a"))
        self.assertIsNotNone(self.ui.confirm)
        self.assertIsNone(self.press("n"))
        self.assertIsNone(self.ui.confirm)
        self.assertEqual(self.ui.message, "cancelled")
        self.press("a")
        self.assertEqual(self.press("y"), ("abandon", "004-health"))

    def test_finished_task_cannot_be_abandoned(self):
        self.press("j")
        self.assertIsNone(self.press("a"))
        self.assertIsNone(self.ui.confirm)
        self.assertIn("already committed", self.ui.message)

    def test_output_worker_cycles(self):
        self.press("l")
        self.assertEqual(self.ui.output_worker, "deepseek")
        self.press("l")
        self.press("l")
        self.assertIsNone(self.ui.output_worker)
        self.ui.selected = "worker:glm"
        self.press(10)
        self.assertEqual(self.ui.output_worker, "glm")

    def test_view_scrolls_and_closes(self):
        self.ui.view = tui.View("t", ["x"] * 50)
        self.ui.page = 10
        self.press(" ")
        self.press("j")
        self.assertEqual(self.ui.view.scroll, 11)
        self.press("k")
        self.press("k")
        self.assertEqual(self.ui.view.scroll, 9)
        self.assertIsNone(self.press("q"), "q closes the view, it does not quit")
        self.assertIsNone(self.ui.view)
        self.assertEqual(self.press("q"), ("quit",))

    def test_resume_only_when_possible(self):
        self.assertIsNone(self.press("R"))
        self.assertIn("no previous chat", self.ui.message)
        self.press("R", can_resume=True)
        self.assertEqual(self.press("y"), ("resume",))

    def test_new_chat_needs_the_tmux_layout(self):
        self.assertIsNone(self.press("N"))
        self.assertIsNone(self.ui.confirm)
        self.assertIn("tmux layout", self.ui.message)

        self.press("N", has_chat_pane=True)
        self.assertEqual(self.ui.confirm.action, ("new_chat",))
        self.assertIsNone(self.press("n"))
        self.assertIsNone(self.ui.confirm)
        self.assertEqual(self.ui.message, "cancelled")

        self.press("N", has_chat_pane=True)
        self.assertEqual(self.press("y"), ("new_chat",))

    def test_output_follows_the_running_worker(self):
        d = data(workers=[{"name": "deepseek", "state": "idle", "log": "/none"},
                          {"name": "glm", "state": "running", "log": "/none"}])
        self.assertEqual(tui.output_worker(self.ui, d), "glm")
        self.ui.output_worker = "deepseek"
        self.assertEqual(tui.output_worker(self.ui, d), "deepseek")
        idle = data(workers=[{"name": "deepseek", "state": "idle", "log": "/none"},
                             {"name": "glm", "state": "idle", "log": "/none"}])
        self.assertEqual(tui.output_worker(tui.UI(), idle, task(worker="glm")), "glm")


class TailTest(unittest.TestCase):
    def test_tail_strips_colours_and_keeps_the_end(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "w.log"
            log.write_text("".join(f"\033[36mline {i}\033[0m\n" for i in range(5000)))
            self.assertEqual(tui.tail(log, 2), ["line 4998", "line 4999"])
            self.assertEqual(tui.tail(Path(d) / "missing", 3), [])


if __name__ == "__main__":
    unittest.main()
