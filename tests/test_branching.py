"""One session branch: `[git] branch_mode = current | branch | pr`."""

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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
os.environ.setdefault("AAAGENT_POLL_SECONDS", "0.05")

from aaagent import branching, dispatch, session, status, tasks, worker  # noqa: E402
from aaagent.config import Config, project_config_path, set_toml_value  # noqa: E402
from aaagent.tasks import FlowError  # noqa: E402

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

    def make_task(self, task_id, files=None, branch=None) -> tasks.Task:
        """A task with a chosen id, for the branch-name rules."""
        path = tasks.tasks_dir(self.project) / task_id
        path.mkdir(parents=True)
        task = tasks.Task(self.project, path)
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

    def implement(self, task, approved=True) -> str:
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "awaiting_review", out)
        if approved:
            task.review(1).write_text(review("approved", 1))
        return out

    def commit(self, task) -> str:
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "committed", out)
        return out


class BranchNameTest(RepoCase):
    def test_the_slug_picks_a_conventional_prefix(self):
        cases = {
            "021-fix-login-crash": "fix/login-crash",
            "022-branch-mode-tui": "feat/branch-mode-tui",
            "023-docs": "docs/023",
            "024-bugfix-crash": "fix/crash",
            "025-hotfix-timeout": "fix/timeout",
            "026-tests-flow": "test/flow",
            "027-chore-bump": "chore/bump",
            "028-refactor-dispatch-py": "refactor/dispatch-py",
            "029-feature-search": "feat/search",
        }
        for task_id, expected in cases.items():
            self.assertEqual(branching.branch_name(self.make_task(task_id)), expected)

    def test_the_plans_branch_wins(self):
        task = self.make_task("030-some-work", branch="feat/custom-name")
        self.assertEqual(branching.branch_name(task), "feat/custom-name")
        self.assertEqual(branching.branch_name(self.make_task("031-x", branch="fix/x-y")), "fix/x-y")

    def test_an_invalid_plan_branch_falls_back(self):
        task = self.make_task("032-fix-thing", branch="bad..name")
        self.assertEqual(branching.branch_name(task), "feat/fix-thing")

    def test_unique_branch_adds_a_suffix(self):
        self.assertEqual(branching.unique_branch(self.project, "feat/x"), "feat/x")
        self.git("branch", "feat/x")
        self.assertEqual(branching.unique_branch(self.project, "feat/x"), "feat/x-2")
        self.git("branch", "feat/x-2")
        self.assertEqual(branching.unique_branch(self.project, "feat/x"), "feat/x-3")


class CurrentModeTest(FlowCase):
    def test_current_mode_stays_on_the_branch_and_opens_no_batch(self):
        task = self.new_task("fix login crash")
        self.assertEqual(self.cfg.branch_mode, "current")
        self.implement(task)
        self.commit(task)
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")
        self.assertFalse(branching.batch_file(self.project).exists())
        self.assertIsNone(branching.last_closed(self.project))
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(), "feat: fake work")
        self.assertEqual(self.calls(self.gh_log), [])


