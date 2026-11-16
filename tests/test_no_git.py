"""No Git mode: `[git] branch_mode = "none"`.

The project folder is not a git repository (or must not be touched): aaagent keeps its own
history in `.aaagent/shadow.git`, with the project folder as its work tree, and an approved
task becomes a checkpoint commit there instead of a worker commit job.
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import branching, dispatch, gitutil, status, tasks, worker  # noqa: E402
from aaagent.config import Config, project_config_path, set_toml_value  # noqa: E402
from aaagent.tasks import FlowError, Step  # noqa: E402

FAKE = ROOT / "tests" / "fake_opencode.py"
AAAGENT = ROOT / "bin" / "aaagent"
DS = "opencode-go/deepseek-v4.1-flash"

PLAN = """---
task: x
worker:
---
# Goal
Append the model name to work.txt.
"""


def review(verdict: str, rnd: int) -> str:
    return f"---\nverdict: {verdict}\nround: {rnd}\n---\n## Required changes\n1. more\n"


class EnvCase(unittest.TestCase):
    """A temp project folder, the fake opencode and a missing user config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.args_log = Path(self.tmp.name) / "args.jsonl"
        self.env_backup = dict(os.environ)

        def restore():
            os.environ.clear()
            os.environ.update(self.env_backup)
        self.addCleanup(restore)
        os.environ.update({
            "AAAGENT_OPENCODE_BIN": str(FAKE),
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "FAKE_OC_ARGS_LOG": str(self.args_log),
            "FAKE_OC_FAIL": "", "FAKE_OC_NOREPORT": "", "FAKE_OC_REVIEW": "",
            "AAAGENT_PROJECT": str(self.project),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        os.environ.pop("GIT_DIR", None)
        os.environ.pop("GIT_WORK_TREE", None)
        os.environ.pop("GIT_INDEX_FILE", None)

    # -- helpers -------------------------------------------------------

    def write_mode(self, mode) -> None:
        set_toml_value(project_config_path(self.project), "git", "branch_mode", mode)

    def git(self, *args) -> str:
        """A git call about the real repository of a test project."""
        return subprocess.run(["git", *args], cwd=self.project, check=True,
                              capture_output=True, text=True).stdout

    def shadow(self, *args) -> str:
        """A git call about the project's shadow repository."""
        return subprocess.run(["git", "--git-dir", str(gitutil.shadow_dir(self.project)),
                               "--work-tree", str(self.project), *args],
                              check=True, capture_output=True, text=True).stdout

    def new_task(self, title="add work") -> tasks.Task:
        task = tasks.create(self.project, title)
        task.plan.write_text(PLAN)
        return task

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

    def agents(self) -> list[str]:
        if not self.args_log.exists():
            return []
        calls = [json.loads(line) for line in self.args_log.read_text().splitlines() if line.strip()]
        return [call[call.index("--agent") + 1] for call in calls if "--agent" in call]

    def log_names(self, task: tasks.Task) -> list[str]:
        return [p.name for p in task.logs.iterdir()] if task.logs.is_dir() else []

    def aaagent(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(AAAGENT), *args], cwd=self.project,
                              capture_output=True, text=True)


class NoGitCase(EnvCase):
    """A temp folder without `.git`, configured as No Git."""

    def setUp(self):
        super().setUp()
        self.write_mode("none")
        (self.project / "README.md").write_text("hi\n")

    def tearDown(self):
        for name in ("worker1", "worker2"):
            worker.stop(self.project, name)
        for _ in range(50):
            if not any(worker.worker_pid(self.project, n) for n in ("worker1", "worker2")):
                break
            time.sleep(0.1)


class RealRepoCase(EnvCase):
    """A temp git repository on `main`; tests switch it to No Git themselves."""

    def setUp(self):
        super().setUp()
        self.git("init", "-q", "-b", "main")
        (self.project / "README.md").write_text("hi\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "init")

    def tearDown(self):
        for name in ("worker1", "worker2"):
            worker.stop(self.project, name)
        for _ in range(50):
            if not any(worker.worker_pid(self.project, n) for n in ("worker1", "worker2")):
                break
            time.sleep(0.1)


class FullFlowTest(NoGitCase):
    def test_an_approved_task_becomes_a_checkpoint(self):
        task = self.new_task()
        out = self.implement(task)
        self.assertIn("work.txt", out)
        out = self.commit(task)

        state = task.load()
        commit = state["commit"]
        self.assertEqual(state["status"], "committed")
        self.assertIn(f"✓ checkpoint {commit[:7]} recorded for {task.id}", out)
        self.assertIn("no git: aaagent's own history in .aaagent/shadow.git", out)
        self.assertNotIn("commit round 1 →", out, "no worker commit job in No Git mode")
        self.assertEqual(self.agents(), ["aaagent-worker"], "the committer never ran")
        self.assertFalse([name for name in self.log_names(task) if "commit" in name])

        self.assertFalse((self.project / ".git").exists(), "no repository is created")
        self.assertEqual(self.shadow("rev-parse", "HEAD").strip(), commit)
        changed = self.shadow("show", "--stat", "--format=", commit)
        self.assertIn("work.txt", changed)
        self.assertIn(DS, self.shadow("show", "--format=", commit))
        self.assertEqual(tasks.active_tasks(self.project), [])

    def test_changes_made_by_hand_get_their_own_checkpoint(self):
        task = self.new_task()
        (self.project / "README.md").write_text("edited by hand\n")
        (self.project / "notes.txt").write_text("mine\n")

        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["preexisting_dirty"], [])

        subjects = self.shadow("log", "--format=%s").splitlines()
        self.assertEqual(subjects[0], "aaagent: changes made outside aaagent")
        self.assertIn("aaagent: starting point", subjects, "the baseline comes first")

        task.review(1).write_text(review("approved", 1))
        self.commit(task)
        stat = self.shadow("show", "--stat", "--format=", task.load()["commit"])
        self.assertIn("work.txt", stat)
        self.assertNotIn("README.md", stat, "the hand edit is in the earlier checkpoint")
        self.assertNotIn("notes.txt", stat)

    def test_a_scoped_task_whose_file_never_appeared_still_gets_a_checkpoint(self):
        task = tasks.create(self.project, "add work")
        task.plan.write_text(PLAN.replace("worker:\n---", "worker:\nfiles: work.txt, missing.txt\n---"))
        self.assertEqual(task.pathspecs(), ["work.txt", "missing.txt"])
        self.implement(task)
        self.commit(task)
        stat = self.shadow("show", "--stat", "--format=", task.load()["commit"])
        self.assertIn("work.txt", stat)

    def test_a_failed_checkpoint_stops_the_task_and_the_next_dispatch_retries(self):
        task = self.new_task()
        self.implement(task)

        def failing(*args, **kwargs):
            raise RuntimeError("git commit failed: boom")

        with mock.patch.object(gitutil, "checkpoint", side_effect=failing):
            code, out = self.run_dispatch(task)
        self.assertEqual(code, 1, out)
        self.assertIn("✗ checkpoint failed: git commit failed: boom", out)
        self.assertIn(f"Task {task.id} is stopped. Fix the problem, then "
                      f"`aaagent dispatch {task.id}` again, or tell the user.", out)
        state = task.load()
        self.assertEqual(state["status"], "failed")
        self.assertIsNone(state["current_job"])
        self.assertEqual(state["history"][-1]["event"], "checkpoint failed")
        self.assertEqual(state["history"][-1]["error"], "git commit failed: boom")
        self.assertEqual(tasks.active_tasks(self.project), [task.id], "the task stays claimed")

        step = tasks.next_step(task, Config.load(self.project))
        self.assertEqual(step.phase, "commit", "the next dispatch tries the checkpoint again")

        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "committed")
        self.assertEqual(tasks.active_tasks(self.project), [])


