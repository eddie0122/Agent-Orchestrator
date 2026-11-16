"""The dashboard's view logic (status.py) and key handling (tui.handle_key), without a terminal."""

import curses
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import branching, models, session, status, tui  # noqa: E402
from aaagent.config import Config  # noqa: E402


def task(**over):
    base = {"id": "004-health", "number": 4, "title": "health", "status": "planning", "round": 0,
            "worker": None, "verdict": None, "plan_written": True, "job": None, "next": None,
            "blocked": None, "attention": None, "commit": None, "files": []}
    base.update(over)
    return base


def session_(**over):
    base = {"id": "sess-current", "short": "sess-cur", "title": "add a /health endpoint",
            "started": "2026-11-13T10:00:00+0900", "last_active": "2026-11-13T21:43:50+0900",
            "transcript_path": "/t/current.jsonl", "available": True, "current": False, "tasks": []}
    base.update(over)
    return base


def recently(**kw):
    """A timestamp a while ago, as sessions.py writes them."""
    when = datetime.now(timezone.utc) - timedelta(**kw)
    return when.strftime("%Y-%m-%dT%H:%M:%S%z")


FILES = [{"name": "plan.md", "path": "/p/plan.md", "kind": "plan"},
         {"name": "report-1.md", "path": "/p/report-1.md", "kind": "report", "status": "done"},
         {"name": "review-1.md", "path": "/p/review-1.md", "kind": "review", "verdict": "changes_requested"}]


def worker(name, state, model="opencode-go/deepseek-v4.1-flash", thinking="", **over):
    base = {"name": name, "state": state, "queued": 0, "job": None, "log": "/none",
            "model": model, "thinking": thinking}
    base.update(over)
    return base


def review_planner():
    """The `review` entry of status_data while the planner (the chat) reviews."""
    return {"planner_reviews": True, "model": "planner", "thinking": "", "backend": None,
            "pid": None, "state": "stopped", "job": None, "queued": 0, "log": "/none"}


def review_model(state="stopped", **over):
    """The `review` entry of status_data while a model reviews each round."""
    base = {"planner_reviews": False, "model": "opencode-go/glm-5.3-flash",
            "thinking": "", "backend": "opencode", "pid": None, "state": state,
            "job": None, "queued": 0, "log": "/none"}
    base.update(over)
    return base


def data(tasks=None, workers=None, sessions=None, reviewer=None, review=None):
    if review is None:
        review = review_planner() if reviewer in (None, "orchestrator") else review_model()
    if reviewer is None:
        reviewer = "orchestrator" if review.get("planner_reviews", True) else "reviewer"
    return {"project": "/p", "active_task": None,
            "planner": {"model": "opus", "thinking": ""},
            "reviewer": reviewer, "review": review,
            "workers": workers if workers is not None else [
                worker("worker1", "idle"),
                worker("worker2", "stopped", model="opencode-go/glm-5.3-flash")],
            "tasks": tasks or [], "sessions": sessions or []}


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
        self.assertEqual(s(task(status="running", job={"phase": "implement", "worker": "worker1"})),
                         "implementing · worker1")
        self.assertEqual(s(task(status="awaiting_review", round=2)), "in review (round 2)")
        self.assertEqual(s(task(status="committed", commit="abcdef123456")), "committed abcdef1")
        self.assertEqual(s(task(status="failed", attention="failed")), "failed: needs you")
        self.assertEqual(s(task(status="x", round=1, next={"phase": "fix"})), "changes requested (round 1)")
        self.assertEqual(s(task(status="x", next={"phase": "commit"})), "approved, ready to commit")

    def test_worker_summary(self):
        w = {"name": "worker1", "state": "running", "queued": 1,
             "job": {"task": "004-health", "phase": "fix", "round": 2}}
        self.assertEqual(status.worker_summary(w), "fixing 004 r2 · 1 queued")
        self.assertEqual(status.worker_summary({"state": "stopped", "queued": 0}), "stopped")
        self.assertEqual(status.worker_summary({"state": "idle", "queued": 0}), "waiting for a plan")

    def test_worker_summary_shows_the_short_model_and_thinking(self):
        w = worker("worker1", "idle", thinking="high",
                   model="opencode-go/deepseek-v4.1-flash")
        self.assertEqual(status.worker_summary(w), "waiting for a plan · deepseek-v4.1-flash · high")
        w = worker("worker1", "stopped", thinking="")
        self.assertEqual(status.worker_summary(w), "stopped · deepseek-v4.1-flash")

    def test_planner_summary(self):
        self.assertEqual(status.planner_summary({"model": "opus", "thinking": "high"}), "opus · high")
        self.assertEqual(status.planner_summary({"model": "opus", "thinking": ""}), "opus")

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

    def test_transitions_say_the_planner_and_stay_quiet_while_a_worker_reviews(self):
        before = {"tasks": [task(status="running")]}
        after = {"tasks": [task(status="awaiting_review", round=2)]}
        ev = status.transitions(before, after)
        self.assertIn("the planner is reviewing", ev[0]["message"])
        self.assertNotIn("Opus", ev[0]["message"])

        before_glm = {"reviewer": "worker2", "tasks": [task(status="running")]}
        after_glm = {"reviewer": "worker2", "tasks": [task(status="awaiting_review", round=2)]}
        self.assertEqual(status.transitions(before_glm, after_glm), [])

        approved = {"reviewer": "worker2", "tasks": [task(status="approved", round=2)]}
        ev = status.transitions(before_glm, approved)
        self.assertEqual(ev[0]["level"], "status")
        self.assertEqual(ev[0]["message"],
                         "004 health: approved by the reviewer in round 2; the planner commits next")

        warning = status.transitions(before, {"tasks": [task(status="failed", attention="failed")]})
        self.assertIn("the planner is waiting for you in the chat", warning[0]["message"])