class BranchModeTest(FlowCase):
    def test_branch_mode_commits_on_a_conventional_branch(self):
        self.write_mode("branch")
        task = self.new_task("fix login crash")
        main_head = self.git("rev-parse", "main").strip()

        out = self.implement(task)
        self.assertIn("created branch fix/login-crash from main (branch_mode = branch)", out)
        self.assertEqual(self.git("branch", "--show-current").strip(), "fix/login-crash")
        self.assertEqual(branching.open_batch(self.project)["tasks"], [task.id])

        out = self.commit(task)
        self.assertEqual(self.git("branch", "--show-current").strip(), "fix/login-crash",
                         "the checkout stays on the branch until it is finished")
        self.assertIn("All tasks on branch fix/login-crash are done (1 commit(s) since main).", out)
        self.assertIn("`aaagent branch-done`", out)
        self.assertTrue(branching.batch_file(self.project).exists(), "the branch is still open")
        self.assertIsNone(branching.last_closed(self.project))
        self.assertEqual(self.git("rev-parse", "main").strip(), main_head, "main is unchanged")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), task.load()["commit"])
        self.assertEqual(self.calls(self.gh_log), [], "branch mode never calls gh")

        lines = branching.finish(self.project, self.cfg, push=False)
        self.assertEqual(lines, ["branch fix/login-crash finished (1 commit(s)); not pushed",
                                 "switched back to main"])
        self.assertFalse(branching.batch_file(self.project).exists())
        closed = branching.last_closed(self.project)
        self.assertEqual(closed["branch"], "fix/login-crash")
        self.assertEqual(closed["base"], "main")
        self.assertEqual(len(closed["commits"]), 1)
        self.assertIn("fake work", closed["commits"][0])
        self.assertIsNone(closed["pr_url"])
        self.assertIsNone(closed["pr_error"])
        self.assertTrue(closed["switched_back"])
        self.assertEqual(len(list(branching.batches_dir(self.project).glob("*.json"))), 1)
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")
        self.assertEqual(self.calls(self.gh_log), [])

    def test_git_status_counts_the_commits_and_reports_idle(self):
        self.write_mode("branch")
        one = self.new_task("fix alpha", "alpha.txt")
        two = self.new_task("fix beta", "beta.txt")
        self.implement(one)
        self.implement(two)
        batch = branching.git_status(self.project, self.cfg)["batch"]
        self.assertEqual(batch["commits"], 0)
        self.assertFalse(batch["idle"], "tasks are still active")

        self.commit(one)
        batch = branching.git_status(self.project, self.cfg)["batch"]
        self.assertEqual(batch["commits"], 1)
        self.assertFalse(batch["idle"])

        self.commit(two)
        batch = branching.git_status(self.project, self.cfg)["batch"]
        self.assertEqual(batch["commits"], 2)
        self.assertTrue(batch["idle"])


class SessionBranchTest(FlowCase):
    """One branch per chat session: the tasks keep joining it until it is finished."""

    def test_a_second_task_after_a_commit_joins_the_same_branch(self):
        self.write_mode("branch")
        one = self.new_task("fix alpha", "alpha.txt")
        two = self.new_task("fix beta", "beta.txt")
        self.implement(one)
        out = self.commit(one)
        self.assertIn("All tasks on branch fix/alpha are done", out)
        self.assertEqual(branching.open_batch(self.project)["tasks"], [one.id])

        out = self.implement(two)
        self.assertIn(f"{two.id} works on fix/alpha", out)
        self.assertEqual(branching.open_batch(self.project)["branch"], "fix/alpha")
        self.assertEqual(branching.open_batch(self.project)["tasks"], [one.id, two.id])
        branches = self.git("branch", "--list", "--format=%(refname:short)").split()
        self.assertEqual([name for name in branches if name.startswith("fix/")], ["fix/alpha"],
                         "no new branch for the second task")
        self.assertEqual(self.git("branch", "--show-current").strip(), "fix/alpha")

        self.commit(two)
        self.assertEqual(branching.git_status(self.project, self.cfg)["batch"]["commits"], 2)

    def test_finish_refuses_while_a_task_is_still_active(self):
        self.write_mode("branch")
        task = self.new_task("fix active", "alpha.txt")
        branching.start_if_needed(self.project, task, self.cfg)
        tasks.claim_active(self.project, task)
        with self.assertRaisesRegex(FlowError, f"tasks are still active on fix/active: {task.id}"):
            branching.finish(self.project, self.cfg, push=False)
        self.assertIsNotNone(branching.open_batch(self.project))
        tasks.release_active(self.project, task)

    def test_finish_refuses_when_no_branch_is_open(self):
        with self.assertRaisesRegex(FlowError, "no open branch to finish"):
            branching.finish(self.project, self.cfg, push=False)

    def test_finish_stays_on_the_branch_when_the_tree_is_dirty(self):
        self.write_mode("branch")
        task = self.new_task("fix dirty", "dirty.txt")
        self.implement(task)
        self.commit(task)
        (self.project / "README.md").write_text("changed\n")

        lines = branching.finish(self.project, self.cfg, push=False)
        self.assertEqual(lines, ["branch fix/dirty finished (1 commit(s)); not pushed",
                                 "stayed on fix/dirty: uncommitted changes in README.md; "
                                 "run `git switch main` yourself"])
        self.assertEqual(self.git("branch", "--show-current").strip(), "fix/dirty")
        self.assertFalse(branching.last_closed(self.project)["switched_back"])

    def test_finish_switches_back_with_an_untracked_file(self):
        self.write_mode("branch")
        task = self.new_task("fix stray", "stray.txt")
        self.implement(task)
        self.commit(task)
        (self.project / "untracked.txt").write_text("kept\n")

        lines = branching.finish(self.project, self.cfg, push=False)
        self.assertEqual(lines, ["branch fix/stray finished (1 commit(s)); not pushed",
                                 "switched back to main"])
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")
        self.assertEqual((self.project / "untracked.txt").read_text(), "kept\n",
                         "git switch carries the untracked file over")

    def test_the_prompt_tells_a_new_chat_about_the_open_branch(self):
        self.write_mode("branch")
        task = self.new_task("fix prompt", "prompt.txt")
        self.assertNotIn("Open branch from an earlier chat",
                         session.orchestrator_prompt(self.cfg, self.project))
        branching.start_if_needed(self.project, task, self.cfg)
        prompt = session.orchestrator_prompt(self.cfg, self.project)
        self.assertIn("## Open branch from an earlier chat", prompt)
        self.assertIn("fix/prompt", prompt)
        self.assertIn("0 commit(s)", prompt)
        self.assertIn(task.id, prompt)

        branching.finish(self.project, self.cfg, push=False)
        self.assertNotIn("Open branch from an earlier chat",
                         session.orchestrator_prompt(self.cfg, self.project))