class CheckpointTest(NoGitCase):
    def test_an_untracked_file_in_a_new_directory_is_committed(self):
        gitutil.ensure_shadow(self.project)
        (self.project / "b.txt").write_text("b\n")
        (self.project / "sub" / "d").mkdir(parents=True)
        (self.project / "sub" / "d" / "x.txt").write_text("x\n")

        commit = gitutil.checkpoint(self.project, "t", ["sub/d/x.txt"], exclude=["b.txt"])

        changed = self.shadow("show", "--name-only", "--format=", commit).split()
        self.assertEqual(changed, ["sub/d/x.txt"])
        self.assertEqual(gitutil.dirty_paths(self.project), ["b.txt"],
                         "the excluded file stays dirty")
        self.assertEqual(gitutil.still_dirty(self.project, ["sub/d/x.txt"], ["b.txt"]), [])
        self.assertEqual(gitutil.still_dirty(self.project, None), ["b.txt"])
        self.assertEqual(gitutil.still_dirty(self.project, None, ["b.txt"]), [])
        self.assertNotIn(".aaagent", self.shadow("ls-files"))


class GitEnvTest(NoGitCase):
    def test_git_env_points_into_the_shadow_repository(self):
        env = gitutil.git_env(self.project)
        self.assertEqual(env["GIT_DIR"], str(gitutil.shadow_dir(self.project)))
        self.assertEqual(env["GIT_WORK_TREE"], str(self.project))
        self.assertTrue((gitutil.shadow_dir(self.project) / "HEAD").is_file())
        self.assertEqual(gitutil.head(self.project), self.shadow("rev-parse", "HEAD").strip())

    def test_a_leaked_git_dir_does_not_change_the_shadow(self):
        os.environ["GIT_DIR"] = str(Path(self.tmp.name) / "elsewhere.git")
        env = gitutil.git_env(self.project)
        self.assertEqual(env["GIT_DIR"], str(gitutil.shadow_dir(self.project)))
        self.assertEqual(gitutil.head(self.project), self.shadow("rev-parse", "HEAD").strip())

    def test_a_git_project_ignores_a_leaked_git_dir(self):
        real = Path(self.tmp.name) / "real"
        real.mkdir()
        subprocess.run(["git", "init", "-q", str(real)], check=True)
        os.environ["GIT_DIR"] = str(gitutil.shadow_dir(self.project))
        os.environ["GIT_WORK_TREE"] = str(self.project)
        env = gitutil.git_env(real)
        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("GIT_WORK_TREE", env)
        self.assertEqual(gitutil.find_root(real), real.resolve())


