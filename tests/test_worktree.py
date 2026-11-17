"""Per-task git worktrees: `[parallel] isolation = "worktree"`."""

import argparse
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from aaagent import cli, status, worker, worktrees  # noqa: E402
from aaagent.config import Config, project_config_path  # noqa: E402
from aaagent.tasks import FlowError  # noqa: E402
from test_branching import FlowCase, review  # noqa: E402


class WorktreeCase(FlowCase):
    """FlowCase with `[parallel] isolation = "worktree"` in the project config."""

    def setUp(self):
        super().setUp()
        self.set_section("parallel", 'isolation = "worktree"')

    def set_section(self, name, *lines):
        """Replace one section of the project config (rather than add a second one)."""
        path = project_config_path(self.project)
        text = path.read_text() if path.is_file() else ""
        kept, skipping = [], False
        for line in text.splitlines():
            if line.strip().startswith("["):
                skipping = line.strip() == f"[{name}]"
            if not skipping:
                kept.append(line)
        body = "\n".join(kept).strip("\n")
        block = f"[{name}]\n" + "\n".join(lines)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((body + "\n\n" if body else "") + block + "\n")
        self.cfg = Config.load(self.project)

    def git_in(self, path: Path, *args) -> str:
        return subprocess.run(["git", "-C", str(path), *args], check=True,
                              capture_output=True, text=True).stdout

    def worktree_of(self, task) -> Path:
        return Path(task.load()["worktree"])


class ImplementInWorktreeTest(WorktreeCase):
    def test_implement_runs_in_the_worktree(self):
        task = self.new_task("feat alpha", "a.txt")
        with mock.patch.object(worker, "enqueue", wraps=worker.enqueue) as enqueue:
            out = self.implement(task, approved=False)
        self.assertIn("own worktree", out)

        state = task.load()
        worktree = Path(state["worktree"])
        self.assertEqual(enqueue.call_args[0][1]["cwd"], str(worktree),
                         "the job tells the worker where to run")
        self.assertTrue(worktree.is_dir(), state)
        self.assertEqual(state["task_branch"], f"aaagent/{task.id}")
        self.assertTrue((worktree / "a.txt").is_file(), "the job wrote in the worktree")
        self.assertFalse((self.project / "a.txt").exists(), "the main tree stays untouched")
        self.assertTrue(task.report(1).is_file(), "the report is copied back")
        self.assertEqual(worktrees.workdir(self.project, task), worktree)
        self.assertEqual(self.git_in(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip(),
                         f"aaagent/{task.id}")

        data = status.status_data(self.project, self.cfg)
        row = next(entry for entry in data["tasks"] if entry["id"] == task.id)
        self.assertEqual(row["worktree"], str(worktree))

        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_diff(argparse.Namespace(task=task.id, stat=True))
        self.assertIn("a.txt", buf.getvalue(), "`aaagent diff` shows the worktree's changes")

    def test_fix_round_sees_the_revised_review(self):
        task = self.new_task("feat beta", "b.txt")
        self.implement(task, approved=False)
        worktree = self.worktree_of(task)

        task.review(1).write_text(review("changes_requested", 1))
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "awaiting_review", out)
        self.assertTrue(task.report(2).is_file(), "the fix report is copied back")
        synced = worktree / ".aaagent" / "tasks" / task.id / "review-1.md"
        self.assertIn("changes_requested", synced.read_text(), "the review was synced in")

    def test_two_tasks_do_not_see_each_other(self):
        one = self.new_task("feat alpha", "a.txt")
        two = self.new_task("fix beta", "b.txt")
        self.implement(one, approved=False)
        self.implement(two, approved=False)

        one_tree, two_tree = self.worktree_of(one), self.worktree_of(two)
        self.assertNotEqual(one_tree, two_tree)
        self.assertTrue((one_tree / "a.txt").is_file())
        self.assertFalse((one_tree / "b.txt").exists())
        self.assertTrue((two_tree / "b.txt").is_file())
        self.assertFalse((two_tree / "a.txt").exists())
        self.assertFalse((self.project / "a.txt").exists())
        self.assertFalse((self.project / "b.txt").exists())

    def test_links_and_setup(self):
        self.set_section("parallel", 'isolation = "worktree"', 'links = [".venv"]',
                         'setup = "touch setup-ran"')
        (self.project / ".venv").mkdir()
        (self.project / ".venv" / "marker").write_text("here\n")

        task = self.new_task("feat links", "l.txt")
        self.implement(task, approved=False)

        worktree = self.worktree_of(task)
        self.assertTrue((worktree / ".venv").is_symlink())
        self.assertTrue((worktree / ".venv" / "marker").is_file())
        self.assertTrue((worktree / "setup-ran").is_file(), "the setup command ran in the worktree")
        self.assertFalse((self.project / "setup-ran").exists())

    def test_failing_setup_stops_the_dispatch(self):
        self.set_section("parallel", 'isolation = "worktree"', 'setup = "echo boom; exit 3"')
        task = self.new_task("feat broken setup", "s.txt")
        with self.assertRaises(FlowError) as caught:
            self.run_dispatch(task)
        self.assertIn("boom", str(caught.exception))

    def test_track_agent_dir_falls_back_to_shared(self):
        self.set_section("git", "track_agent_dir = true")
        task = self.new_task("feat shared", "c.txt")
        out = self.implement(task, approved=False)
        self.assertIn("is ignored", out)
        self.assertNotIn("worktree", task.load())
        self.assertTrue((self.project / "c.txt").is_file())
        self.assertFalse((self.project / ".aaagent" / "worktrees").exists())

    def test_commit_is_refused_for_now(self):
        task = self.new_task("feat commit", "d.txt")
        self.implement(task)                       # writes an approved review
        with self.assertRaises(FlowError) as caught:
            self.run_dispatch(task)
        self.assertIn("worktree", str(caught.exception))

    def test_shared_mode_job_has_no_cwd(self):
        self.set_section("parallel", 'isolation = "shared"')
        task = self.new_task("feat shared mode", "e.txt")
        with mock.patch.object(worker, "enqueue", wraps=worker.enqueue) as enqueue:
            out = self.implement(task, approved=False)
        self.assertNotIn("own worktree", out)
        self.assertNotIn("worktree", task.load())
        self.assertNotIn("cwd", enqueue.call_args[0][1], "shared jobs stay exactly as before")
        self.assertTrue((self.project / "e.txt").is_file())
        self.assertFalse((self.project / ".aaagent" / "worktrees").exists())


if __name__ == "__main__":
    unittest.main()