class PrModeTest(FlowCase):
    def test_nothing_is_pushed_after_the_last_task_is_committed(self):
        self.write_mode("pr")
        one = self.new_task("fix alpha", "alpha.txt")
        two = self.new_task("fix beta", "beta.txt")

        self.implement(one)
        out = self.implement(two)
        batch = branching.open_batch(self.project)
        self.assertEqual(batch["branch"], "fix/alpha")
        self.assertEqual(batch["tasks"], [one.id, two.id])
        self.assertEqual(batch["base"], "main")
        self.assertIn(f"{two.id} works on fix/alpha", out)

        self.commit(one)
        self.assertIsNone(branching.last_closed(self.project), "the branch is still open")
        self.assertEqual(self.calls(self.gh_log), [], "no pull request before the last commit")
        self.assertEqual(branching.open_batch(self.project)["tasks"], [one.id, two.id])

        out = self.commit(two)
        self.assertIn("All tasks on branch fix/alpha are done (2 commit(s) since main).", out)
        self.assertIn("Ask the user whether to open a pull request into main now.", out)
        self.assertIn("`aaagent pr`", out)
        self.assertEqual(self.calls(self.gh_log), [], "nothing is pushed without `aaagent pr`")
        self.assertNotIn("fix/alpha", self.origin_branches(), "the branch was not pushed")
        self.assertTrue(branching.batch_file(self.project).exists())
        self.assertIsNone(branching.last_closed(self.project))
        self.assertEqual(self.git("branch", "--show-current").strip(), "fix/alpha")

    def test_pr_finishes_the_idle_branch_and_switches_back(self):
        self.write_mode("pr")
        one = self.new_task("fix alpha", "alpha.txt")
        two = self.new_task("fix beta", "beta.txt")
        self.implement(one)
        self.implement(two)
        self.commit(one)
        self.commit(two)
        commits = self.git("log", "--format=%h", "main..fix/alpha").split()
        self.assertEqual(len(commits), 2)
        self.assertEqual(self.calls(self.gh_log), [])

        lines = branching.retry_pr(self.project, self.cfg)
        self.assertEqual(lines, [f"pull request: {GH_URL}", "switched back to main"])
        closed = branching.last_closed(self.project)
        self.assertIsNotNone(closed)
        self.assertFalse(branching.batch_file(self.project).exists())
        self.assertEqual(len(list(branching.batches_dir(self.project).glob("*.json"))), 1)
        self.assertEqual(closed["branch"], "fix/alpha")
        self.assertEqual(closed["tasks"], [one.id, two.id])
        self.assertEqual(len(closed["commits"]), 2)
        self.assertEqual(closed["pr_url"], GH_URL)
        self.assertIsNone(closed["pr_error"])
        self.assertTrue(closed["switched_back"])
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")
        self.assertIn("fix/alpha", self.origin_branches(), "the branch was pushed to origin")
        gh_calls = self.calls(self.gh_log)
        self.assertEqual(len(gh_calls), 1, "one pull request for the branch")
        call = gh_calls[0]
        self.assertEqual(call[:2], ["pr", "create"])
        self.assertEqual(call[call.index("--base") + 1], "main")
        self.assertEqual(call[call.index("--head") + 1], "fix/alpha")
        body = call[call.index("--body") + 1]
        for sha in commits:
            self.assertIn(sha, body, "the pull request body lists both commits")
        self.assertIn(one.id, body)
        self.assertIn(two.id, body)

    def test_a_failed_pull_request_is_recorded_and_retried(self):
        self.write_mode("pr")
        task = self.new_task("feat gamma", "gamma.txt")
        self.implement(task)
        out = self.commit(task)
        self.assertIn("`aaagent pr`", out)

        os.environ["FAKE_GH_FAIL"] = "1"
        lines = branching.retry_pr(self.project, self.cfg)
        self.assertIn("run `aaagent pr` to retry", lines)
        closed = branching.last_closed(self.project)
        self.assertIsNone(closed["pr_url"])
        self.assertIn("could not create pull request", closed["pr_error"])
        self.assertEqual(self.git("branch", "--show-current").strip(), "main",
                         "the checkout still goes back to the base branch")

        os.environ.pop("FAKE_GH_FAIL", None)
        lines = branching.retry_pr(self.project, self.cfg)
        self.assertEqual(lines, [f"pull request: {GH_URL}"])
        closed = branching.last_closed(self.project)
        self.assertEqual(closed["pr_url"], GH_URL)
        self.assertIsNone(closed["pr_error"])

    def test_retry_pr_with_nothing_closed_says_so(self):
        self.assertEqual(branching.retry_pr(self.project, self.cfg),
                         ["no finished branch to open a pull request for"])

    def test_pr_switched_on_mid_branch_asks_at_its_end(self):
        self.write_mode("branch")
        task = self.new_task("feat epsilon", "epsilon.txt")
        self.implement(task)
        self.write_mode("pr")
        out = self.commit(task)
        self.assertIn("`aaagent pr`", out)
        self.assertEqual(self.calls(self.gh_log), [], "branch mode never pushes")

        branching.retry_pr(self.project, self.cfg)
        self.assertEqual(branching.last_closed(self.project)["pr_url"], GH_URL)
        self.assertIn("feat/epsilon", self.origin_branches())
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")


