"""The optional `[integration]` check: at the end of a branch and before finishing it."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from aaagent import branching  # noqa: E402
from aaagent.config import Config, project_config_path, set_toml_value  # noqa: E402
from aaagent.tasks import FlowError  # noqa: E402
from test_branching import FlowCase  # noqa: E402

FIX_FAILED = "echo broken-thing; exit 4"


class IntegrationGateTest(FlowCase):
    def write_integration(self, command, timeout_minutes=None):
        path = project_config_path(self.project)
        set_toml_value(path, "integration", "command", command)
        if timeout_minutes is not None:
            set_toml_value(path, "integration", "timeout_minutes", timeout_minutes)
        self.cfg = Config.load(self.project)

    def one_commit(self, title="fix alpha"):
        task = self.new_task(title, "alpha.txt")
        self.implement(task)
        return task, self.commit(task)

    def test_passing_check_keeps_the_usual_instruction(self):
        self.write_mode("pr")
        self.write_integration("true")
        task, out = self.one_commit()

        self.assertIn("All tasks on branch fix/alpha are done (1 commit(s) since main).", out)
        self.assertIn("Integration check passed: `true`", out)
        self.assertIn("Ask the user whether to open a pull request", out)
        self.assertIn("`aaagent pr`", out)

    def test_failing_check_blocks_the_pull_request(self):
        self.write_mode("pr")
        self.write_integration(FIX_FAILED)
        task, out = self.one_commit()

        self.assertIn("Integration check FAILED", out)
        self.assertIn("exit 4", out)
        self.assertIn("broken-thing", out)
        self.assertIn("Plan a fix task", out)
        self.assertNotIn("Ask the user whether to open a pull request", out)

        with self.assertRaisesRegex(FlowError, "integration"):
            branching.finish(self.project, self.cfg, push=True)
        self.assertIsNotNone(branching.open_batch(self.project),
                             "a failed check leaves the branch open")

    def test_pr_auto_failure_says_do_not_open(self):
        self.write_mode("pr-auto")
        self.write_integration("exit 1")
        task, out = self.one_commit()

        self.assertIn("Do not open a pull request", out)
        self.assertNotIn("branch_mode is pr-auto", out)

    def test_finish_runs_the_check_when_stale(self):
        self.write_mode("pr")
        self.write_integration("true")
        task, out = self.one_commit()
        self.assertIn("Integration check passed", out)

        # A commit made by hand moves HEAD past the stored result.
        (self.project / "extra.txt").write_text("extra\n")
        self.git("add", "extra.txt")
        self.git("commit", "-q", "-m", "feat: extra work")
        self.write_integration("echo stale-failure; exit 3")

        with self.assertRaisesRegex(FlowError, "integration"):
            branching.finish(self.project, self.cfg, push=False)
        self.assertIsNotNone(branching.open_batch(self.project))

        self.write_integration("true")
        lines = branching.finish(self.project, self.cfg, push=False)
        self.assertEqual(lines, ["branch fix/alpha finished (2 commit(s)); not pushed",
                                 "switched back to main"])

    def test_timeout_is_a_failure(self):
        self.write_mode("pr")
        self.write_integration("sleep 5", timeout_minutes=0.01)
        task, out = self.one_commit()

        self.assertIn("timed out", out)
        self.assertIn("Do not open a pull request", out)

    def test_no_command_is_unchanged(self):
        self.write_mode("pr")
        task, out = self.one_commit()

        self.assertNotIn("Integration check", out)
        self.assertIn("Ask the user whether to open a pull request", out)


if __name__ == "__main__":
    unittest.main()
