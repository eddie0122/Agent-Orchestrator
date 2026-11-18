"""The automatic check after each implement/fix round, before the review."""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import dispatch, tasks, worker  # noqa: E402
from aaagent.config import REVIEWER, Config, project_config_path  # noqa: E402

FAKE_OC = ROOT / "tests" / "fake_opencode.py"

PLAN = """---
task: x
worker:
---
# Goal
Append the model name to work.txt.
"""

REVIEWER_CFG = '[reviewer]\nmodel = "opencode-go/glm-5.3-flash"\n'


class CheckGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.args_log = Path(self.tmp.name) / "args.jsonl"
        self.env_backup = dict(os.environ)
        os.environ.update({
            "AAAGENT_OPENCODE_BIN": str(FAKE_OC),
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "FAKE_OC_ARGS_LOG": str(self.args_log),
            "FAKE_OC_FAIL": "", "FAKE_OC_NOREPORT": "",
            "FAKE_OC_REVIEW": "", "FAKE_OC_NOREVIEW": "",
            "AAAGENT_PROJECT": str(self.project),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        self.git("init", "-q")
        (self.project / "README.md").write_text("hi\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "init")
        from aaagent import session
        session.init_project(self.project, Config.load(self.project))

    def stop_workers(self):
        for name in ("worker1", "worker2", REVIEWER):
            worker.stop(self.project, name)
        for _ in range(50):
            if not any(worker.worker_pid(self.project, n)
                       for n in ("worker1", "worker2", REVIEWER)):
                break
            time.sleep(0.1)

    def tearDown(self):
        self.stop_workers()
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------------

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.project, check=True,
                              capture_output=True, text=True).stdout

    def config(self, text: str) -> None:
        project_config_path(self.project).write_text(text)

    def new_task(self, title="add work") -> tasks.Task:
        task = tasks.create(self.project, title)
        task.plan.write_text(PLAN)
        return task

    def run_dispatch(self, task) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = dispatch.dispatch(self.project, task.id)
        return code, buf.getvalue()

    def calls(self) -> list[list[str]]:
        if not self.args_log.exists():
            return []
        return [json.loads(line) for line in self.args_log.read_text().splitlines()]

    def review_calls(self) -> list[list[str]]:
        return [call for call in self.calls()
                if call[call.index("--agent") + 1] == "aaagent-reviewer"]

    def check_text(self, task, rnd=1) -> str:
        path = task.path / f"check-{rnd}.md"
        self.assertTrue(path.is_file(), f"check-{rnd}.md was not written")
        return path.read_text()

    # -- 1. nothing configured: unchanged ------------------------------------

    def test_no_check_configured_changes_nothing(self):
        self.config(REVIEWER_CFG)
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertNotIn("running check", out)
        self.assertNotIn("check passed", out)
        self.assertFalse((task.path / "check-1.md").exists())
        self.assertNotIn("check", task.load())

    # -- 2. a passing check reaches the reviewer ------------------------------

    def test_passing_check_is_saved_and_given_to_the_reviewer(self):
        self.config(REVIEWER_CFG + '[check]\ncommand = "test -f work.txt"\n')
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        text = self.check_text(task)
        self.assertIn("ok: true", text)
        self.assertIn("round: 1", text)
        self.assertIn("✓ check passed", out)
        self.assertIn("check-1.md", out)
        state = task.load()
        self.assertEqual(state["check"], {"round": 1, "ok": True, "code": 0,
                                          "timed_out": False})

        reviews = self.review_calls()
        self.assertEqual(len(reviews), 1, reviews)
        prompt = reviews[0][-1]
        self.assertIn("AUTOMATIC CHECK", prompt)
        self.assertIn("PASSED", prompt)
        self.assertIn("check-1.md", prompt)

    # -- 3. a failing check with the default on_fail = "review" --------------

    def test_failing_check_still_reviews_and_warns_on_approval(self):
        self.config(REVIEWER_CFG + '[check]\ncommand = "exit 7"\n')
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertIn("✗ check failed (exit 7)", out)
        self.assertIn("the automatic check failed for this round", out)
        self.assertIn("ok: false", self.check_text(task))
        self.assertEqual(task.verdict(1), "approved")
        reviews = self.review_calls()
        self.assertEqual(len(reviews), 1, reviews)
        prompt = reviews[0][-1]
        self.assertIn("AUTOMATIC CHECK", prompt)
        self.assertIn("FAILED", prompt)

    # -- 4. on_fail = "fix": the round is rejected without a review -----------

    def test_failing_check_with_on_fail_fix_rejects_without_a_review(self):
        self.config(REVIEWER_CFG
                    + '[check]\ncommand = "false"\non_fail = "fix"\n')
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertIn("rejected without a review", out)
        review = task.review(1)
        self.assertTrue(review.is_file())
        self.assertEqual(task.verdict(1), "changes_requested")
        self.assertIn("Automatic check failed; no reviewer ran.", review.read_text())
        self.assertEqual(task.load()["status"], "changes_requested")
        self.assertEqual(self.review_calls(), [], "no review job runs for a rejected round")
        self.assertTrue(task.report(2).is_file(), "the next round is a fix")

    # -- 5. a blocked report always reaches a reviewer ------------------------

    def test_only_done_and_partial_reports_are_rejected_by_the_check(self):
        self.assertTrue(dispatch._check_rejects("done", "fix"))
        self.assertTrue(dispatch._check_rejects("partial", "fix"))
        self.assertFalse(dispatch._check_rejects("blocked", "fix"))
        self.assertFalse(dispatch._check_rejects("?", "fix"))
        self.assertFalse(dispatch._check_rejects("done", "review"))

    def test_check_settings_defaults_and_fallbacks(self):
        cfg = Config.load(self.project)
        self.assertEqual(dispatch.check_settings(cfg), ("", "review", 20.0))

        self.config('[integration]\ncommand = "true"\ntimeout_minutes = 5\n'
                    '[check]\ncommand = ""\non_fail = "review"\ntimeout_minutes = 0\n')
        cfg = Config.load(self.project)
        self.assertEqual(dispatch.check_settings(cfg), ("true", "review", 5.0))

    # -- 6. enabled = false wins over the integration command ----------------

    def test_enabled_false_turns_the_check_off(self):
        self.config(REVIEWER_CFG
                    + '[integration]\ncommand = "true"\n'
                    + '[check]\nenabled = false\n')
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertNotIn("running check", out)
        self.assertNotIn("Integration check", out)
        self.assertFalse((task.path / "check-1.md").exists())

    # -- 7. an empty [check] command falls back to [integration] -------------

    def test_empty_check_command_falls_back_to_the_integration_command(self):
        self.config(REVIEWER_CFG + '[integration]\ncommand = "test -f work.txt"\n')
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertIn("running check: test -f work.txt", out)
        self.assertIn("ok: true", self.check_text(task))

    # -- 8. worktree mode: the check runs in the task's own tree -------------

    def test_check_runs_in_the_worktree_and_the_reviewer_can_read_it(self):
        self.config(REVIEWER_CFG + '[parallel]\nisolation = "worktree"\n'
                    + '[check]\ncommand = "test -f work.txt"\n')
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertIn("✓ check passed", out)
        worktree = Path(task.load()["worktree"])
        copied = worktree / ".aaagent" / "tasks" / task.id / "check-1.md"
        self.assertTrue(copied.is_file(), "the review job reads the check from its worktree")
        self.assertEqual(copied.read_text(), self.check_text(task))
        reviews = self.review_calls()
        self.assertEqual(len(reviews), 1, reviews)
        self.assertIn("PASSED", reviews[0][-1])


if __name__ == "__main__":
    unittest.main()
