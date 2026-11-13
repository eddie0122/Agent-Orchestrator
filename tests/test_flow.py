"""End-to-end tests of the task flow against a fake opencode."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import dispatch, tasks, worker  # noqa: E402
from aaagent.config import Config  # noqa: E402
from aaagent.tasks import FlowError  # noqa: E402

FAKE = ROOT / "tests" / "fake_opencode.py"
DS, GLM = "opencode-go/deepseek-v4.1-flash", "opencode-go/glm-5.3-flash"

PLAN = """---
task: x
worker:
---
# Goal
Append the model name to work.txt.
"""


def review(verdict: str, rnd: int) -> str:
    return f"---\nverdict: {verdict}\nround: {rnd}\n---\n## Required changes\n1. more\n"


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.args_log = Path(self.tmp.name) / "args.jsonl"
        self.env_backup = dict(os.environ)
        os.environ.update({
            "AAAGENT_OPENCODE_BIN": str(FAKE),
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "FAKE_OC_ARGS_LOG": str(self.args_log),
            "FAKE_OC_FAIL": "", "FAKE_OC_NOREPORT": "",
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
        import time
        for name in ("deepseek", "glm"):
            worker.stop(self.project, name)
        for _ in range(50):
            if not any(worker.worker_pid(self.project, n) for n in ("deepseek", "glm")):
                break
            time.sleep(0.1)

    def tearDown(self):
        self.stop_workers()
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.project, check=True,
                              capture_output=True, text=True).stdout

    def new_task(self, title="add work") -> tasks.Task:
        task = tasks.create(self.project, title)
        task.plan.write_text(PLAN)
        return task

    def run_dispatch(self, task, **kw) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = dispatch.dispatch(self.project, task.id, **kw)
        return code, buf.getvalue()

    def calls(self):
        if not self.args_log.exists():
            return []
        return [json.loads(line) for line in self.args_log.read_text().splitlines()]

    # -------------------------------------------------------------------------

    def test_happy_path_implement_review_commit(self):
        task = self.new_task()
        head0 = self.git("rev-parse", "HEAD").strip()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertTrue(task.report(1).is_file())
        self.assertEqual(task.load()["status"], "awaiting_review")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), head0, "implement must not commit")
        self.assertIn("work.txt", out)

        task.review(1).write_text(review("approved", 1))
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        state = task.load()
        self.assertEqual(state["status"], "committed")
        self.assertNotEqual(self.git("rev-parse", "HEAD").strip(), head0)
        self.assertNotIn(".aaagent", self.git("show", "--name-only", "HEAD"))
        self.assertIsNone(tasks.active_task(self.project))
        agents = [c[c.index("--agent") + 1] for c in self.calls()]
        self.assertEqual(agents, ["aaagent-worker", "aaagent-committer"])
        with self.assertRaises(FlowError):
            tasks.next_step(task, Config.load(self.project))

    def test_template_plan_is_refused(self):
        task = tasks.create(self.project, "unwritten")
        with self.assertRaises(FlowError):
            self.run_dispatch(task)

    def test_review_required_before_next_round(self):
        task = self.new_task()
        self.run_dispatch(task)
        with self.assertRaisesRegex(FlowError, "no review yet"):
            self.run_dispatch(task)
        task.review(1).write_text("no frontmatter\n")
        with self.assertRaisesRegex(FlowError, "verdict"):
            self.run_dispatch(task)

    def test_failed_job_falls_back_to_glm(self):
        os.environ["FAKE_OC_FAIL"] = DS
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertIn("retrying with glm", out)
        self.assertIn(GLM, task.report(1).read_text())
        self.assertEqual(task.load()["worker"], "glm")

    def test_missing_report_counts_as_failure(self):
        os.environ["FAKE_OC_NOREPORT"] = DS
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertIn("without writing", out)
        self.assertEqual(task.load()["worker"], "glm")

    def test_both_workers_fail_stops_task(self):
        os.environ["FAKE_OC_FAIL"] = f"{DS},{GLM}"
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 1, out)
        self.assertEqual(task.load()["status"], "failed")
        os.environ["FAKE_OC_FAIL"] = ""
        self.stop_workers()  # daemons keep the env they started with
        code, out = self.run_dispatch(task)  # re-dispatch after a failure is allowed
        self.assertEqual(code, 0, out)

    def test_rejected_reviews_switch_worker_then_escalate(self):
        task = self.new_task()
        self.run_dispatch(task)
        task.review(1).write_text(review("changes_requested", 1))
        self.run_dispatch(task)                       # round 2, still deepseek, same session
        self.assertIn(DS, task.report(2).read_text())
        ds_calls = [c for c in self.calls() if DS in c]
        self.assertIn("--session", ds_calls[1], "fix round should continue the worker's session")

        task.review(2).write_text(review("changes_requested", 2))
        code, out = self.run_dispatch(task)          # round 3 goes to glm
        self.assertIn("switching worker", out)
        self.assertIn(GLM, task.report(3).read_text())
        self.assertNotIn("--session", [c for c in self.calls() if GLM in c][0])

        task.review(3).write_text(review("changes_requested", 3))
        with self.assertRaisesRegex(FlowError, "ask the user"):
            self.run_dispatch(task)
        code, out = self.run_dispatch(task, force=True)
        self.assertEqual(code, 0, out)
        self.assertTrue(task.report(4).is_file())

    def test_worker_override_and_plan_frontmatter(self):
        task = self.new_task()
        task.plan.write_text(PLAN.replace("worker:", "worker: glm"))
        self.run_dispatch(task)
        self.assertIn(GLM, task.report(1).read_text())

    def test_one_active_task_per_repo(self):
        first, second = self.new_task("one"), self.new_task("two")
        self.run_dispatch(first)
        with self.assertRaisesRegex(FlowError, "still in progress"):
            self.run_dispatch(second)
        buf = io.StringIO()
        with redirect_stdout(buf):
            from aaagent import cli
            cli.main(["abandon", first.id])
        code, out = self.run_dispatch(second)
        self.assertEqual(code, 0, out)

    def test_preexisting_changes_listed_for_committer(self):
        (self.project / "README.md").write_text("edited by user\n")
        task = self.new_task()
        self.assertEqual(task.load()["preexisting_dirty"], ["README.md"])
        self.run_dispatch(task)
        task.review(1).write_text(review("approved", 1))
        step = tasks.next_step(task, Config.load(self.project))
        prompt = dispatch.build_prompt(task, step, Config.load(self.project))
        self.assertIn("README.md", prompt)

    def test_find_task_by_number(self):
        task = self.new_task()
        self.assertEqual(tasks.find(self.project, "1").id, task.id)
        self.assertEqual(tasks.find(self.project, "001").id, task.id)

    def test_agent_dir_is_git_excluded(self):
        self.new_task()
        self.assertNotIn(".aaagent", self.git("status", "--porcelain"))

    def cli(self, *argv) -> tuple[int, str]:
        from aaagent import cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_status_json_follows_the_task(self):
        code, out = self.cli("status", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([w["name"] for w in data["workers"]], ["deepseek", "glm"])
        self.assertEqual(data["tasks"], [])

        task = tasks.create(self.project, "unwritten plan")
        t = json.loads(self.cli("status", "--json")[1])["tasks"][0]
        self.assertFalse(t["plan_written"])
        self.assertIsNone(t["next"])
        self.assertIn("template", t["blocked"])

        task.plan.write_text(PLAN)
        t = json.loads(self.cli("status", "--json")[1])["tasks"][0]
        self.assertEqual(t["next"], {"phase": "implement", "round": 1, "worker": "deepseek"})
        self.assertEqual((t["number"], t["title"]), (1, "unwritten plan"))

        self.run_dispatch(task)
        data = json.loads(self.cli("status", "--json")[1])
        t = data["tasks"][0]
        self.assertEqual(t["status"], "awaiting_review")
        self.assertEqual(data["active_task"], task.id)
        self.assertEqual([f["name"] for f in t["files"]], ["plan.md", "report-1.md"])
        self.assertEqual(t["files"][1]["status"], "done")
        self.assertEqual(next(w for w in data["workers"] if w["name"] == "deepseek")["state"], "idle")

        task.review(1).write_text(review("changes_requested", 1))
        t = json.loads(self.cli("status", "--json")[1])["tasks"][0]
        self.assertEqual(t["verdict"], "changes_requested")
        self.assertEqual(t["files"][-1]["kind"], "review")
        self.assertEqual(t["next"]["phase"], "fix")
        self.assertIsNone(t["attention"])

    def test_status_json_flags_tasks_that_need_the_user(self):
        os.environ["FAKE_OC_FAIL"] = f"{DS},{GLM}"
        task = self.new_task()
        self.run_dispatch(task)
        t = json.loads(self.cli("status", "--json")[1])["tasks"][0]
        self.assertEqual(t["attention"], "failed")

    def test_stop_named_worker(self):
        worker.start_background(self.project, "deepseek")
        worker.start_background(self.project, "glm")
        code, out = self.cli("stop", "glm")
        self.assertEqual(out.strip(), "stopped glm")
        self.assertIsNotNone(worker.worker_pid(self.project, "deepseek"))
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.cli("stop", "nope")[0], 2)


    # -- dashboard and chat restore -------------------------------------------------

    def test_ui_snapshot_shows_the_task_and_its_files(self):
        task = self.new_task("add health")
        self.run_dispatch(task)
        code, out = self.cli("ui", "--snapshot")
        self.assertEqual(code, 0)
        self.assertIn("WORKERS", out)
        self.assertRegex(out, r"▾ 001 add health +in review \(round 1\)")
        self.assertIn("report-1.md", out)
        self.assertTrue(all(len(line) <= 80 for line in out.splitlines()))

    def test_abandon_command_uses_shared_helper(self):
        task = self.new_task()
        code, out = self.cli("abandon", "1")
        self.assertEqual((code, out.splitlines()[0]), (0, f"task {task.id} abandoned."))
        self.assertEqual(task.load()["status"], "abandoned")

    def test_resume_flag_routes_to_start(self):
        from unittest import mock
        from aaagent import cli, session
        for argv, resume in ((["-c"], True), (["--continue", "hi"], True), (["--no-tmux"], False), ([], False)):
            with mock.patch.object(session, "launch", return_value=0) as launch:
                self.assertEqual(cli.main(argv), 0)
            self.assertEqual(launch.call_args.kwargs["resume"], resume, argv)
        self.assertEqual(launch.call_args.args[1], None)

    def test_last_chat_needs_a_transcript(self):
        from aaagent import session
        self.assertIsNone(session.last_chat(self.project))
        transcript = Path(self.tmp.name) / "t.jsonl"
        chat = session.chat_file(self.project)
        chat.write_text(json.dumps({"session_id": "s2", "transcript_path": str(transcript),
                                    "previous": {"session_id": "s1", "transcript_path": str(transcript)}}))
        self.assertIsNone(session.last_chat(self.project), "no transcript on disk yet")
        transcript.write_text("{}\n")
        self.assertEqual(session.last_chat(self.project), "s2")
        self.assertEqual(session.previous_chat(self.project), "s1")
        chat.write_text("not json")
        self.assertIsNone(session.last_chat(self.project))

    def test_claude_command_and_settings(self):
        from aaagent import session
        cfg = Config.load(self.project)
        cmd = session.claude_command(self.project, cfg, "claude", None)
        self.assertNotIn("--resume", cmd)
        cmd = session.claude_command(self.project, cfg, "claude", "go", resume_id="abc")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")
        self.assertEqual(cmd[-1], "go")
        hooks = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]
        self.assertIn("record_session.py", hooks["SessionStart"][0]["hooks"][0]["command"])
        self.assertIn("guard_edit.py", hooks["PreToolUse"][0]["hooks"][0]["command"])

    def test_prompt_lists_unfinished_tasks(self):
        from aaagent import session
        cfg = Config.load(self.project)
        self.assertNotIn("## Unfinished work", session.orchestrator_prompt(cfg, self.project))
        task = self.new_task("half done")
        prompt = session.orchestrator_prompt(cfg, self.project)
        self.assertIn("## Unfinished work from an earlier chat", prompt)
        self.assertIn(task.id, prompt)
        self.cli("abandon", task.id)
        self.assertNotIn("## Unfinished work", session.orchestrator_prompt(cfg, self.project))


class RecordSessionHookTest(unittest.TestCase):
    HOOK = ROOT / "claude" / "hooks" / "record_session.py"

    def run_hook(self, project, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, str(self.HOOK)], input=text, capture_output=True,
                              text=True, env={**os.environ, "AAAGENT_PROJECT": project})

    def test_records_and_keeps_the_previous_chat(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".aaagent").mkdir()
            t1 = Path(d) / "s1.jsonl"
            r = self.run_hook(d, {"session_id": "s1", "transcript_path": str(t1), "source": "startup"})
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
            chat = json.loads((Path(d) / ".aaagent" / "chat.json").read_text())
            self.assertEqual((chat["session_id"], chat["previous"]), ("s1", None))

            # s1 was never used (no transcript), so it is not kept as previous
            self.run_hook(d, {"session_id": "s2", "transcript_path": str(Path(d) / "s2.jsonl")})
            chat = json.loads((Path(d) / ".aaagent" / "chat.json").read_text())
            self.assertEqual((chat["session_id"], chat["previous"]), ("s2", None))

            (Path(d) / "s2.jsonl").write_text("{}\n")
            self.run_hook(d, {"session_id": "s3", "transcript_path": str(Path(d) / "s3.jsonl")})
            self.run_hook(d, {"session_id": "s3", "transcript_path": str(Path(d) / "s3.jsonl"), "source": "resume"})
            chat = json.loads((Path(d) / ".aaagent" / "chat.json").read_text())
            self.assertEqual((chat["session_id"], chat["previous"]["session_id"]), ("s3", "s2"))

    def test_never_fails(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_hook(d, "not json").returncode, 0)
            self.assertEqual(self.run_hook(d, {"session_id": "x"}).returncode, 0)   # no .aaagent here
            self.assertFalse((Path(d) / ".aaagent").exists())


class GuardHookTest(unittest.TestCase):
    HOOK = ROOT / "claude" / "hooks" / "guard_edit.py"

    def run_hook(self, path, project):
        event = {"tool_name": "Write", "tool_input": {"file_path": path}, "cwd": project}
        return subprocess.run([sys.executable, str(self.HOOK)], input=json.dumps(event),
                              capture_output=True, text=True, env={**os.environ, "AAAGENT_PROJECT": project})

    def test_blocks_project_source(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_hook(f"{d}/src/app.py", d)
            self.assertEqual(r.returncode, 2)
            self.assertIn("planner", r.stderr)

    def test_allows_agent_dir_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_hook(f"{d}/.aaagent/tasks/001-x/plan.md", d).returncode, 0)
            self.assertEqual(self.run_hook(".aaagent/tasks/001-x/review-1.md", d).returncode, 0)
            self.assertEqual(self.run_hook(f"{d}/.aaagent/../src/x.py", d).returncode, 2)


if __name__ == "__main__":
    unittest.main()
