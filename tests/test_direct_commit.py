"""Commit an approved task in aaagent itself (`[git] direct_commit`), fallback to a commit job."""

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
os.environ.setdefault("AAAGENT_POLL_SECONDS", "0.05")

from aaagent import dispatch, gitutil, tasks, worker  # noqa: E402
from aaagent.config import Config, project_config_path, set_toml_value  # noqa: E402
from aaagent import session  # noqa: E402

FAKE = ROOT / "tests" / "fake_opencode.py"
DS, GLM = "opencode-go/deepseek-v4.1-flash", "opencode-go/glm-5.3-flash"


def plan(files=None):
    lines = ["---", "task: x", "worker:"]
    if files is not None:
        lines.append(f"files: {files}")
    lines += ["---", "# Goal", "Fake work.", ""]
    return "\n".join(lines)


def review(verdict, rnd):
    return f"---\nverdict: {verdict}\nround: {rnd}\n---\n## Required changes\n1. more\n"


def git_result(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


class DirectCommitCase(unittest.TestCase):
    """A temp git project, the fake opencode, and dispatch helpers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.args_log = Path(self.tmp.name) / "args.jsonl"
        self.env_backup = dict(os.environ)
        self.addCleanup(self.restore_env)
        os.environ.update({
            "AAAGENT_OPENCODE_BIN": str(FAKE),
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "FAKE_OC_ARGS_LOG": str(self.args_log),
            "FAKE_OC_FAIL": "", "FAKE_OC_NOREPORT": "",
            "FAKE_OC_REVIEW": "", "FAKE_OC_NOREVIEW": "",
            "AAAGENT_PROJECT": str(self.project),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        os.environ.pop("FAKE_OC_COMMIT_MSG", None)
        self.git("init", "-q")
        (self.project / "README.md").write_text("hi\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "init")
        session.init_project(self.project, Config.load(self.project))

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self.env_backup)

    def tearDown(self):
        for name in ("worker1", "worker2"):
            worker.stop(self.project, name)
        for _ in range(50):
            if not any(worker.worker_pid(self.project, n) for n in ("worker1", "worker2")):
                break
            time.sleep(0.1)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.project, check=True,
                              capture_output=True, text=True).stdout

    def set_config(self, section, key, value):
        set_toml_value(project_config_path(self.project), section, key, value)

    def new_task(self, title, files="work.txt") -> tasks.Task:
        task = tasks.create(self.project, title)
        task.plan.write_text(plan(files))
        return task

    def run_dispatch(self, task) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = dispatch.dispatch(self.project, task.id)
        return code, buf.getvalue()

    def implement(self, task):
        """Dispatch the implement round and write an approved review."""
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "awaiting_review", out)
        task.review(1).write_text(review("approved", 1))
        return out

    def commit(self, task) -> tuple[int, str]:
        return self.run_dispatch(task)

    def calls(self):
        if not self.args_log.exists():
            return []
        return [json.loads(line) for line in self.args_log.read_text().splitlines()]

    def agents(self):
        return [c[c.index("--agent") + 1] for c in self.calls()]


class DirectCommitFlowTest(DirectCommitCase):
    def test_commit_message_is_used_without_a_commit_job(self):
        os.environ["FAKE_OC_COMMIT_MSG"] = ("feat: add the work\\n\\n"
                                            "- append the model name\\n"
                                            "- keep it short")
        task = self.new_task("feat add work", "work.txt, more.txt")
        self.implement(task)
        self.assertNotIn("aaagent-committer", self.agents())

        code, out = self.commit(task)

        self.assertEqual(code, 0, out)
        state = task.load()
        self.assertEqual(state["status"], "committed", out)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), state["commit"])
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "feat: add the work")
        self.assertEqual(self.git("log", "-1", "--format=%b").strip(),
                         "- append the model name\n- keep it short")
        self.assertNotIn("aaagent-committer", self.agents())
        self.assertIsNone(tasks.active_task(self.project))
        committed = [e for e in state["history"] if e["event"] == "committed"]
        self.assertEqual(committed[-1]["worker"], "aaagent")
        self.assertIn("✓ committed by aaagent:", out)
        self.assertIn("Task", out)

    def test_missing_section_falls_back_to_the_commit_job(self):
        task = self.new_task("feat no message")
        self.implement(task)

        code, out = self.commit(task)

        self.assertEqual(code, 0, out)
        self.assertIn("no commit message in the report", out)
        self.assertEqual(task.load()["status"], "committed")
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "feat: fake work")
        self.assertIn("aaagent-committer", self.agents())

    def test_the_setting_turns_direct_commits_off(self):
        os.environ["FAKE_OC_COMMIT_MSG"] = "feat: should not be used"
        self.set_config("git", "direct_commit", "false")
        task = self.new_task("feat setting off")
        self.implement(task)

        code, out = self.commit(task)

        self.assertEqual(code, 0, out)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "feat: fake work")
        self.assertIn("aaagent-committer", self.agents())
        self.assertIn("committed by worker1", out)

    def test_scope_and_preexisting_dirty_files_stay_out(self):
        os.environ["FAKE_OC_COMMIT_MSG"] = "feat: only work.txt"
        (self.project / "README.md").write_text("edited by user\n")
        task = self.new_task("feat scoped", "work.txt")
        self.implement(task)
        (self.project / "outside.txt").write_text("not this task\n")

        code, out = self.commit(task)

        self.assertEqual(code, 0, out)
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(),
                         ["work.txt"])
        self.assertIn("README.md", task.load()["preexisting_dirty"])
        status = self.git("status", "--porcelain")
        self.assertIn("README.md", status)
        self.assertIn("outside.txt", status)
        self.assertNotIn("work.txt", status)

    def test_commit_trailer_is_the_last_body_line(self):
        trailer = "Co-Authored-By: aaagent <noreply@example.invalid>"
        self.set_config("git", "commit_trailer", trailer)
        os.environ["FAKE_OC_COMMIT_MSG"] = "feat: with trailer\\n\\n- did it"
        task = self.new_task("feat trailer")
        self.implement(task)

        code, out = self.commit(task)

        self.assertEqual(code, 0, out)
        body = self.git("log", "-1", "--format=%b").strip().splitlines()
        self.assertEqual(body[0], "- did it")
        self.assertEqual(body[-1], trailer)

    def test_failing_pre_commit_hook_falls_back_and_stages_nothing(self):
        os.environ["FAKE_OC_COMMIT_MSG"] = "feat: the hook will fail"
        task = self.new_task("feat hook")
        self.implement(task)
        hook = self.project / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertIsNone(dispatch._direct_commit(self.project, task,
                                                      Config.load(self.project)))
        self.assertIn("could not commit", buf.getvalue())
        self.assertEqual(git_result(self.project, "diff", "--cached", "--quiet").returncode, 0,
                         "aaagent leaves nothing staged when its commit fails")

        code, out = self.commit(task)

        self.assertIn("could not commit", out)
        self.assertIn("aaagent-committer", self.agents(), "aaagent fell back to the commit job")
        self.assertNotEqual(code, 0, out)  # the hook blocks the fake committer too

    def test_worktree_task_is_committed_there_then_merged(self):
        self.set_config("parallel", "isolation", "worktree")
        os.environ["FAKE_OC_COMMIT_MSG"] = "feat: worktree task\\n\\n- write a.txt"
        task = self.new_task("feat worktree", "a.txt")
        self.implement(task)
        worktree = Path(task.load()["worktree"])
        self.assertTrue(worktree.is_dir())

        code, out = self.commit(task)

        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "committed", out)
        self.assertTrue((self.project / "a.txt").is_file())
        self.assertFalse(worktree.exists(), "the worktree is removed after the merge")
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "feat: worktree task")
        self.assertIn("a.txt", self.git("log", "-1", "--name-only"))
        self.assertNotIn("aaagent-committer", self.agents())
        self.assertIn("✓ committed by aaagent:", out)


class CommitMessageTest(unittest.TestCase):
    """`dispatch.commit_message`: parsing the latest report's `## Commit message`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.task = tasks.Task(self.project, self.project / ".aaagent" / "tasks" / "001-x")
        self.task.path.mkdir(parents=True)
        self.env_backup = dict(os.environ)
        self.addCleanup(self.restore_env)
        os.environ["AAAGENT_USER_CONFIG"] = str(Path(self.tmp.name) / "none.toml")
        os.environ.pop("FAKE_OC_COMMIT_MSG", None)

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self.env_backup)

    def message(self, text, rnd=1):
        self.task.report(rnd).write_text(text)
        return dispatch.commit_message(self.task)

    def test_a_fenced_message_drops_the_fence_lines(self):
        message = self.message("---\ntask: x\n---\n## Commit message\n```\nfeat: x\n\n"
                               "- body line\n```\n")
        self.assertEqual(message, ("feat: x", "- body line"))

    def test_a_message_without_a_body(self):
        self.assertEqual(self.message("## Commit message\nfeat: x\n"), ("feat: x", ""))

    def test_backticks_and_quotes_around_the_subject(self):
        self.assertEqual(self.message("## Commit message\n`feat: x`\n"), ("feat: x", ""))
        self.assertEqual(self.message("## Commit message\n\"feat: x\"\n"), ("feat: x", ""))

    def test_the_heading_is_case_insensitive(self):
        self.assertEqual(self.message("## commit message\nfeat: x\n"), ("feat: x", ""))

    def test_a_subject_that_is_too_long_is_refused(self):
        self.assertIsNone(self.message("## Commit message\n" + "x" * 101 + "\n"))

    def test_a_missing_section_is_refused(self):
        self.assertIsNone(self.message("## Summary\nby worker1\n"))

    def test_another_heading_ends_the_section(self):
        message = self.message("## Commit message\nfeat: x\n\n- body\n## Notes\nnot the body\n")
        self.assertEqual(message, ("feat: x", "- body"))

    def test_only_the_latest_report_counts(self):
        self.task.report(1).write_text("## Commit message\nfeat: old\n")
        self.task.report(2).write_text("## Summary\nno message this round\n")
        self.assertIsNone(dispatch.commit_message(self.task))

    def test_the_commit_trailer_is_appended(self):
        set_toml_value(project_config_path(self.project), "git", "commit_trailer", "Trailer: x")
        message = self.message("## Commit message\nfeat: x\n\n- body\n")
        self.assertEqual(message, ("feat: x", "- body\n\nTrailer: x"))