class TimeoutTest(FlowCase):
    def test_a_hung_gh_times_out_and_is_recorded(self):
        self.git("switch", "-q", "-c", "feat/slow")
        (self.project / "slow.txt").write_text("slow\n")
        self.git("add", "slow.txt")
        self.git("commit", "-q", "-m", "feat: slow work")
        record = {"branch": "feat/slow", "base": "main", "start_head": None, "mode": "pr",
                  "tasks": ["001-slow"],
                  "commits": [self.git("log", "-1", "--format=%h %s").strip()],
                  "pr_url": None, "pr_error": None}
        os.environ["FAKE_GH_SLEEP"] = "10"
        with mock.patch.object(branching, "GH_TIMEOUT_SECONDS", 0.3):
            lines = branching.open_pr(self.project, record)
        self.assertIsNone(record["pr_url"])
        self.assertIn("timed out", record["pr_error"])
        self.assertIn("0.3 s", record["pr_error"])
        self.assertTrue(any("run `aaagent pr` to retry" in line for line in lines), lines)
        self.assertIn("feat/slow", self.origin_branches(), "the push still ran")

    def test_the_network_call_runs_outside_the_active_task_lock(self):
        """open_pr must not run under the project-wide active-task flock."""
        self.write_mode("pr")
        task = self.new_task("feat zeta", "zeta.txt")
        self.implement(task)                      # opens the branch and claims the task
        other = self.new_task("feat eta", "eta.txt")
        self.commit(task)                         # the branch stays open, with one commit
        depths = []
        real_open_pr = branching.open_pr

        def spy_open_pr(project, record):
            depths.append(getattr(tasks._lock_state, "depth", 0))
            # Another dispatch could claim now; in this thread a nested claim is allowed too.
            tasks.claim_active(project, other)
            return real_open_pr(project, record)

        with mock.patch.object(branching, "open_pr", side_effect=spy_open_pr):
            lines = branching.retry_pr(self.project, self.cfg)
        self.assertEqual(depths, [0], "open_pr runs with no active-task lock held")
        self.assertEqual(tasks.active_tasks(self.project), [other.id])
        self.assertEqual(branching.last_closed(self.project)["pr_url"], GH_URL)
        self.assertIn(f"pull request: {GH_URL}", lines)