class WorkerEnvTest(NoGitCase):
    def test_run_job_gives_the_worker_the_shadow_repository(self):
        task = self.new_task()
        cfg = Config.load(self.project)
        step = Step("implement", 1, cfg.default_worker)
        job = {
            "id": "test-job", "task": task.id, "phase": "implement", "round": 1,
            "worker": step.worker, "model": cfg.worker_model(step.worker),
            "thinking": cfg.worker_thinking(step.worker), "agent": cfg.implement_agent,
            "backend": "opencode", "session": None,
            "prompt": dispatch.build_prompt(task, step, cfg),
            "timeout": 60, "log": str(task.logs / "test.jsonl"),
        }
        seen = {}
        real_popen = subprocess.Popen

        def spy(*args, **kwargs):
            seen.update(kwargs.get("env") or {})
            return real_popen(*args, **kwargs)

        with mock.patch.object(worker.subprocess, "Popen", side_effect=spy):
            result = worker.run_job(self.project, job, cfg)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(seen.get("GIT_DIR"), str(gitutil.shadow_dir(self.project)))
        self.assertEqual(seen.get("GIT_WORK_TREE"), str(self.project))
        self.assertTrue((self.project / "work.txt").is_file())


class FindProjectTest(NoGitCase):
    def test_a_subfolder_finds_the_no_git_project(self):
        sub = self.project / "sub" / "dir"
        sub.mkdir(parents=True)
        self.assertEqual(gitutil.find_project(sub), self.project)
        self.assertEqual(gitutil.find_project(self.project), self.project)
        self.assertIsNone(gitutil.find_root(self.project), "there is no real repository")

    def test_a_plain_folder_has_no_project(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        self.assertIsNone(gitutil.find_project(plain))

    def test_an_inner_repository_wins_over_an_outer_no_git_folder(self):
        outer = Path(self.tmp.name) / "work"
        (outer / ".aaagent").mkdir(parents=True)
        (outer / ".aaagent" / "config.toml").write_text('[git]\nbranch_mode = "none"\n')
        (outer / "loose").mkdir()
        repo = outer / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "README.md").write_text("hi\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                        "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init"],
                       check=True)
        sub = repo / "sub"
        sub.mkdir()

        self.assertEqual(gitutil.find_project(sub), repo.resolve(),
                         "the inner repository owns its subfolders")
        self.assertEqual(gitutil.find_project(repo), repo.resolve())
        self.assertEqual(gitutil.find_project(outer / "loose"), outer.resolve(),
                         "the outer folder still owns what is outside the repository")
        self.assertEqual(gitutil.find_project(outer), outer.resolve())


class ChangeModeTest(NoGitCase):
    def test_setting_the_same_mode_changes_nothing(self):
        self.assertEqual(branching.change_mode(self.project, "NONE"), ["branch mode: none"])

    def test_an_unknown_mode_lists_the_choices(self):
        with self.assertRaisesRegex(FlowError, "choices: current, branch, pr, none"):
            branching.change_mode(self.project, "git")

    def test_none_to_a_git_mode_needs_a_repository(self):
        with self.assertRaisesRegex(FlowError, "not a git repository; run `git init` first"):
            branching.change_mode(self.project, "branch")
        self.assertEqual(Config.load(self.project).branch_mode, "none")

    def test_switching_while_a_task_is_active_is_refused(self):
        task = self.new_task()
        tasks.claim_active(self.project, task)
        self.addCleanup(tasks.release_active, self.project, task)
        with self.assertRaisesRegex(FlowError, "finish or abandon the active tasks first"):
            branching.change_mode(self.project, "branch")
        self.assertEqual(Config.load(self.project).branch_mode, "none")


