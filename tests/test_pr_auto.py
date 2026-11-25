"""`[git] branch_mode = "pr-auto"`: like `pr`, without the question, then a fresh chat.

`aaagent pr` in this mode pushes the branch, opens the pull request, switches back to the base
and leaves `.aaagent/new-chat-pending.json` so the chat restarts with a summary of the request.
"""

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

from aaagent import branching, cli, dispatch, session, tasks, worker  # noqa: E402
from aaagent.config import Config, project_config_path, set_toml_value  # noqa: E402

FAKE = ROOT / "tests" / "fake_opencode.py"
FAKE_GH = ROOT / "tests" / "fake_gh.py"
GH_URL = "https://github.com/example/repo/pull/1"


def plan(files=None, branch=None):
    lines = ["---", "task: x", "worker:"]
    if files is not None:
        lines.append(f"files: {files}")
    if branch is not None:
        lines.append(f"branch: {branch}")
    lines += ["---", "# Goal", "Fake work."]
    return "\n".join(lines) + "\n"


def review(verdict, rnd):
    return f"---\nverdict: {verdict}\nround: {rnd}\n---\n## Required changes\n1. more\n"


class RepoCase(unittest.TestCase):
    """A temp git project on `main`, a bare `origin`, and the fake opencode and gh."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.origin = Path(self.tmp.name) / "origin.git"
        self.gh_log = Path(self.tmp.name) / "gh.jsonl"
        self.env_backup = dict(os.environ)

        def restore():
            os.environ.clear()
            os.environ.update(self.env_backup)
        self.addCleanup(restore)
        os.environ.update({
            "AAAGENT_OPENCODE_BIN": str(FAKE),
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "AAAGENT_GH": str(FAKE_GH),
            "FAKE_GH_LOG": str(self.gh_log),
            "FAKE_GH_FAIL": "",
            "FAKE_GH_SLEEP": "",
            "FAKE_OC_FAIL": "", "FAKE_OC_NOREPORT": "",
            "FAKE_OC_REVIEW": "", "FAKE_OC_NOREVIEW": "",
            "AAAGENT_PROJECT": str(self.project),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        self.git("init", "-q", "-b", "main")
        (self.project / "README.md").write_text("hi\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "init")
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        self.git("remote", "add", "origin", str(self.origin))
        session.init_project(self.project, Config.load(self.project))
        self.cfg = Config.load(self.project)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.project, check=True,
                              capture_output=True, text=True).stdout

    def write_mode(self, mode):
        set_toml_value(project_config_path(self.project), "git", "branch_mode", mode)
        self.cfg = Config.load(self.project)

    def new_task(self, title, files=None, branch=None) -> tasks.Task:
        task = tasks.create(self.project, title)
        task.plan.write_text(plan(files, branch))
        return task

    def calls(self, path: Path) -> list:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def origin_branches(self) -> list[str]:
        out = subprocess.run(["git", "branch", "--list", "--format=%(refname:short)"],
                             cwd=self.origin, check=True, capture_output=True, text=True).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]

    def marker(self) -> dict:
        path = branching.new_chat_pending_file(self.project)
        self.assertTrue(path.is_file(), "the new-chat marker was not written")
        return json.loads(path.read_text())


class FlowCase(RepoCase):
    """RepoCase plus dispatch helpers and worker cleanup."""

    def tearDown(self):
        for name in ("worker1", "worker2"):
            worker.stop(self.project, name)
        for _ in range(50):
            if not any(worker.worker_pid(self.project, n) for n in ("worker1", "worker2")):
                break
            time.sleep(0.1)

    def run_dispatch(self, task) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = dispatch.dispatch(self.project, task.id)
        return code, buf.getvalue()

    def implement(self, task) -> str:
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "awaiting_review", out)
        task.review(1).write_text(review("approved", 1))
        return out

    def commit(self, task) -> str:
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "committed", out)
        return out


class PrAutoModeTest(FlowCase):
    def test_dispatching_a_task_creates_the_session_branch(self):
        self.write_mode("pr-auto")
        task = self.new_task("fix login crash", "alpha.txt")

        out = self.implement(task)
        self.assertIn("created branch fix/login-crash from main (branch_mode = pr-auto)", out)
        self.assertEqual(self.git("branch", "--show-current").strip(), "fix/login-crash")
        self.assertEqual(branching.open_batch(self.project)["tasks"], [task.id])
        self.assertFalse(branching.new_chat_pending_file(self.project).exists(),
                         "no chat restart before the pull request")

    def test_on_idle_tells_the_planner_to_run_pr_without_asking(self):
        self.write_mode("pr-auto")
        task = self.new_task("fix alpha", "alpha.txt")
        self.implement(task)
        out = self.commit(task)

        self.assertIn("All tasks on branch fix/alpha are done (1 commit(s) since main).", out)
        self.assertIn("branch_mode is pr-auto", out)
        self.assertIn("`aaagent pr`", out)
        self.assertNotIn("Ask the user whether to open a pull request", out)
        self.assertEqual(self.calls(self.gh_log), [], "gh is not called from dispatch")
        self.assertTrue(branching.batch_file(self.project).exists(), "the branch stays open")
        self.assertIsNone(branching.last_closed(self.project))
        self.assertEqual(self.git("branch", "--show-current").strip(), "fix/alpha")

    def test_pr_opens_the_pull_request_and_requests_a_new_chat(self):
        self.write_mode("pr-auto")
        task = self.new_task("fix alpha", "alpha.txt")
        self.implement(task)
        self.commit(task)

        lines = branching.retry_pr(self.project, self.cfg)
        self.assertEqual(lines[:2], [f"pull request: {GH_URL}", "switched back to main"])
        self.assertTrue(any("new chat" in line for line in lines), lines)
        self.assertIn("fix/alpha", self.origin_branches())
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")
        self.assertFalse(branching.batch_file(self.project).exists())
        closed = branching.last_closed(self.project)
        self.assertEqual(closed["pr_url"], GH_URL)
        self.assertIsNone(closed["pr_error"])

        data = self.marker()
        self.assertEqual(set(data), {"reason", "branch", "base", "pr_url", "pr_error",
                                     "prompt", "at"})
        self.assertEqual(data["reason"], "pull-request")
        self.assertEqual(data["branch"], "fix/alpha")
        self.assertEqual(data["base"], "main")
        self.assertEqual(data["pr_url"], GH_URL)
        self.assertIsNone(data["pr_error"])
        self.assertTrue(data["at"])
        prompt = data["prompt"]
        self.assertIn("aaagent started this chat automatically", prompt)
        self.assertIn(GH_URL, prompt)
        self.assertIn("feat: fake work", prompt)
        self.assertIn("switched back to main", prompt)
        self.assertIn("wait for my next request", prompt)
        self.assertNotIn("`aaagent pr`", prompt)

    def test_a_failed_pull_request_still_requests_a_new_chat(self):
        self.write_mode("pr-auto")
        task = self.new_task("fix alpha", "alpha.txt")
        self.implement(task)
        self.commit(task)
        self.git("remote", "remove", "origin")

        lines = branching.retry_pr(self.project, self.cfg)
        self.assertTrue(any("could not push" in line for line in lines), lines)
        self.assertIn("switched back to main", lines)
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")

        data = self.marker()
        self.assertEqual(data["reason"], "pull-request")
        self.assertEqual(data["branch"], "fix/alpha")
        self.assertEqual(data["base"], "main")
        self.assertIsNone(data["pr_url"])
        self.assertTrue(data["pr_error"])
        prompt = data["prompt"]
        self.assertIn("aaagent started this chat automatically", prompt)
        self.assertIn("was finished, but the pull request could not be opened:", prompt)
        self.assertIn(str(data["pr_error"]), prompt)
        self.assertIn("switched back to main", prompt)
        self.assertIn("`aaagent pr`", prompt)
        self.assertIn("wait for my next request", prompt)

    def test_a_zero_commit_batch_requests_a_new_chat_with_a_clear_error(self):
        self.write_mode("pr-auto")
        task = self.new_task("fix empty", "empty.txt")
        self.assertEqual(branching.start_if_needed(self.project, task, self.cfg),
                         "created branch fix/empty from main (branch_mode = pr-auto)")

        lines = branching.retry_pr(self.project, self.cfg)
        self.assertIn("branch fix/empty has no commits; no pull request opened", lines)
        self.assertIn("switched back to main", lines)
        data = self.marker()
        self.assertIsNone(data["pr_url"])
        self.assertIsNone(data["pr_error"])
        self.assertIn("the branch had no commits", data["prompt"])
        self.assertNotIn("None", data["prompt"])

    def test_plain_pr_mode_writes_no_marker(self):
        self.write_mode("pr")
        task = self.new_task("fix alpha", "alpha.txt")
        self.implement(task)
        self.commit(task)

        lines = branching.retry_pr(self.project, self.cfg)
        self.assertIn(f"pull request: {GH_URL}", lines)
        self.assertFalse(branching.new_chat_pending_file(self.project).exists())

    def test_retrying_a_closed_batch_writes_no_marker(self):
        self.write_mode("pr-auto")
        task = self.new_task("fix alpha", "alpha.txt")
        self.implement(task)
        self.commit(task)

        branching.retry_pr(self.project, self.cfg)
        marker = branching.new_chat_pending_file(self.project)
        self.assertTrue(marker.is_file())
        marker.unlink()

        self.assertIsNone(branching.open_batch(self.project))
        lines = branching.retry_pr(self.project, self.cfg)
        self.assertEqual(lines, [f"pull request: {GH_URL}"])
        self.assertFalse(branching.new_chat_pending_file(self.project).exists(),
                         "a retried, already closed batch does not restart the chat")


class PrAutoConfigTest(RepoCase):
    def test_change_mode_accepts_pr_auto(self):
        self.assertEqual(branching.change_mode(self.project, "pr-auto"),
                         ["branch mode: pr-auto"])
        self.assertEqual(Config.load(self.project).branch_mode, "pr-auto")
        self.write_mode("pr-auto")
        self.assertEqual(self.cfg.branch_mode, "pr-auto")

    def test_the_branch_mode_parser_accepts_pr_auto(self):
        args = cli.build_parser().parse_args(["branch-mode", "pr-auto"])
        self.assertEqual(args.mode, "pr-auto")
        self.assertEqual(args.func, cli.cmd_branch_mode)


if __name__ == "__main__":
    unittest.main()