class SessionSummaryTest(unittest.TestCase):
    NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)

    def iso(self, **kw):
        return (self.NOW - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%S%z")

    def test_ago(self):
        now = self.NOW.timestamp()
        self.assertEqual(status.ago(self.iso(seconds=30), now), "just now")
        self.assertEqual(status.ago(self.iso(minutes=5), now), "5 min ago")
        self.assertEqual(status.ago(self.iso(hours=3), now), "3 h ago")
        self.assertEqual(status.ago(self.iso(days=1), now), "1 day ago")
        self.assertEqual(status.ago(self.iso(days=2), now), "2 days ago")
        self.assertEqual(status.ago(self.iso(days=13), now), "13 days ago")
        self.assertEqual(status.ago(self.iso(days=30), now), "2026-10-14")
        self.assertEqual(status.ago("not a date", now), "")
        self.assertEqual(status.ago(None, now), "")
        self.assertEqual(status.ago("", now), "")

    def test_session_label(self):
        self.assertEqual(status.session_label(session_(title="fix login")), "fix login")
        self.assertEqual(status.session_label(session_(title="")), "(no messages yet)")

    def test_session_summary(self):
        two = session_(last_active=recently(hours=2), tasks=["001-a", "002-b"])
        self.assertEqual(status.session_summary(two), "2 h ago · 2 tasks")
        self.assertEqual(status.session_summary(session_(last_active=recently(hours=2),
                                                         tasks=["001-a"])), "2 h ago · 1 task")
        self.assertEqual(status.session_summary(two, True), "open · 2 tasks")
        self.assertEqual(status.session_summary(session_(last_active=recently(hours=2),
                                                         tasks=["001-a"], available=False)),
                         "2 h ago · 1 task · gone")
        self.assertEqual(status.session_summary(session_(last_active=None)), "")


class RowsTest(unittest.TestCase):
    def test_collapsed_and_expanded(self):
        d = data([task(status="awaiting_review", round=1, files=FILES)])
        rows = status.rows(d)
        self.assertEqual([r.kind for r in rows],
                         ["header", "planner", "header", "reviewer", "header", "worker", "worker",
                          "add_worker", "header", "note", "header", "task"])
        self.assertEqual(rows[8].text, "SESSIONS")
        self.assertTrue(rows[-1].text.startswith("▸ 004 health"))
        rows = status.rows(d, {"004-health"})
        self.assertEqual([r.text for r in rows if r.kind == "file"], ["plan.md", "report-1.md", "review-1.md"])
        self.assertEqual(rows[-1].detail, "changes requested")
        self.assertTrue(all(r.task == "004-health" for r in rows if r.kind == "file"))

    def test_three_sections_before_sessions(self):
        rows = status.rows(data())
        self.assertEqual([r.id for r in rows if r.kind == "header"],
                         ["header:planner", "header:reviewer", "header:workers",
                          "header:sessions", "header:tasks"])
        self.assertEqual([r.text for r in rows if r.kind == "header"],
                         ["PLANNER", "REVIEWER", "WORKERS", "SESSIONS", "TASKS"])

    def test_sessions_section(self):
        d = data([task(status="awaiting_review", round=1)], sessions=[
            session_(current=True, tasks=["004-health"], last_active=recently(minutes=5)),
            session_(id="sess-old", short="sess-old", title="fix login", available=False,
                     last_active=recently(hours=3))])
        rows = status.rows(d)
        at = next(i for i, r in enumerate(rows) if r.id == "header:sessions")
        self.assertEqual(rows[at].text, "SESSIONS")
        current, old = rows[at + 1], rows[at + 2]
        self.assertEqual(current.id, "session:sess-current")
        self.assertEqual(current.text, "▸ add a /health endpoint")
        self.assertEqual(current.detail, "open · 1 task")
        self.assertEqual(current.style, "ok")
        self.assertTrue(current.selectable)
        self.assertEqual(old.text, "· fix login")
        self.assertEqual(old.detail, "3 h ago · gone")
        self.assertEqual(old.style, "dim")
        self.assertEqual(rows[at + 3].text, "TASKS")

    def test_a_session_shows_its_tasks_when_expanded(self):
        d = data([task(status="awaiting_review", round=1, files=FILES)], sessions=[
            session_(current=True, tasks=["004-health", "999-unknown"])])
        rows = status.rows(d, {"session:sess-current"})
        session_row = next(r for r in rows if r.id == "session:sess-current")
        self.assertEqual(session_row.text, "▾ add a /health endpoint")
        stasks = [r for r in rows if r.kind == "stask"]
        self.assertEqual(len(stasks), 1, "unknown task ids are skipped")
        self.assertEqual(stasks[0].id, "stask:sess-current:004-health")
        self.assertEqual(stasks[0].text, "004 health")
        self.assertEqual(stasks[0].detail, "in review (round 1)")
        self.assertEqual(stasks[0].indent, 3)
        self.assertEqual(stasks[0].task, "004-health")
        self.assertTrue(stasks[0].selectable)

    def test_the_current_session_is_kept_and_more_sessions_are_counted(self):
        sessions = [session_(id=f"s{i}", short=f"s{i}", current=(i == 6)) for i in range(7)]
        rows = status.rows(data(sessions=sessions), keep_sessions=5)
        shown = [r.id for r in rows if r.kind == "session"]
        self.assertEqual(shown, [f"session:s{i}" for i in range(5)] + ["session:s6"])
        note = next(r for r in rows if r.id == "note:sessions-more")
        self.assertEqual(note.text, "… 1 more: aaagent sessions")
        self.assertEqual(note.style, "dim")

    def test_sessions_note_when_there_are_none(self):
        note = next(r for r in status.rows(data()) if r.id == "note:sessions")
        self.assertEqual(note.text, "none yet: start the chat with aaagent")
        self.assertEqual(note.style, "dim")
        self.assertFalse(note.selectable)

    def test_planner_row(self):
        d = data()
        rows = status.rows(d)
        planner = next(r for r in rows if r.id == "planner")
        self.assertEqual(planner.kind, "planner")
        self.assertEqual(planner.text, "planner")
        self.assertTrue(planner.selectable)
        self.assertEqual(planner.detail, status.planner_summary(d["planner"]))
        self.assertEqual(planner.detail, "opus")

    def test_reviewer_row_while_the_planner_reviews(self):
        row = next(r for r in status.rows(data()) if r.id == "reviewer")
        self.assertEqual(row.kind, "reviewer")
        self.assertEqual(row.text, "reviewer")
        self.assertEqual(row.detail, "planner (the chat)")
        self.assertEqual(row.indent, 1)
        self.assertTrue(row.selectable)
        self.assertTrue(row.ref["planner_reviews"])
        self.assertEqual(row.ref["reviewer"], "orchestrator")

    def test_reviewer_row_with_a_model_reviewer(self):
        d = data(reviewer="reviewer")
        row = next(r for r in status.rows(d) if r.id == "reviewer")
        self.assertEqual(row.detail, "stopped · glm-5.3-flash")
        self.assertFalse(row.ref["planner_reviews"])
        self.assertEqual(row.ref["reviewer"], "reviewer")

    def test_reviewer_row_while_reviewing_a_job(self):
        d = data(reviewer="reviewer", review=review_model(
            state="running", thinking="high",
            job={"task": "016-x", "phase": "review", "round": 2, "created": 0}))
        row = next(r for r in status.rows(d) if r.id == "reviewer")
        self.assertTrue(row.detail.startswith("reviewing 016 r2"))
        self.assertIn("glm-5.3-flash", row.detail)
        self.assertIn("high", row.detail)
        self.assertEqual(row.style, "running")

    def test_reviewer_row_without_a_review_entry(self):
        d = data()
        del d["review"]
        d["reviewer"] = "worker2"
        self.assertEqual(status.reviewer_summary(d), "worker2 · glm-5.3-flash",
                         "old status data without a review entry still works")
        d["reviewer"] = "missing"
        self.assertEqual(status.reviewer_summary(d), "missing")

    def test_add_worker_row_is_the_last_worker_row(self):
        rows = status.rows(data())
        add = next(r for r in rows if r.id == "add_worker")
        self.assertEqual(add.kind, "add_worker")
        self.assertEqual(add.text, "+ add worker")
        self.assertEqual(add.style, "dim")
        self.assertEqual(add.indent, 1)
        self.assertTrue(add.selectable)
        self.assertEqual([r.kind for r in rows if r.kind in ("worker", "add_worker")],
                         ["worker", "worker", "add_worker"])

    def test_worker_rows_mark_the_default_and_the_fallback(self):
        d = data(workers=[worker("worker1", "idle", default=True),
                          worker("worker2", "stopped", model="opencode-go/glm-5.3-flash",
                                 fallback=True)])
        by_id = {r.id: r for r in status.rows(d)}
        self.assertTrue(by_id["worker:worker1"].detail.endswith(" · default"))
        self.assertTrue(by_id["worker:worker2"].detail.endswith(" · fallback"))

    def test_no_tasks(self):
        self.assertEqual(status.rows(data())[-1].kind, "note")
        self.assertEqual(status.rows(None), [])


class StatusDataTest(unittest.TestCase):
    def project(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_workers_carry_their_default_and_fallback_flags(self):
        cfg = Config({"workers": {"worker1": {"model": "sonnet"},
                                  "worker2": {"model": "opus"},
                                  "worker3": {"model": "haiku"}},
                      "flow": {"default_worker": "worker2", "fallback_worker": "worker3"},
                      "reviewer": {"model": "planner"}})
        workers = status.status_data(self.project(), cfg)["workers"]
        self.assertEqual([w["name"] for w in workers], ["worker1", "worker2", "worker3"])
        self.assertEqual([w["default"] for w in workers], [False, True, False])
        self.assertEqual([w["fallback"] for w in workers], [False, False, True])

    def test_the_only_worker_is_the_default_but_not_the_fallback(self):
        cfg = Config({"workers": {"worker1": {"model": "sonnet"}},
                      "reviewer": {"model": "planner"}})
        worker_entry = status.status_data(self.project(), cfg)["workers"][0]
        self.assertTrue(worker_entry["default"])
        self.assertFalse(worker_entry["fallback"])


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
        row = status.Row("w", "worker", "worker1", "waiting for a plan", indent=1)
        line = status.format_row(row, 40)
        self.assertEqual(status.text_width(line), 40)
        self.assertTrue(line.startswith(" worker1") and line.endswith("waiting for a plan"))
        self.assertLessEqual(status.text_width(status.format_row(row, 12)), 12)


class GitSummaryTest(unittest.TestCase):
    """The git half of the dashboard's top line."""

    def summary(self, **git):
        base = {"mode": "current", "branch": "main", "batch": None, "last_pr": None}
        base.update(git)
        return tui.git_summary({"git": base})

    def test_each_mode(self):
        self.assertEqual(self.summary(), "on main")
        self.assertEqual(self.summary(mode="branch"), "new branch · on main")
        self.assertEqual(self.summary(mode="pr"), "branch + PR · on main")

    def test_no_git_mode(self):
        self.assertEqual(self.summary(mode="none", branch=None), "no git")
        self.assertEqual(self.summary(mode="none", branch=None, last_pr={"error": "gh not found"}),
                         "no git")

    def test_an_idle_open_branch_waits_for_the_user(self):
        batch = {"branch": "feat/x", "base": "main", "tasks": ["001-a"], "commits": 2, "idle": True}
        self.assertEqual(self.summary(mode="pr", branch="feat/x", batch=batch),
                         "branch + PR · on feat/x (from main) · 2 commit(s), waiting: PR?")
        self.assertEqual(self.summary(mode="branch", branch="feat/x", batch=batch),
                         "new branch · on feat/x (from main) · 2 commit(s), waiting: finish?")
        self.assertEqual(self.summary(mode="current", branch="feat/x", batch=batch),
                         "on feat/x (from main) · 2 commit(s), waiting: finish?")

    def test_a_busy_branch_or_one_without_commits_does_not_wait(self):
        busy = {"branch": "feat/x", "base": "main", "commits": 2, "idle": False}
        self.assertEqual(self.summary(mode="pr", branch="feat/x", batch=busy),
                         "branch + PR · on feat/x (from main)")
        fresh = {"branch": "feat/x", "base": "main", "commits": 0, "idle": True}
        self.assertEqual(self.summary(mode="pr", branch="feat/x", batch=fresh),
                         "branch + PR · on feat/x (from main)")

    def test_without_a_branch(self):
        self.assertEqual(self.summary(branch=None), "")
        self.assertEqual(self.summary(mode="branch", branch=None), "new branch")
        self.assertEqual(self.summary(mode="pr", branch=None), "branch + PR")

    def test_an_open_batch_shows_where_it_started(self):
        batch = {"branch": "feat/x", "base": "main", "tasks": ["001-a"]}
        self.assertEqual(self.summary(mode="pr", branch="feat/x", batch=batch),
                         "branch + PR · on feat/x (from main)")

    def test_a_failed_pull_request_points_at_the_command(self):
        last_pr = {"branch": "feat/x", "url": None, "error": "gh not found"}
        self.assertEqual(self.summary(last_pr=last_pr), "on main · PR failed: aaagent pr")
        batch = {"branch": "feat/x", "base": "main", "tasks": ["001-a"]}
        self.assertEqual(self.summary(mode="branch", branch="feat/x", batch=batch, last_pr=last_pr),
                         "new branch · on feat/x (from main)",
                         "an open batch is not a PR failure")

    def test_no_git_data(self):
        self.assertEqual(tui.git_summary(None), "")
        self.assertEqual(tui.git_summary({}), "")
        self.assertEqual(tui.git_summary({"git": None}), "")


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.data = data([task(status="awaiting_review", round=1, files=FILES),
                          task(id="003-old", number=3, title="old", status="committed", commit="abc1234")])
        self.ui = tui.UI()

    def press(self, key, has_chat_pane=False, reviewer_job=False):
        rows = status.rows(self.data, self.ui.expanded)
        k = ord(key) if isinstance(key, str) else key
        return tui.handle_key(self.ui, k, rows, ("worker1", "worker2"), has_chat_pane, reviewer_job)

    def test_starts_on_the_open_task_and_moves(self):
        rows = status.rows(self.data)
        self.assertEqual(tui.current(self.ui, rows).id, "task:004-health")
        self.press("j")
        self.assertEqual(self.ui.selected, "task:003-old")
        self.press("j")
        self.assertEqual(self.ui.selected, "task:003-old", "stops at the end")
        self.press("g")
        self.assertEqual(self.ui.selected, "planner")

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
        self.assertEqual(self.ui.output_worker, "worker1")
        self.press("l")
        self.press("l")
        self.assertIsNone(self.ui.output_worker)
        self.ui.selected = "worker:worker2"
        self.press(10)
        self.assertEqual(self.ui.output_worker, "worker2")

    def test_output_worker_cycles_through_the_reviewer_only_when_it_has_a_job(self):
        for expected in ("worker1", "worker2", "reviewer", None):
            self.press("l", reviewer_job=True)
            self.assertEqual(self.ui.output_worker, expected)
        self.press("l")
        self.assertEqual(self.ui.output_worker, "worker1",
                         "the planner reviews, so the cycle has no reviewer")

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

    def test_restore_needs_the_tmux_layout(self):
        self.assertIsNone(self.press("R"))
        self.assertIn("tmux layout", self.ui.message)
        self.assertIn("aaagent sessions", self.ui.message)
        self.assertEqual(self.press("R", has_chat_pane=True), ("restore",))

    def test_new_session_needs_the_tmux_layout(self):
        self.assertIsNone(self.press("N"))
        self.assertIsNone(self.ui.confirm)
        self.assertIn("tmux layout", self.ui.message)

        self.press("N", has_chat_pane=True)
        self.assertEqual(self.ui.confirm.action, ("new_chat",))
        self.assertIn("start a new session", self.ui.confirm.prompt)
        self.assertIsNone(self.press("n"))
        self.assertIsNone(self.ui.confirm)
        self.assertEqual(self.ui.message, "cancelled")

        self.press("N", has_chat_pane=True)
        self.assertEqual(self.press("y"), ("new_chat",))

    def test_output_follows_the_running_worker(self):
        d = data(workers=[{"name": "worker1", "state": "idle", "log": "/none"},
                          {"name": "worker2", "state": "running", "log": "/none"}])
        self.assertEqual(tui.output_worker(self.ui, d), "worker2")
        self.ui.output_worker = "worker1"
        self.assertEqual(tui.output_worker(self.ui, d), "worker1")
        idle = data(workers=[{"name": "worker1", "state": "idle", "log": "/none"},
                             {"name": "worker2", "state": "idle", "log": "/none"}])
        self.assertEqual(tui.output_worker(tui.UI(), idle, task(worker="worker2")), "worker2")

    def test_output_follows_the_running_reviewer(self):
        d = data(reviewer="reviewer", review=review_model(
            state="running", job={"task": "016-x", "phase": "review", "round": 2, "created": 0}))
        self.assertEqual(tui.output_worker(self.ui, d), "reviewer")
        self.ui.output_worker = "worker1"
        self.assertEqual(tui.output_worker(self.ui, d), "worker1")

        planner_reviews = tui.UI()
        planner_reviews.output_worker = "reviewer"
        self.assertEqual(tui.output_worker(planner_reviews, data()), "worker1",
                         "while the planner reviews there is no reviewer output")

    def test_model_and_thinking_keys_use_the_selected_row(self):
        self.ui.selected = "worker:worker1"
        self.assertEqual(self.press("m"), ("model", "worker1"))
        self.assertEqual(self.press("t"), ("thinking", "worker1"))
        self.ui.selected = "planner"
        self.assertEqual(self.press("m"), ("model", "planner"))
        self.assertEqual(self.press("t"), ("thinking", "planner"))
        self.ui.selected = "task:004-health"
        self.assertEqual(self.press("m"), ("model", None))
        self.assertEqual(self.press("t"), ("thinking", None))

    def test_the_reviewer_row_uses_its_own_model_and_thinking(self):
        self.ui.selected = "reviewer"
        self.assertEqual(self.press("m"), ("model", "reviewer"))
        self.assertIsNone(self.press("t"), "the planner reviews: it has no level of its own")
        self.assertIn("pick a reviewer model first", self.ui.message)

        self.data = data(reviewer="reviewer")
        self.assertEqual(self.press("t"), ("thinking", "reviewer"))

    def test_reviewer_key_and_row(self):
        self.assertEqual(self.press("v"), ("model", "reviewer"))
        self.ui.selected = "reviewer"
        self.assertEqual(self.press(10), ("model", "reviewer"),
                         "while the planner reviews, enter offers the reviewer's model")
        self.assertEqual(self.press(curses.KEY_RIGHT), ("model", "reviewer"))
        self.ui.selected = "planner"
        self.assertIsNone(self.press(10), "enter on the planner row still means nothing")
        self.assertEqual(self.press("v"), ("model", "reviewer"))

    def test_branch_mode_key(self):
        self.assertEqual(self.press("B"), ("branch_mode",))

    def test_enter_on_the_reviewer_row_shows_its_output(self):
        self.data = data(reviewer="reviewer", review=review_model(
            state="running", job={"task": "016-x", "phase": "review", "round": 2, "created": 0}))
        self.ui.selected = "reviewer"
        self.assertIsNone(self.press(10))
        self.assertEqual(self.ui.output_worker, "reviewer")
        self.assertEqual(self.ui.message, "showing reviewer output")

    def test_add_worker_key_and_row(self):
        self.ui.selected = "add_worker"
        self.assertEqual(self.press(10), ("add_worker",))
        self.ui.selected = "task:004-health"
        self.assertEqual(self.press("+"), ("add_worker",))

    def test_remove_worker_asks_first(self):
        self.ui.selected = "worker:worker2"
        self.assertIsNone(self.press("-"))
        self.assertIsNotNone(self.ui.confirm)
        self.assertIn("Remove worker2 (glm-5.3-flash)", self.ui.confirm.prompt)
        self.assertEqual(self.ui.confirm.action, ("remove_worker", "worker2"))
        self.assertIsNone(self.press("n"))
        self.assertIsNone(self.ui.confirm)

        self.ui.selected = "worker:worker2"
        self.assertIsNone(self.press(curses.KEY_DC))
        self.assertEqual(self.press("y"), ("remove_worker", "worker2"))

    def test_remove_key_on_another_row_asks_for_a_worker(self):
        self.ui.selected = "task:004-health"
        self.assertIsNone(self.press("-"))
        self.assertIsNone(self.ui.confirm)
        self.assertEqual(self.ui.message, "select a worker to remove")

    def test_remove_refuses_the_only_worker(self):
        self.ui.selected = "worker:worker1"
        rows = status.rows(self.data)
        self.assertIsNone(tui.handle_key(self.ui, ord("-"), rows, ("worker1",)))
        self.assertIsNone(self.ui.confirm)
        self.assertEqual(self.ui.message, "cannot remove the only worker")

    def test_picker_moves_and_returns_the_value(self):
        self.ui.picker = tui.Picker("pick", [("a", "1", ""), ("b", "2", ""), ("c", "3", "")],
                                    action=("chosen",))
        self.press(curses.KEY_DOWN)
        self.press("j")
        self.assertEqual(self.ui.picker.index, 2)
        self.press("j")
        self.assertEqual(self.ui.picker.index, 2, "stops at the end")
        self.press(curses.KEY_UP)
        self.assertEqual(self.ui.picker.index, 1)
        self.press(curses.KEY_HOME)
        self.assertEqual(self.ui.picker.index, 0)
        self.press(curses.KEY_END)
        self.assertEqual(self.ui.picker.index, 2)
        self.assertEqual(self.press(10), ("chosen", "3"))
        self.assertIsNone(self.ui.picker)

    def test_picker_cancels(self):
        for key in ("q", 27, curses.KEY_LEFT, "h"):
            self.ui.picker = tui.Picker("pick", [("a", "1", "")], action=("chosen",))
            self.assertIsNone(self.press(key))
            self.assertIsNone(self.ui.picker)
            self.assertEqual(self.ui.message, "cancelled")

    def test_prompt_collects_text(self):
        self.ui.prompt = tui.Prompt("model for planner: ", action=("model_pick", "planner"))
        for ch in "sonnet":
            self.assertIsNone(self.press(ch))
        self.assertEqual(self.ui.prompt.text, "sonnet")
        self.press(curses.KEY_BACKSPACE)
        self.assertEqual(self.ui.prompt.text, "sonne")
        self.assertEqual(self.press(10), ("model_pick", "planner", "sonne"))
        self.assertIsNone(self.ui.prompt)

    def test_prompt_ignores_empty_text_and_cancels_with_escape(self):
        self.ui.prompt = tui.Prompt("model: ", action=("model_pick", "planner"))
        self.assertIsNone(self.press(10))
        self.assertIsNotNone(self.ui.prompt)
        self.assertIsNone(self.press(27))
        self.assertIsNone(self.ui.prompt)
        self.assertEqual(self.ui.message, "cancelled")


class SessionKeysTest(unittest.TestCase):
    """Enter, o, d and a on the SESSIONS rows."""

    def setUp(self):
        self.data = data(
            [task(status="awaiting_review", round=1, files=FILES)],
            sessions=[session_(id="sess-current", short="sess-cur", current=True,
                               tasks=["004-health"], last_active=recently(minutes=5)),
                      session_(id="sess-old", short="sess-old", title="fix login", tasks=[],
                               last_active=recently(hours=2)),
                      session_(id="sess-gone", short="sess-gone", title="gone chat",
                               available=False, last_active=recently(hours=3))])
        self.ui = tui.UI()

    def press(self, key, has_chat_pane=False):
        rows = status.rows(self.data, self.ui.expanded)
        k = ord(key) if isinstance(key, str) else key
        return tui.handle_key(self.ui, k, rows, ("worker1", "worker2"), has_chat_pane)

    def test_enter_shows_a_sessions_tasks(self):
        self.ui.selected = "session:sess-current"
        self.assertIsNone(self.press(10))
        self.assertIn("session:sess-current", self.ui.expanded)
        self.press(curses.KEY_RIGHT)
        self.assertIn("session:sess-current", self.ui.expanded)
        self.assertIsNone(self.press(10))
        self.assertNotIn("session:sess-current", self.ui.expanded)
        self.press(10)
        self.press(curses.KEY_LEFT)
        self.assertNotIn("session:sess-current", self.ui.expanded)

    def test_enter_on_a_session_without_tasks_says_so(self):
        self.ui.selected = "session:sess-old"
        self.assertIsNone(self.press(10))
        self.assertEqual(self.ui.message, "no tasks in this session yet")
        self.assertEqual(self.ui.expanded, set())

    def test_enter_on_a_sessions_task_selects_it_in_tasks(self):
        self.ui.expanded.add("session:sess-current")
        self.ui.selected = "stask:sess-current:004-health"
        self.assertIsNone(self.press(10))
        self.assertEqual(self.ui.selected, "task:004-health")
        self.assertIn("004-health", self.ui.expanded)
        self.assertEqual(self.press("d"), ("diff", "004-health"))

    def test_left_on_a_sessions_task_goes_back_to_its_session(self):
        self.ui.expanded.add("session:sess-current")
        self.ui.selected = "stask:sess-current:004-health"
        self.press(curses.KEY_LEFT)
        self.assertEqual(self.ui.selected, "session:sess-current")

    def test_abandon_works_from_a_sessions_task(self):
        self.ui.expanded.add("session:sess-current")
        self.ui.selected = "stask:sess-current:004-health"
        self.press("a")
        self.assertEqual(self.ui.confirm.action, ("abandon", "004-health"))

    def test_open_session_explains_itself_outside_tmux(self):
        self.ui.selected = "session:sess-old"
        self.assertIsNone(self.press("o"))
        self.assertIsNone(self.ui.confirm)
        self.assertEqual(self.ui.message, "open it with: aaagent --session sess-old")

    def test_open_session_when_gone_or_already_open(self):
        self.ui.selected = "session:sess-gone"
        self.press("o", has_chat_pane=True)
        self.assertIsNone(self.ui.confirm)
        self.assertEqual(self.ui.message, "this session's conversation is gone")
        self.ui.selected = "session:sess-current"
        self.press("o", has_chat_pane=True)
        self.assertIsNone(self.ui.confirm)
        self.assertEqual(self.ui.message, "this session is already the chat")

    def test_open_session_asks_first(self):
        self.ui.selected = "session:sess-old"
        self.assertIsNone(self.press("o", has_chat_pane=True))
        self.assertIn('Open session "fix login"', self.ui.confirm.prompt)
        self.assertIn("current chat closes", self.ui.confirm.prompt)
        self.assertEqual(self.press("y"), ("open_session", "sess-old"))

    def test_o_on_another_row_asks_for_a_session(self):
        self.ui.selected = "task:004-health"
        self.assertIsNone(self.press("o"))
        self.assertEqual(self.ui.message, "select a session to open")

    def test_x_on_another_row_asks_for_a_session(self):
        self.ui.selected = "task:004-health"
        self.assertIsNone(self.press("x"))
        self.assertIsNone(self.ui.confirm)
        self.assertEqual(self.ui.message, "select a session to delete")

    def test_x_on_the_current_session_suggests_force(self):
        self.ui.selected = "session:sess-current"
        self.assertIsNone(self.press("x"))
        self.assertIsNone(self.ui.confirm)
        self.assertIn("current chat's session", self.ui.message)
        self.assertIn("aaagent sessions delete sess-cur --force", self.ui.message)

    def test_x_on_another_session_asks_first(self):
        self.ui.selected = "session:sess-old"
        self.assertIsNone(self.press("x"))
        self.assertIn('Delete session "fix login"', self.ui.confirm.prompt)
        self.assertIn("conversation file is deleted too", self.ui.confirm.prompt)
        self.assertEqual(self.press("y"), ("delete_session", "sess-old"))

    def test_x_on_a_gone_session_works_too(self):
        self.ui.selected = "session:sess-gone"
        self.press("x")
        self.assertEqual(self.ui.confirm.action, ("delete_session", "sess-gone"))

    def test_x_can_be_cancelled(self):
        self.ui.selected = "session:sess-old"
        self.press("x")
        self.assertIsNotNone(self.ui.confirm)
        self.assertIsNone(self.press("n"))
        self.assertIsNone(self.ui.confirm)
        self.assertEqual(self.ui.message, "cancelled")


class FakeScreen:
    """Just enough of a curses screen for Dashboard in tests."""

    def __init__(self, height=24, width=80):
        self.height, self.width = height, width
        self.written: list[tuple[int, int, str]] = []

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.written = []

    def addstr(self, y, x, text, attr=0):
        self.written.append((y, x, text))

    def refresh(self):
        pass

    def timeout(self, ms):
        pass

    def keypad(self, flag):
        pass

    def text(self) -> str:
        return "\n".join(t for _y, _x, t in self.written)


class ActTest(unittest.TestCase):
    """The model/thinking flow of Dashboard.act, without a terminal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.user = Path(self.tmp.name) / "user.toml"
        env = mock.patch.dict(os.environ, {"AAAGENT_USER_CONFIG": str(self.user)})
        env.start()
        self.addCleanup(env.stop)
        self.scr = FakeScreen()
        self.dash = tui.Dashboard(self.scr, self.project)
        self.dash.styles = dict.fromkeys(
            ("normal", "header", "running", "ok", "warn", "dim", "add", "del", "hunk", "title"), 0)
        self.dash.draw = lambda: None

    def test_model_picker_lists_the_choices_and_a_typed_name(self):
        with mock.patch.object(models, "choices", return_value=["sonnet", "opus"]):
            self.assertTrue(self.dash.act(("model", "planner")))
        picker = self.dash.ui.picker
        self.assertEqual(picker.title, "model for planner")
        self.assertEqual([i[1] for i in picker.items], ["opus", "sonnet", tui.TYPE_NEW])
        self.assertEqual(picker.items[0][2], "current")
        self.assertEqual(picker.items[-1][0], "type a name…")

        self.dash.act(("model_pick", "planner", tui.TYPE_NEW))
        self.assertEqual(self.dash.ui.prompt.action, ("model_pick", "planner"))
        self.dash.act(("model_pick", "planner", "sonnet"))
        self.assertEqual([i[1] for i in self.dash.ui.picker.items], ["user", "project"])

    def test_picker_can_pick_which_target(self):
        self.dash.act(("model", None))
        picker = self.dash.ui.picker
        self.assertEqual([i[1] for i in picker.items], ["planner", "reviewer", "worker1", "worker2"])
        self.assertEqual(picker.items[1][2], "planner (the chat)")
        self.assertEqual(picker.items[2][2], "opencode-go/deepseek-v4.1-flash")
        self.dash.act(("thinking", None))
        self.assertEqual([i[1] for i in self.dash.ui.picker.items], ["planner", "worker1", "worker2"],
                         "the planner reviews, so the reviewer has no thinking level to pick")
        self.assertEqual(self.dash.ui.picker.items[1][2], "default")

    def test_thinking_targets_include_the_reviewer_when_a_model_reviews(self):
        self.dash.act(("model_save", "reviewer", "sonnet", "project"))
        self.dash.act(("thinking", None))
        self.assertEqual([i[1] for i in self.dash.ui.picker.items],
                         ["planner", "reviewer", "worker1", "worker2"])

    def test_thinking_picker_lists_the_levels(self):
        with mock.patch.object(models, "thinking_choices", return_value=["low", "high", "max"]):
            self.dash.act(("thinking", "worker1"))
        picker = self.dash.ui.picker
        self.assertEqual(picker.title, "thinking for worker1")
        self.assertEqual([i[1] for i in picker.items], ["default", "low", "high", "max"])
        self.assertEqual(picker.items[0][2], "the model's own setting")

    def test_thinking_save_writes_the_user_config(self):
        with mock.patch.object(models, "thinking_choices", return_value=["low", "high", "max"]):
            self.dash.act(("thinking_save", "worker1", "high", "user"))
        text = self.user.read_text()
        self.assertIn("[workers.worker1]", text)
        self.assertIn('thinking = "high"', text)
        self.assertIn("worker1 thinking: high", self.dash.ui.message)
        self.assertEqual(self.dash.data["planner"]["model"], "opus")

    def test_model_save_writes_the_project_config(self):
        self.dash.act(("model_save", "planner", "sonnet", "project"))
        path = self.project / ".aaagent" / "config.toml"
        self.assertTrue(path.is_file())
        text = path.read_text()
        self.assertIn("[orchestrator]", text)
        self.assertIn('model = "sonnet"', text)
        self.assertIn("planner model: sonnet", self.dash.ui.message)

    def test_reviewer_model_picker_lists_the_planner_first(self):
        with mock.patch.object(models, "choices", return_value=["planner", "sonnet", "opus"]):
            self.assertTrue(self.dash.act(("model", "reviewer")))
        picker = self.dash.ui.picker
        self.assertEqual(picker.title, "who reviews each round?")
        self.assertEqual([i[0] for i in picker.items], ["planner (the chat)", "sonnet", "opus",
                                                        "type a name…"])
        self.assertEqual([i[1] for i in picker.items[:3]], ["planner", "sonnet", "opus"])
        self.assertEqual(picker.items[0][2], "current")
        self.assertEqual(picker.items[-1][1], tui.TYPE_NEW)
        self.assertEqual(picker.action, ("model_pick", "reviewer"))

        self.dash.act(("model_pick", "reviewer", "sonnet"))
        self.assertEqual([i[1] for i in self.dash.ui.picker.items], ["user", "project"])
        self.dash.act(("model_save", "reviewer", "sonnet", "project"))
        text = (self.project / ".aaagent" / "config.toml").read_text()
        self.assertIn("[reviewer]", text)
        self.assertIn('model = "sonnet"', text)
        self.assertEqual(self.dash.data["reviewer"], "reviewer")
        self.assertIn("reviewer: reviewer (sonnet", self.dash.ui.message)

    def test_reviewer_can_be_given_back_to_the_planner(self):
        self.dash.act(("model_save", "reviewer", "sonnet", "project"))
        self.assertEqual(self.dash.data["reviewer"], "reviewer")
        self.dash.act(("model_save", "reviewer", "planner", "project"))
        text = (self.project / ".aaagent" / "config.toml").read_text()
        self.assertIn('model = "planner"', text)
        self.assertEqual(self.dash.data["reviewer"], "orchestrator")

    def test_reviewer_picker_marks_the_current_model(self):
        self.dash.act(("model_save", "reviewer", "sonnet", "project"))
        with mock.patch.object(models, "choices", return_value=["planner", "sonnet", "opus"]):
            self.dash.act(("model", "reviewer"))
        details = {i[1]: i[2] for i in self.dash.ui.picker.items}
        self.assertEqual(details["sonnet"], "current")
        self.assertEqual(details["planner"], "")
        self.assertEqual(details["opus"], "")

    def test_reviewer_model_can_be_typed(self):
        with mock.patch.object(models, "choices", return_value=["planner", "sonnet"]):
            self.dash.act(("model", "reviewer"))
        self.dash.act(("model_pick", "reviewer", tui.TYPE_NEW))
        self.assertEqual(self.dash.ui.prompt.action, ("model_pick", "reviewer"))
        self.assertIn("reviewer", self.dash.ui.prompt.title)
        self.dash.act(("model_pick", "reviewer", "opus"))
        self.assertEqual([i[1] for i in self.dash.ui.picker.items], ["user", "project"])

    def test_add_worker_picks_a_model_and_creates_it(self):
        with mock.patch.object(models, "new_worker_choices", return_value=["sonnet", "opus"]):
            self.assertTrue(self.dash.act(("add_worker",)))
        picker = self.dash.ui.picker
        self.assertEqual(picker.title, "model for the new worker")
        self.assertEqual([i[1] for i in picker.items], ["sonnet", "opus", tui.TYPE_NEW])
        self.assertEqual(picker.action, ("add_worker_pick",))

        self.dash.act(("add_worker_pick", tui.TYPE_NEW))
        self.assertEqual(self.dash.ui.prompt.action, ("add_worker_pick",))
        self.assertEqual(self.dash.ui.prompt.title, "model for the new worker: ")

        self.dash.act(("add_worker_pick", "sonnet"))
        self.assertEqual([i[1] for i in self.dash.ui.picker.items], ["user", "project"])

        self.dash.act(("add_worker_save", "sonnet", "user"))
        text = self.user.read_text()
        self.assertIn("[workers.worker3]", text)
        self.assertIn('model = "sonnet"', text)
        self.assertEqual([w["name"] for w in self.dash.data["workers"]],
                         ["worker1", "worker2", "worker3"])
        self.assertEqual(self.dash.ui.selected, "worker:worker3")
        self.assertIn("added worker3", self.dash.ui.message)

    def test_remove_worker_removes_it_and_resets_the_output(self):
        self.dash.act(("add_worker_save", "sonnet", "user"))
        self.dash.ui.output_worker = "worker3"
        self.assertTrue(self.dash.act(("remove_worker", "worker3")))
        self.assertEqual([w["name"] for w in self.dash.data["workers"]], ["worker1", "worker2"])
        self.assertIsNone(self.dash.ui.output_worker)
        self.assertIn("removed worker3", self.dash.ui.message)

    def test_removing_the_only_worker_reports_the_error(self):
        self.user.write_text('[workers.worker1]\nmodel = "sonnet"\n')
        self.dash.load()
        self.assertEqual([w["name"] for w in self.dash.data["workers"]], ["worker1"])
        self.dash.act(("remove_worker", "worker1"))
        self.assertIn("only worker", self.dash.ui.message)

    def test_branch_mode_picker_marks_the_current_mode(self):
        self.assertTrue(self.dash.act(("branch_mode",)))
        picker = self.dash.ui.picker
        self.assertEqual(picker.title, "where do tasks commit?")
        self.assertEqual([i[1] for i in picker.items], ["current", "branch", "pr", "none"])
        self.assertEqual([i[0] for i in picker.items],
                         ["current branch", "new branch", "new branch + pull request", "no git"])
        self.assertEqual(picker.items[0][2], "current")
        self.assertEqual(picker.items[1][2],
                         "one branch per chat session; asks before finishing it")
        self.assertEqual(picker.items[2][2],
                         "one branch per chat session; asks before the pull request")
        self.assertEqual(picker.items[3][2],
                         "no repository needed; aaagent keeps its own history")
        self.assertEqual(picker.action, ("branch_mode_pick",))

    def test_branch_mode_pick_writes_the_project_config(self):
        self.assertTrue(self.dash.act(("branch_mode_pick", "pr")))
        self.assertEqual(Config.load(self.project).branch_mode, "pr")
        text = (self.project / ".aaagent" / "config.toml").read_text()
        self.assertIn("[git]", text)
        self.assertIn('branch_mode = "pr"', text)
        self.assertEqual(self.dash.ui.message, "branch mode: pr")
        self.assertEqual(self.dash.data["git"]["mode"], "pr")

    def test_branch_mode_pick_shows_the_lines_from_change_mode(self):
        lines = ["branch mode: pr", "note: the open branch feat/x keeps going"]
        with mock.patch.object(branching, "change_mode", return_value=lines) as change:
            self.assertTrue(self.dash.act(("branch_mode_pick", "pr")))
        change.assert_called_once_with(self.project, "pr")
        self.assertEqual(self.dash.ui.message, "; ".join(lines))

    def test_branch_mode_pick_shows_a_refusal(self):
        with mock.patch.object(branching, "change_mode",
                               side_effect=tui.FlowError("finish the open branch first: "
                                                         "`aaagent pr` or `aaagent branch-done`")):
            self.assertTrue(self.dash.act(("branch_mode_pick", "none")))
        self.assertEqual(self.dash.ui.message,
                         "finish the open branch first: `aaagent pr` or `aaagent branch-done`")
        self.assertEqual(Config.load(self.project).branch_mode, "current")

    def test_draw_shows_the_git_summary_in_the_top_line(self):
        self.dash.data = dict(data(), git={"mode": "branch", "branch": "feat/x",
                                           "batch": {"branch": "feat/x", "base": "main",
                                                     "tasks": ["001-a"]},
                                           "last_pr": None})
        tui.Dashboard.draw(self.dash)
        top = [text for y, _x, text in self.scr.written if y == 0]
        self.assertIn("new branch · on feat/x (from main)", top)

    def test_draw_shows_no_git_in_the_top_line(self):
        self.dash.data = dict(data(), git={"mode": "none", "branch": None, "batch": None,
                                           "last_pr": None})
        tui.Dashboard.draw(self.dash)
        top = [text for y, _x, text in self.scr.written if y == 0]
        self.assertIn("no git", top)

    def test_errors_go_to_the_message(self):
        self.dash.act(("model_save", "planner", "  ", "project"))
        self.assertIn("empty", self.dash.ui.message)

    def test_draw_shows_the_picker_and_the_prompt(self):
        self.dash.ui.picker = tui.Picker("model for planner", [("opus", "opus", "current"),
                                                              ("sonnet", "sonnet", "")], index=1)
        tui.Dashboard.draw(self.dash)
        text = self.scr.text()
        self.assertIn("model for planner", text)
        self.assertIn("sonnet", text)
        self.assertIn("enter select", text)

        self.dash.ui.picker = None
        self.dash.ui.prompt = tui.Prompt("model for planner: ", "sonne")
        tui.Dashboard.draw(self.dash)
        text = self.scr.text()
        self.assertIn("model for planner: sonne_", text)
        self.assertIn("enter save", text)


class ActSessionsTest(unittest.TestCase):
    """Restoring and opening chat sessions from Dashboard.act, without a terminal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.user = Path(self.tmp.name) / "user.toml"
        env = mock.patch.dict(os.environ, {"AAAGENT_USER_CONFIG": str(self.user)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AAAGENT_TMUX_CLOSE", None)
        self.scr = FakeScreen()
        self.dash = tui.Dashboard(self.scr, self.project)
        self.dash.chat_pane = None
        self.dash.styles = dict.fromkeys(
            ("normal", "header", "running", "ok", "warn", "dim", "add", "del", "hunk", "title"), 0)
        self.dash.draw = lambda: None
        self.dash.data = data(sessions=[
            session_(id="sess-current", current=True, tasks=["004-health"]),
            session_(id="sess-gone", title="a gone chat", available=False),
            session_(id="sess-old", title="fix login", tasks=["003-old"], last_active=recently(hours=2)),
            session_(id="sess-older", title="earlier work", last_active=recently(days=2))])

    def test_restore_builds_a_picker_of_reopenable_sessions(self):
        self.assertTrue(self.dash.act(("restore",)))
        picker = self.dash.ui.picker
        self.assertEqual(picker.title, "restore which session?")
        self.assertEqual([i[1] for i in picker.items], ["sess-old", "sess-older"])
        self.assertEqual([i[0] for i in picker.items], ["fix login", "earlier work"])
        self.assertEqual(picker.action, ("open_session",))
        self.assertEqual(picker.items[0][2], "2 h ago · 1 task")

    def test_restore_without_an_earlier_session_says_so(self):
        self.dash.data["sessions"] = [session_(id="sess-current", current=True)]
        self.dash.act(("restore",))
        self.assertIsNone(self.dash.ui.picker)
        self.assertEqual(self.dash.ui.message, "no earlier session to restore")

    def test_open_session_restarts_the_chat_pane_on_it(self):
        self.dash.chat_pane = "%1"
        with mock.patch.object(session, "respawn_chat") as respawn, \
                mock.patch.object(self.dash, "load"):
            self.assertTrue(self.dash.act(("open_session", "sess-old")))
        respawn.assert_called_once_with(self.project, "%1", "sess-old", "tmux kill-window")
        self.assertEqual(self.dash.ui.message, "opened session sess-old")

    def test_open_session_needs_the_tmux_layout(self):
        with mock.patch.object(session, "respawn_chat") as respawn:
            self.assertTrue(self.dash.act(("open_session", "sess-old")))
        respawn.assert_not_called()
        self.assertIn("tmux layout", self.dash.ui.message)

    def test_delete_session_forgets_it_and_says_so(self):
        from aaagent import sessions
        transcript = Path(self.tmp.name) / "sess-old.jsonl"
        transcript.write_text("{}\n")
        sessions.record_start(self.project, "sess-old", str(transcript),
                              now="2026-11-13T20:00:00+0900")
        sessions.record_start(self.project, "sess-current", None,
                              now="2026-11-13T21:00:00+0900")
        self.dash.load()
        self.assertEqual([s["id"] for s in self.dash.data["sessions"]],
                         ["sess-current", "sess-old"])

        self.assertTrue(self.dash.act(("delete_session", "sess-old")))
        self.assertEqual([s["id"] for s in self.dash.data["sessions"]], ["sess-current"])
        self.assertFalse(transcript.exists())
        self.assertEqual(self.dash.ui.message, "deleted session sess-old")


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
