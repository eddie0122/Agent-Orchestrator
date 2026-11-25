"""A worker as the reviewer: one dispatch runs implement → review → fix … by itself."""

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

from aaagent import dispatch, status, tasks, worker  # noqa: E402
from aaagent.config import REVIEWER, Config, project_config_path  # noqa: E402
from aaagent.tasks import FlowError  # noqa: E402

FAKE_OC = ROOT / "tests" / "fake_opencode.py"
FAKE_CLAUDE = ROOT / "tests" / "fake_claude.py"
DS, GLM = "opencode-go/deepseek-v4.1-flash", "opencode-go/glm-5.3-flash"

PLAN = """---
task: x
worker:
---
# Goal
Append the model name to work.txt.
"""

REVIEWER_CFG = '[reviewer]\nmodel = "opencode-go/glm-5.3-flash"\n'
LEGACY_REVIEWER = '[flow]\nreviewer = "worker2"\n'


def review(verdict: str, rnd: int) -> str:
    return f"---\nverdict: {verdict}\nround: {rnd}\n---\n## Required changes\n1. more\n"


class ReviewJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.args_log = Path(self.tmp.name) / "args.jsonl"
        self.claude_args_log = Path(self.tmp.name) / "claude-args.jsonl"
        self.env_backup = dict(os.environ)
        os.environ.update({
            "AAAGENT_OPENCODE_BIN": str(FAKE_OC),
            "AAAGENT_CLAUDE_BIN": str(FAKE_CLAUDE),
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "FAKE_OC_ARGS_LOG": str(self.args_log),
            "FAKE_OC_FAIL": "", "FAKE_OC_NOREPORT": "",
            "FAKE_OC_REVIEW": "", "FAKE_OC_NOREVIEW": "",
            "FAKE_CLAUDE_ARGS_LOG": str(self.claude_args_log),
            "FAKE_CLAUDE_REVIEW": "", "FAKE_CLAUDE_NOREVIEW": "",
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

    def run_dispatch(self, task, **kw) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = dispatch.dispatch(self.project, task.id, **kw)
        return code, buf.getvalue()

    def calls(self) -> list[list[str]]:
        if not self.args_log.exists():
            return []
        return [json.loads(line) for line in self.args_log.read_text().splitlines()]

    def claude_calls(self) -> list[list[str]]:
        if not self.claude_args_log.exists():
            return []
        return [json.loads(line) for line in self.claude_args_log.read_text().splitlines()]

    # -- 1. the setting ------------------------------------------------------

    def test_reviewer_defaults_and_unknown_names(self):
        cfg = Config.load(self.project)
        self.assertEqual(cfg.reviewer, "orchestrator")
        self.assertIsNone(cfg.reviewer_worker)
        self.assertTrue(cfg.reviewer_is_planner)
        self.assertEqual(cfg.review_agent, "aaagent-reviewer")

        self.config('[reviewer]\nmodel = "nope"\n')
        cfg = Config.load(self.project)
        self.assertEqual(cfg.reviewer_model, "nope")
        self.assertEqual(cfg.reviewer_worker, REVIEWER,
                         "a model reviewer is never the orchestrator")

        self.config('[flow]\nreviewer = "nope"\n')
        cfg = Config.load(self.project)
        self.assertEqual(cfg.reviewer, "orchestrator",
                         "an unknown legacy name falls back to the planner")

        for alias in ("planner", "chat", ""):
            self.config(f'[reviewer]\nmodel = "{alias}"\n' if alias else "")
            self.assertEqual(Config.load(self.project).reviewer, "orchestrator", alias)

        self.config(REVIEWER_CFG)
        cfg = Config.load(self.project)
        self.assertEqual(cfg.reviewer, REVIEWER)
        self.assertEqual(cfg.reviewer_worker, REVIEWER)
        self.assertEqual(cfg.reviewer_model, GLM)

    # -- 2. one dispatch runs the whole round --------------------------------

    def test_one_dispatch_implements_reviews_then_commits(self):
        self.config(REVIEWER_CFG)
        task = self.new_task()
        head0 = self.git("rev-parse", "HEAD").strip()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertTrue(task.report(1).is_file())
        self.assertTrue(task.review(1).is_file())
        self.assertEqual(task.verdict(1), "approved")
        state = task.load()
        self.assertEqual(state["status"], "approved")
        self.assertEqual(state["worker"], "worker1", "a review never changes the implementing worker")
        self.assertIn("the reviewer approved", out)
        agents = [c[c.index("--agent") + 1] for c in self.calls()]
        self.assertEqual(agents, ["aaagent-worker", "aaagent-reviewer"])
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), head0, "the work stays uncommitted")

        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "committed")
        agents = [c[c.index("--agent") + 1] for c in self.calls()]
        self.assertEqual(agents[-1], "aaagent-committer")
        self.assertNotEqual(self.git("rev-parse", "HEAD").strip(), head0)

    # -- 3. rejected reviews drive fix rounds --------------------------------

    def test_changes_requested_then_approved_in_one_dispatch(self):
        os.environ["FAKE_OC_REVIEW"] = "changes_requested,approved"
        self.config(REVIEWER_CFG)
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.verdict(1), "changes_requested")
        self.assertEqual(task.verdict(2), "approved")
        self.assertTrue(task.report(2).is_file())
        state = task.load()
        self.assertEqual(state["status"], "approved")
        self.assertEqual(state["worker"], "worker1")
        self.assertIn("worker1", state["sessions"])
        self.assertIn(REVIEWER, state["sessions"], "the reviewer keeps its own session")

        reviewer_calls = [c for c in self.calls() if c[c.index("--agent") + 1] == "aaagent-reviewer"]
        self.assertEqual(len(reviewer_calls), 2)
        self.assertNotIn("--session", reviewer_calls[0], "the first review starts a fresh session")
        self.assertIn("--session", reviewer_calls[1], "the second review continues it")

    # -- 3b. a plan problem stops the chain until the plan is revised ---------

    def test_plan_problem_stops_until_the_plan_is_revised(self):
        os.environ["FAKE_OC_REVIEW"] = "plan_problem,approved"
        self.config(REVIEWER_CFG)
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.verdict(1), "plan_problem")
        self.assertEqual(task.load()["status"], "plan_problem")
        self.assertFalse(task.report(2).exists())
        self.assertIn("revise", out.lower())

        with self.assertRaises(FlowError) as ctx:
            self.run_dispatch(task)
        self.assertIn("plan.md", str(ctx.exception))
        self.assertFalse(task.report(2).exists())

        task.plan.write_text(task.plan.read_text() + "\n# Revised\nAlso fine.\n")
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertTrue(task.report(2).is_file())
        self.assertEqual(task.verdict(2), "approved")
        state = task.load()
        self.assertEqual(state["status"], "approved")
        self.assertEqual(state["worker"], "worker1")

    def test_plan_problem_does_not_switch_to_the_fallback_worker(self):
        os.environ["FAKE_OC_REVIEW"] = "plan_problem,approved"
        self.config(REVIEWER_CFG + '[flow]\nfallback_after_failed_reviews = 1\n')
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "plan_problem")

        task.plan.write_text(task.plan.read_text() + "\n# Revised\nAlso fine.\n")
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertNotIn("switching worker", out, "the plan was at fault, not the worker")
        self.assertEqual(task.load()["worker"], "worker1")
        self.assertEqual(task.verdict(2), "approved")

    def test_status_shows_plan_problem_attention(self):
        os.environ["FAKE_OC_REVIEW"] = "plan_problem,approved"
        self.config(REVIEWER_CFG)
        task = self.new_task()
        self.run_dispatch(task)
        data = status.status_data(self.project, Config.load(self.project))
        entry = next(t for t in data["tasks"] if t["id"] == task.id)
        self.assertEqual(entry["attention"], "plan problem")

    # -- 4. the round limit stops the chain -----------------------------------

    def test_round_limit_stops_the_chain(self):
        os.environ["FAKE_OC_REVIEW"] = "changes_requested,changes_requested,changes_requested"
        self.config(REVIEWER_CFG)
        task = self.new_task()
        head0 = self.git("rev-parse", "HEAD").strip()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertIn("stops here", out)
        self.assertIn("ask the user", out)
        self.assertEqual(task.load()["status"], "changes_requested")
        for n in (1, 2, 3):
            self.assertTrue(task.report(n).is_file(), n)
            self.assertTrue(task.review(n).is_file(), n)
        self.assertEqual(task.verdict(3), "changes_requested")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), head0, "no commit was made")

        code, out = self.run_dispatch(task, force=True)
        self.assertEqual(code, 0, out)
        self.assertTrue(task.report(4).is_file())

    # -- 5. a failed review retries on the reviewer, then goes to the planner --

    def test_review_failure_retries_once_on_the_reviewer_then_the_planner(self):
        os.environ["FAKE_OC_NOREVIEW"] = GLM
        self.config(REVIEWER_CFG)
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertIn("retrying on the reviewer", out)
        self.assertNotIn("retrying with worker1", out, "a review never falls back to a worker")
        self.assertIn("Review it yourself", out)
        self.assertFalse(task.review(1).exists())
        state = task.load()
        self.assertEqual(state["status"], "awaiting_review")
        self.assertEqual(state["review_failed_round"], 1)
        self.assertEqual(state["worker"], "worker1")
        reviewer_calls = [c for c in self.calls() if c[c.index("--agent") + 1] == "aaagent-reviewer"]
        self.assertEqual(len(reviewer_calls), 2, "the failed review is retried once")

        # The planner writes the review itself and dispatch commits as usual.
        task.review(1).write_text(review("approved", 1))
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "committed")

    # -- 6. an invalid verdict is a failed review -----------------------------

    def test_invalid_verdict_is_a_failed_review(self):
        os.environ["FAKE_OC_REVIEW"] = "bad"
        self.config(REVIEWER_CFG)
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertIn("retrying on the reviewer", out)
        self.assertTrue((task.path / "review-1.invalid.md").is_file())
        self.assertFalse(task.review(1).exists())
        state = task.load()
        self.assertEqual(state["status"], "awaiting_review")
        self.assertEqual(state["review_failed_round"], 1)

    # -- 8. the planner can overrule an approval ------------------------------

    def test_overruling_an_approval_runs_another_round(self):
        self.config(REVIEWER_CFG)
        task = self.new_task()
        self.run_dispatch(task)
        self.assertEqual(task.verdict(1), "approved")

        task.review(1).write_text(review("changes_requested", 1))
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertTrue(task.report(2).is_file())
        self.assertTrue(task.review(2).is_file())
        self.assertEqual(task.verdict(2), "approved")
        self.assertEqual(task.load()["status"], "approved")

    # -- 9. a reviewer on Claude Code -----------------------------------------

    def test_reviewer_on_claude_gets_the_guard_role(self):
        self.config('[reviewer]\nmodel = "sonnet"\n')
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "approved")
        self.assertIn("by sonnet", task.review(1).read_text())
        calls = self.claude_calls()
        self.assertEqual(len(calls), 1, calls)
        settings = json.loads(calls[0][calls[0].index("--settings") + 1])
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertTrue(command.endswith("guard_worker.py reviewer"), command)

    def test_claude_reviewer_gets_thinking_as_effort(self):
        self.config('[reviewer]\nmodel = "sonnet"\nthinking = "high"\n')
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "approved")
        calls = self.claude_calls()
        self.assertEqual(calls[0][calls[0].index("--effort") + 1], "high", calls[0])

    # -- 10. the legacy flow.reviewer still works ------------------------------

    def test_legacy_flow_reviewer_runs_reviewer_jobs_with_the_workers_model(self):
        self.config(LEGACY_REVIEWER)
        cfg = Config.load(self.project)
        self.assertEqual(cfg.reviewer_model, GLM, "worker2's model, not the seed reviewer's")
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.verdict(1), "approved")
        self.assertIn("by " + GLM, task.review(1).read_text())

    # -- 11. the chat's prompt says who reviews -------------------------------

    def test_orchestrator_prompt_mentions_the_worker_reviewer(self):
        from aaagent import session
        cfg = Config.load(self.project)
        plain = session.orchestrator_prompt(cfg, self.project)
        self.assertNotIn("Reviews are written by the reviewer", plain)

        self.config(REVIEWER_CFG)
        cfg = Config.load(self.project)
        self.new_task("half done")
        prompt = session.orchestrator_prompt(cfg, self.project)
        self.assertIn("Reviews are written by the reviewer", prompt)
        self.assertNotIn("`worker2`", prompt, "the reviewer is its own role now")
        self.assertIn(f"a separate reviewer ({GLM}) reviews each round", prompt)
        self.assertLess(prompt.index("Reviews are written by the reviewer"),
                        prompt.index("## Unfinished work from an earlier chat"))

    # -- 12. the dashboard says what a review job is doing --------------------

    def test_status_summary_of_a_review_job(self):
        t = {"id": "001-x", "number": 1, "title": "x", "status": "running", "round": 1,
             "worker": "worker1", "verdict": None, "plan_written": True,
             "job": {"phase": "review", "round": 1, "worker": "worker2"},
             "next": None, "blocked": None, "attention": None, "commit": None, "files": []}
        self.assertEqual(status.task_summary(t), "reviewing · worker2")

    # -- the default flow is unchanged ---------------------------------------

    def test_default_config_still_requires_a_review(self):
        task = self.new_task()
        self.run_dispatch(task)
        with self.assertRaisesRegex(FlowError, "no review yet"):
            self.run_dispatch(task)