class RealRepoModeTest(RealRepoCase):
    def test_switching_to_none_while_a_task_is_active_is_refused(self):
        self.assertEqual(Config.load(self.project).branch_mode, "current")
        task = self.new_task()
        tasks.claim_active(self.project, task)
        self.addCleanup(tasks.release_active, self.project, task)
        with self.assertRaisesRegex(FlowError, "finish or abandon the active tasks first"):
            branching.change_mode(self.project, "none")
        self.assertEqual(Config.load(self.project).branch_mode, "current")
        self.assertFalse((gitutil.shadow_dir(self.project) / "HEAD").exists())

    def test_current_to_none_writes_the_config_and_creates_the_shadow(self):
        self.write_mode("current")
        head_before = self.git("rev-parse", "HEAD").strip()
        lines = branching.change_mode(self.project, "none")
        self.assertEqual(lines, [
            "branch mode: none",
            "no git: aaagent keeps its own history in .aaagent/shadow.git; restart the chat "
            "so its git commands use it",
        ])
        self.assertEqual(Config.load(self.project).branch_mode, "none")
        self.assertTrue((gitutil.shadow_dir(self.project) / "HEAD").is_file())
        self.assertEqual(gitutil.head(self.project), self.shadow("rev-parse", "HEAD").strip())
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), head_before,
                         "the real repository is untouched")

        lines = branching.change_mode(self.project, "current")
        self.assertEqual(lines, [
            "branch mode: current",
            "restart the chat so its git commands use the project's repository",
        ])
        self.assertEqual(Config.load(self.project).branch_mode, "current")

    def test_switching_to_none_with_an_open_branch_is_refused(self):
        self.write_mode("branch")
        task = self.new_task()
        self.assertTrue(branching.start_if_needed(self.project, task, Config.load(self.project)))
        with self.assertRaisesRegex(FlowError, "finish the open branch first"):
            branching.change_mode(self.project, "none")
        self.assertEqual(Config.load(self.project).branch_mode, "branch")
        self.assertFalse((gitutil.shadow_dir(self.project) / "HEAD").exists())

    def test_a_full_flow_leaves_the_real_repository_alone(self):
        self.write_mode("none")
        head_before = self.git("rev-parse", "HEAD").strip()
        branches_before = self.git("branch", "--list", "--format=%(refname:short)")

        task = self.new_task()
        self.implement(task)
        out = self.commit(task)
        state = task.load()
        self.assertIn(f"✓ checkpoint {state['commit'][:7]} recorded for {task.id}", out)

        self.assertEqual(self.git("rev-parse", "HEAD").strip(), head_before, "no commit")
        self.assertEqual(self.git("branch", "--list", "--format=%(refname:short)"),
                         branches_before, "no branch")
        self.assertNotIn(task.id, self.git("log", "--format=%s"), "no commit in the real repo")
        self.assertNotIn("work.txt", self.git("ls-files"))
        self.assertIn(f"{task.id}: approved", self.shadow("log", "--format=%s"))
        self.assertEqual(self.shadow("rev-parse", "HEAD").strip(), state["commit"])


class NoBranchTest(NoGitCase):
    def test_pr_and_branch_done_refuse(self):
        cfg = Config.load(self.project)
        with self.assertRaisesRegex(FlowError, "runs without git"):
            branching.finish(self.project, cfg, push=False)
        with self.assertRaisesRegex(FlowError, "runs without git"):
            branching.retry_pr(self.project, cfg)

    def test_on_idle_and_start_if_needed_do_nothing(self):
        task = self.new_task()
        cfg = Config.load(self.project)
        self.assertIsNone(branching.start_if_needed(self.project, task, cfg))
        self.assertEqual(branching.on_idle(self.project, cfg), [])

    def test_git_status_says_none_with_null_fields(self):
        cfg = Config.load(self.project)
        expected = {"mode": "none", "branch": None, "batch": None, "last_pr": None}
        self.assertEqual(branching.git_status(self.project, cfg), expected)
        self.assertEqual(status.status_data(self.project, cfg)["git"], expected)
        self.assertFalse((self.project / ".git").exists())


class DiffTest(NoGitCase):
    def test_diff_shows_the_checkpoint_of_a_committed_task(self):
        task = self.new_task()
        self.implement(task)
        self.commit(task)

        run = self.aaagent("diff", task.id)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("diff --git", run.stdout)
        self.assertIn("work.txt", run.stdout)
        self.assertIn(f"+{DS}", run.stdout)

        stat = self.aaagent("diff", task.id, "--stat")
        self.assertEqual(stat.returncode, 0, stat.stderr)
        self.assertIn("work.txt", stat.stdout)
        self.assertNotIn("diff --git", stat.stdout, "--stat prints no patch")

    def test_branch_mode_and_status_work_outside_a_repository(self):
        run = self.aaagent("branch-mode")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(
            run.stdout.strip(),
            "branch mode: none (no git; aaagent's own history in .aaagent/shadow.git)")
        self.assertNotIn("branch: ", run.stdout, "the shadow's branch is not the user's git state")

        run = self.aaagent("status")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("reviewer:", run.stdout)


if __name__ == "__main__":
    unittest.main()