class CommitPathsTest(unittest.TestCase):
    """`gitutil.commit_paths`: scoped staging, exclusions and a clean index on failure."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.env_backup = dict(os.environ)
        self.addCleanup(self.restore_env)
        os.environ.update({
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        self.git("init", "-q")
        (self.repo / "README.md").write_text("hi\n")
        (self.repo / "keep.txt").write_text("kept\n")
        self.git("add", "README.md", "keep.txt")
        self.git("commit", "-q", "-m", "init")

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self.env_backup)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout

    def staged(self):
        return git_result(self.repo, "diff", "--cached", "--quiet").returncode != 0

    def test_only_the_declared_paths_are_committed(self):
        (self.repo / "a.txt").write_text("a\n")
        (self.repo / "b.txt").write_text("b\n")

        new = gitutil.commit_paths(self.repo, "feat: a", "", ["a.txt"])

        self.assertEqual(new, self.git("rev-parse", "HEAD").strip())
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(), ["a.txt"])
        self.assertIn("b.txt", self.git("status", "--porcelain"))
        self.assertFalse(self.staged())

    def test_excluded_paths_are_staged_then_unstaged(self):
        (self.repo / "a.txt").write_text("a\n")
        (self.repo / "keep.txt").write_text("changed\n")

        gitutil.commit_paths(self.repo, "feat: a", "", None, exclude=["keep.txt"])

        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(), ["a.txt"])
        self.assertIn("keep.txt", self.git("status", "--porcelain"))
        self.assertNotIn("keep.txt", self.git("show", "--name-only", "HEAD"))

    def test_a_declared_path_without_a_change_is_dropped(self):
        (self.repo / "a.txt").write_text("a\n")

        gitutil.commit_paths(self.repo, "feat: a", "", ["a.txt", "never.txt"])

        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(), ["a.txt"])

    def test_nothing_to_commit_raises_and_leaves_the_index_clean(self):
        with self.assertRaisesRegex(RuntimeError, "nothing to commit"):
            gitutil.commit_paths(self.repo, "feat: nothing", "", ["never.txt"])
        self.assertFalse(self.staged())

    def test_a_failing_hook_unstages_everything(self):
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        (self.repo / "a.txt").write_text("a\n")

        with self.assertRaises(RuntimeError):
            gitutil.commit_paths(self.repo, "feat: blocked", "", ["a.txt"])

        self.assertFalse(self.staged(), "a failed commit leaves nothing staged")
        self.assertIn("a.txt", self.git("status", "--porcelain"))
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "init")

    def test_the_body_becomes_the_second_paragraph(self):
        (self.repo / "a.txt").write_text("a\n")

        gitutil.commit_paths(self.repo, "feat: a", "- one\n- two", ["a.txt"])

        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "feat: a")
        self.assertEqual(self.git("log", "-1", "--format=%b").strip(), "- one\n- two")


if __name__ == "__main__":
    unittest.main()