class GuardWorkerReviewerHookTest(unittest.TestCase):
    HOOK = ROOT / "claude" / "hooks" / "guard_worker.py"

    def run_hook(self, path, project, tool="Write", role=None):
        event = {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": project}
        cmd = [sys.executable, str(self.HOOK)] + ([role] if role else [])
        return subprocess.run(cmd, input=json.dumps(event), capture_output=True, text=True,
                              env={**os.environ, "AAAGENT_PROJECT": project})

    def test_a_reviewer_may_only_write_its_review(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_hook("src/app.py", d, role="reviewer").returncode, 2)
            self.assertEqual(self.run_hook(".aaagent/tasks/001-x/review-1.md", d,
                                           role="reviewer").returncode, 0)
            self.assertEqual(self.run_hook(".aaagent/tasks/001-x/report-1.md", d,
                                           role="reviewer").returncode, 2)
            self.assertEqual(self.run_hook(".aaagent/tasks/001-x/plan.md", d, tool="Read",
                                           role="reviewer").returncode, 0)
            self.assertEqual(self.run_hook(".env", d, tool="Read", role="reviewer").returncode, 2)
            r = self.run_hook("src/app.py", d, role="reviewer")
            self.assertIn("review file", r.stderr)

    def test_without_the_argument_reviews_stay_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_hook(".aaagent/tasks/001-x/review-1.md", d).returncode, 2)


if __name__ == "__main__":
    unittest.main()
