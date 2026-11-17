"""Per-task git worktrees: `[parallel] isolation = "worktree"`."""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from aaagent import cli, dispatch, status, worker, worktrees  # noqa: E402
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

    def test_commit_lands_on_the_branch(self):
        task = self.new_task("feat commit", "d.txt")
        self.implement(task)                       # writes an approved review
        worktree = self.worktree_of(task)

        out = self.commit(task)
        state = task.load()
        self.assertTrue((self.project / "d.txt").is_file(), out)
        self.assertIn("d.txt", self.git("log", "-1", "--name-only"))
        self.assertEqual(state["commit"], self.git("rev-parse", "HEAD").strip())
        self.assertFalse(worktree.exists(), "the worktree is removed after the merge")
        self.assertEqual(self.git("branch", "--list", f"aaagent/{task.id}").strip(), "")

    def test_second_task_is_cherry_picked(self):
        one = self.new_task("feat alpha", "a.txt")
        two = self.new_task("fix beta", "b.txt")
        self.implement(one)
        self.implement(two)
        self.commit(one)
        self.commit(two)

        self.assertTrue((self.project / "a.txt").is_file())
        self.assertTrue((self.project / "b.txt").is_file())
        self.assertEqual(self.git("rev-parse", "HEAD~1").strip(), one.load()["commit"],
                         "the first task keeps its commit; the second is cherry-picked on top")
        self.assertEqual(one.load()["status"], "committed")
        self.assertEqual(two.load()["status"], "committed")

    def test_already_applied_change_is_skipped(self):
        task = self.new_task("feat already applied", "a.txt")
        self.implement(task)
        worktree = self.worktree_of(task)
        # The very same change is already on the branch, by a commit of our own.
        content = (worktree / "a.txt").read_text()
        (self.project / "a.txt").write_text(content)
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "the same change, already on the branch")
        manual_head = self.git("rev-parse", "HEAD").strip()

        out = self.commit(task)

        self.assertIn("already on the branch", out)
        self.assertIn("1 commit(s) already on the branch were skipped", out)
        self.assertIn("nothing new", out)
        self.assertNotIn("UU", self.git("status", "--porcelain"), "the checkout is left clean")
        self.assertFalse((self.project / ".git" / "CHERRY_PICK_HEAD").exists(),
                         "no cherry-pick is left in progress")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), manual_head,
                         "the identical change is not committed a second time")
        self.assertFalse(worktree.exists(), "the worktree is removed after the merge")

    def test_partial_apply_is_rolled_back(self):
        task = self.new_task("feat partial", "a.txt")
        self.implement(task)
        worktree = self.worktree_of(task)
        # Two commits on the task branch: the first applies cleanly, the second conflicts.
        self.git_in(worktree, "add", "a.txt")
        self.git_in(worktree, "commit", "-q", "-m", "feat: task change to a.txt")
        (worktree / "b.txt").write_text("from the task\n")
        self.git_in(worktree, "add", "b.txt")
        self.git_in(worktree, "commit", "-q", "-m", "feat: task change to b.txt")
        (self.project / "b.txt").write_text("from main\n")
        self.git("add", "b.txt")
        self.git("commit", "-q", "-m", "conflicting change to b.txt")
        conflict_head = self.git("rev-parse", "HEAD").strip()

        code, out = self.run_dispatch(task)

        self.assertEqual(code, 1, out)
        self.assertEqual(task.load()["status"], "failed", out)
        self.assertIn("could not apply", out)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), conflict_head,
                         "the commit picked before the conflict is rolled back")
        self.assertFalse((self.project / "a.txt").exists(), "nothing of the task stays applied")
        self.assertNotIn("UU", self.git("status", "--porcelain"), "the checkout is left clean")
        self.assertFalse((self.project / ".git" / "CHERRY_PICK_HEAD").exists())
        self.assertTrue(worktree.is_dir(), "the worktree is kept for the retry")

    def test_merge_failure_keeps_the_worktree_and_retries_only_the_merge(self):
        args_log = Path(self.tmp.name) / "oc-args.jsonl"
        os.environ["FAKE_OC_ARGS_LOG"] = str(args_log)
        task = self.new_task("feat conflict", "a.txt")
        self.implement(task)
        worktree = self.worktree_of(task)
        (self.project / "a.txt").write_text("other\n")
        self.git("add", "a.txt")
        self.git("commit", "-q", "-m", "conflicting change")
        conflict_head = self.git("rev-parse", "HEAD").strip()

        code, out = self.run_dispatch(task)
        self.assertEqual(code, 1, out)
        self.assertEqual(task.load()["status"], "failed", out)
        self.assertIn("could not apply", out)
        self.assertTrue(worktree.is_dir(), "the worktree is kept for the retry")
        self.assertNotIn("UU", self.git("status", "--porcelain"), "the checkout is left clean")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), conflict_head)

        self.git("reset", "--hard", "HEAD~1")
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "committed", out)
        self.assertTrue((self.project / "a.txt").is_file())

        calls = [json.loads(line) for line in args_log.read_text().splitlines() if line.strip()]
        committers = [args for args in calls if "aaagent-committer" in args]
        self.assertEqual(len(committers), 1, "the retry runs the merge only, not a second commit")

    def test_abandon_keeps_the_worktree(self):
        task = self.new_task("feat abandon", "a.txt")
        self.implement(task, approved=False)
        worktree = self.worktree_of(task)

        text = dispatch.abandon(self.project, task.id)
        self.assertIn("git worktree remove", text)
        self.assertTrue(worktree.is_dir(), "the worktree stays for the user to inspect")

    def test_missing_worktree_is_an_error(self):
        task = self.new_task("feat missing", "a.txt")
        self.implement(task, approved=False)
        task.review(1).write_text(review("changes_requested", 1))
        shutil.rmtree(self.worktree_of(task))

        with self.assertRaises(FlowError) as caught:
            self.run_dispatch(task)
        self.assertIn("missing", str(caught.exception))

    def test_stale_review_copy_is_removed(self):
        task = self.new_task("feat stale", "a.txt")
        self.implement(task, approved=False)
        worktree = self.worktree_of(task)
        stale = worktree / ".aaagent" / "tasks" / task.id / "review-1.md"
        stale.write_text(review("approved", 1))

        worktrees.sync_into(self.project, task, worktree)
        self.assertFalse(task.review(1).exists())
        self.assertFalse(stale.exists(), "a review the main folder does not have is dropped")

    def test_links_are_not_outside_scope(self):
        self.set_section("parallel", 'isolation = "worktree"', 'links = [".venv"]')
        (self.project / ".venv").mkdir()
        (self.project / ".venv" / "marker").write_text("here\n")
        task = self.new_task("feat linked scope", "l.txt")

        out = self.implement(task, approved=False)
        self.assertNotIn("changed outside its declared files", out)

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