class JoinTest(FlowCase):
    def test_a_refused_task_never_joins_the_batch(self):
        self.write_mode("branch")
        one = self.new_task("fix alpha", "alpha.txt")
        third = self.new_task("fix alpha again", "alpha.txt, gamma.txt")
        self.implement(one)
        self.assertEqual(branching.open_batch(self.project)["tasks"], [one.id])

        with self.assertRaisesRegex(FlowError, "alpha.txt overlaps alpha.txt"):
            self.run_dispatch(third)
        self.assertEqual(branching.open_batch(self.project)["tasks"], [one.id])
        self.assertEqual(tasks.active_tasks(self.project), [one.id])


class AbandonTest(RepoCase):
    def test_abandoning_the_only_task_deletes_the_empty_branch(self):
        self.write_mode("branch")
        task = self.new_task("fix login crash", "alpha.txt")
        branching.start_if_needed(self.project, task, self.cfg)
        tasks.claim_active(self.project, task)
        text = dispatch.abandon(self.project, task.id)
        self.assertIn("branch fix/login-crash had no commits; switched back to main and deleted it",
                      text)
        self.assertFalse(branching.batch_file(self.project).exists())
        self.assertEqual(branching.last_closed(self.project)["branch"], "fix/login-crash")
        self.assertEqual(self.git("branch", "--show-current").strip(), "main")
        self.assertNotIn("fix/login-crash",
                         self.git("branch", "--list", "--format=%(refname:short)"))


class DetachedHeadTest(RepoCase):
    def test_a_detached_head_refuses_to_start_a_branch(self):
        self.write_mode("branch")
        task = self.new_task("feat delta", "delta.txt")
        self.git("checkout", "-q", "--detach", "HEAD")
        with self.assertRaisesRegex(FlowError, "HEAD is detached"):
            branching.start_if_needed(self.project, task, self.cfg)
        self.assertIsNone(branching.open_batch(self.project))

    def test_a_failed_branch_creation_releases_the_claim(self):
        self.write_mode("branch")
        task = self.new_task("feat delta", "delta.txt")
        self.git("checkout", "-q", "--detach", "HEAD")
        with self.assertRaisesRegex(FlowError, "HEAD is detached"):
            dispatch.dispatch(self.project, task.id)
        self.assertEqual(tasks.active_tasks(self.project), [])
        self.assertIsNone(branching.open_batch(self.project))

    def test_a_plan_branch_is_ignored_when_a_batch_is_open(self):
        self.write_mode("branch")
        one = self.new_task("fix alpha", "alpha.txt")
        two = self.new_task("fix beta", "beta.txt", branch="feat/other")
        branching.start_if_needed(self.project, one, self.cfg)
        line = branching.start_if_needed(self.project, two, self.cfg)
        self.assertIn("joins the open branch fix/alpha", line)
        self.assertIn("its plan's branch: feat/other is ignored", line)
        self.assertEqual(branching.open_batch(self.project)["tasks"], [one.id, two.id])


class GitStatusTest(RepoCase):
    def test_status_data_has_the_git_entry(self):
        self.write_mode("branch")
        data = status.status_data(self.project, self.cfg)
        self.assertEqual(data["git"]["mode"], "branch")
        self.assertEqual(data["git"]["branch"], "main")
        self.assertIsNone(data["git"]["batch"])
        self.assertIsNone(data["git"]["last_pr"])

    def test_status_data_shows_the_open_batch(self):
        self.write_mode("branch")
        task = self.new_task("fix login crash", "alpha.txt")
        branching.start_if_needed(self.project, task, self.cfg)
        data = status.status_data(self.project, self.cfg)
        self.assertEqual(data["git"]["branch"], "fix/login-crash")
        self.assertEqual(data["git"]["batch"],
                         {"branch": "fix/login-crash", "base": "main", "tasks": [task.id],
                          "commits": 0, "idle": True})


if __name__ == "__main__":
    unittest.main()
