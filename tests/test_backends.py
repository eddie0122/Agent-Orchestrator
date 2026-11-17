"""The worker backends: opencode (`opencode run`) and headless Claude Code (`claude -p`)."""

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

from aaagent import backends, dispatch, models, session, status, tasks, worker  # noqa: E402
from aaagent.config import (PLANNER_THINKING, Config, infer_backend,  # noqa: E402
                            project_config_path)

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


def review(verdict: str, rnd: int) -> str:
    return f"---\nverdict: {verdict}\nround: {rnd}\n---\n## Required changes\n1. more\n"


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.args_log = Path(self.tmp.name) / "args.jsonl"
        self.env_log = Path(self.tmp.name) / "env.jsonl"
        self.claude_args_log = Path(self.tmp.name) / "claude-args.jsonl"
        self.env_backup = dict(os.environ)
        os.environ.update({
            "AAAGENT_OPENCODE_BIN": str(FAKE_OC),
            "AAAGENT_CLAUDE_BIN": str(FAKE_CLAUDE),
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "FAKE_OC_ARGS_LOG": str(self.args_log),
            "FAKE_OC_ENV_LOG": str(self.env_log),
            "FAKE_OC_FAIL": "", "FAKE_OC_NOREPORT": "",
            "FAKE_CLAUDE_ARGS_LOG": str(self.claude_args_log),
            "FAKE_CLAUDE_FAIL": "", "FAKE_CLAUDE_NOREPORT": "",
            "AAAGENT_PROJECT": str(self.project),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        self.git("init", "-q")
        (self.project / "README.md").write_text("hi\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "init")
        session.init_project(self.project, Config.load(self.project))

    def tearDown(self):
        for name in ("worker1", "worker2"):
            worker.stop(self.project, name)
        for _ in range(50):
            if not any(worker.worker_pid(self.project, n) for n in ("worker1", "worker2")):
                break
            time.sleep(0.1)
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

    def job(self, phase="implement", **kw) -> dict:
        report = self.project / ".aaagent" / "tasks" / "001-x" / "report-1.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        job = {
            "id": "j1", "task": "001-x", "phase": phase, "round": 1, "worker": "worker1",
            "model": DS, "thinking": "", "agent": "aaagent-worker",
            "prompt": f"Write your report to: {report}\nround: 1\n",
            "log": str(report.parent / "log.jsonl"), "timeout": 30,
        }
        job.update(kw)
        return job

    def calls(self) -> list[list[str]]:
        if not self.args_log.exists():
            return []
        return [json.loads(line) for line in self.args_log.read_text().splitlines()]

    def claude_calls(self) -> list[list[str]]:
        if not self.claude_args_log.exists():
            return []
        return [json.loads(line) for line in self.claude_args_log.read_text().splitlines()]

    def fake_envs(self) -> list[dict]:
        """The PWD and real cwd each fake opencode run saw."""
        if not self.env_log.exists():
            return []
        return [json.loads(line) for line in self.env_log.read_text().splitlines()]

    def run_dispatch(self, task, **kw) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = dispatch.dispatch(self.project, task.id, **kw)
        return code, buf.getvalue()

    def cli(self, *argv) -> tuple[int, str]:
        from aaagent import cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    # -- 1. the backend of a worker -----------------------------------------

    def test_infer_backend_and_worker_backend(self):
        self.assertEqual(infer_backend("opencode-go/x"), "opencode")
        self.assertEqual(infer_backend("sonnet"), "claude")
        with self.assertRaises(KeyError):
            Config.load(self.project).worker_backend("nope")

        cfg = Config.load(self.project)
        self.assertEqual(cfg.worker_backend("worker1"), "opencode")
        self.assertEqual(cfg.worker_backend("worker2"), "opencode")

        self.config('[workers.worker1]\nmodel = "sonnet"\n')
        self.assertEqual(Config.load(self.project).worker_backend("worker1"), "claude")

        self.config('[workers.worker1]\nmodel = "sonnet"\nbackend = "claude"\n')
        self.assertEqual(Config.load(self.project).worker_backend("worker1"), "claude")

        self.config('[workers.worker1]\nmodel = "opencode-go/deepseek-v4.1-flash"\nbackend = "x"\n')
        self.assertEqual(Config.load(self.project).worker_backend("worker1"), "opencode")

    # -- 2. the opencode command is unchanged --------------------------------

    def test_opencode_command_is_todays_argv(self):
        cfg = Config.load(self.project)
        job = self.job(model=DS, thinking="high", session="ses_1")
        cmd = backends.command(job, cfg)
        self.assertEqual(cmd, [
            str(FAKE_OC), "run", "--auto", "--agent", "aaagent-worker", "-m", f"{DS}#high",
            "--format", "json", "--title", "aaagent 001-x implement r1",
            "--session", "ses_1", job["prompt"],
        ])

    # -- 3. the claude command -----------------------------------------------

    def test_claude_command_flags_and_settings(self):
        cfg = Config.load(self.project)
        job = self.job(model="sonnet", thinking="high", session="abc", backend="claude")
        cmd = backends.command(job, cfg)
        self.assertEqual(cmd[0], str(FAKE_CLAUDE))
        self.assertIn("-p", cmd)
        self.assertIn("--verbose", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[-1], job["prompt"])
        self.assertIn("unattended implementation worker",
                      cmd[cmd.index("--append-system-prompt") + 1])
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertIn("guard_worker.py",
                      settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
        self.assertIn("Bash", settings["permissions"]["allow"])

        commit = self.job(phase="commit", model="sonnet", backend="claude",
                          agent="aaagent-committer")
        cmd = backends.command(commit, cfg)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "default")
        self.assertNotIn("--effort", cmd)
        self.assertNotIn("--resume", cmd)
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertIn("Edit", settings["permissions"]["deny"])
        self.assertNotIn("Edit", settings["permissions"]["allow"])
        self.assertIn("commit step", cmd[cmd.index("--append-system-prompt") + 1])

    def test_claude_command_without_the_binary(self):
        cfg = Config.load(self.project)
        cfg.data["workers"]["claude_bin"] = str(Path(self.tmp.name) / "no-claude")
        cfg.data["orchestrator"]["claude_bin"] = ""
        os.environ.pop("AAAGENT_CLAUDE_BIN", None)
        with self.assertRaises(FileNotFoundError):
            backends.command(self.job(model="sonnet", backend="claude"), cfg)

    # -- 4. a claude job through run_job -------------------------------------

    def test_run_job_on_claude(self):
        cfg = Config.load(self.project)
        job = self.job(model="sonnet", backend="claude")
        result = worker.run_job(self.project, job, cfg)
        self.assertEqual(result["status"], "ok", result.get("error"))
        report = self.project / ".aaagent" / "tasks" / "001-x" / "report-1.md"
        self.assertTrue(report.is_file())
        events = [json.loads(line) for line in Path(job["log"]).read_text().splitlines() if line.strip()]
        self.assertEqual(result["session"], events[0]["session_id"])
        self.assertEqual(result["tokens"], {"input": 100, "output": 10})
        self.assertEqual(result["backend"], "claude")

    def test_run_job_passes_the_job_directory_as_pwd(self):
        cfg = Config.load(self.project)
        wt = self.project / "wt"
        wt.mkdir()
        job = self.job(cwd=str(wt))
        # The parent (aaagent dispatch) was started in the main checkout: PWD says so.
        with mock.patch.dict(os.environ, {"PWD": str(self.project)}):
            result = worker.run_job(self.project, job, cfg)
        self.assertEqual(result["status"], "ok", result.get("error"))
        seen = self.fake_envs()
        self.assertEqual(len(seen), 1, seen)
        self.assertEqual(os.path.realpath(seen[0]["pwd"]), os.path.realpath(wt))
        self.assertEqual(os.path.realpath(seen[0]["cwd"]), os.path.realpath(wt))

    def test_run_job_without_cwd_passes_the_project_as_pwd(self):
        cfg = Config.load(self.project)
        job = self.job()
        with mock.patch.dict(os.environ, {"PWD": str(self.tmp.name)}):
            result = worker.run_job(self.project, job, cfg)
        self.assertEqual(result["status"], "ok", result.get("error"))
        seen = self.fake_envs()
        self.assertEqual(len(seen), 1, seen)
        self.assertEqual(os.path.realpath(seen[0]["pwd"]), os.path.realpath(self.project))
        self.assertEqual(os.path.realpath(seen[0]["cwd"]), os.path.realpath(self.project))

    # -- 5. the whole flow on claude -----------------------------------------

    def test_full_flow_on_claude(self):
        self.config('[workers.worker1]\nmodel = "sonnet"\n')
        task = self.new_task()
        head0 = self.git("rev-parse", "HEAD").strip()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertTrue(task.report(1).is_file())
        self.assertEqual(task.load()["status"], "awaiting_review")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), head0, "implement must not commit")

        task.review(1).write_text(review("approved", 1))
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertEqual(task.load()["status"], "committed")
        self.assertNotEqual(self.git("rev-parse", "HEAD").strip(), head0)

        self.assertEqual(len(self.claude_calls()), 2, self.claude_calls())
        self.assertEqual(self.calls(), [], "the opencode fake must not be called")
        sessions = [c[c.index("--resume") + 1] for c in self.claude_calls() if "--resume" in c]
        self.assertEqual(len(sessions), 0, "the commit job starts a fresh session")

    # -- 6. a failed claude job falls back -----------------------------------

    def test_failed_claude_job_falls_back_to_opencode(self):
        self.config('[workers.worker1]\nmodel = "sonnet"\n'
                    '[workers.worker2]\nmodel = "opencode-go/glm-5.3-flash"\n')
        os.environ["FAKE_CLAUDE_FAIL"] = "sonnet"
        task = self.new_task()
        code, out = self.run_dispatch(task)
        self.assertEqual(code, 0, out)
        self.assertIn("retrying with worker2", out)
        self.assertIn(GLM, task.report(1).read_text())
        self.assertEqual(task.load()["worker"], "worker2")
        self.assertEqual(task.load()["status"], "awaiting_review")

    # -- 7. sessions only continue on their own backend ----------------------

    def test_session_fits(self):
        uuid = "0f9c2f7e-7a4b-4c2d-9d21-1a2b3c4d5e6f"
        self.assertTrue(backends.session_fits("opencode", "ses_1"))
        self.assertFalse(backends.session_fits("claude", "ses_1"))
        self.assertTrue(backends.session_fits("claude", uuid))
        self.assertFalse(backends.session_fits("opencode", uuid))
        self.assertFalse(backends.session_fits("opencode", ""))
        self.assertFalse(backends.session_fits("claude", None))

    # -- 9. choices, thinking and the CLI ------------------------------------

    def test_worker_choices_include_claude(self):
        cfg = Config.load(self.project)
        names = models.choices(cfg, "worker1")
        self.assertIn("opencode-go/kimi-k3", names)
        self.assertIn("sonnet", names)
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("sonnet", models.worker_choices(cfg))

    def test_claude_worker_has_claude_thinking(self):
        self.config('[workers.worker1]\nmodel = "sonnet"\n'
                    '[workers.worker2]\nmodel = "opencode-go/glm-5.3-flash"\n')
        cfg = Config.load(self.project)
        self.assertEqual(models.thinking_choices(cfg, "worker1"), PLANNER_THINKING)
        self.assertEqual(models.thinking_choices(cfg, "worker2"), ["low", "high", "max"])

    def test_switching_a_worker_to_claude_says_so(self):
        code, out = self.cli("model", "worker1", "sonnet")
        self.assertEqual(code, 0, out)
        self.assertIn("Claude Code", out)
        self.assertNotIn("warning", out)
        cfg = Config.load(self.project)
        self.assertEqual((cfg.worker_model("worker1"), cfg.worker_backend("worker1")),
                         ("sonnet", "claude"))

    def test_model_json_has_the_backend(self):
        code, out = self.cli("model", "--json")
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertEqual(data["workers"]["worker1"]["backend"], "opencode")
        self.assertEqual(data["workers"]["worker2"]["backend"], "opencode")
        self.assertIn("sonnet", data["worker_choices"])
        self.assertIn("opencode-go/kimi-k3", data["worker_choices"])

    def test_thinking_json_has_the_backend(self):
        code, out = self.cli("thinking", "--json")
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertEqual(data["workers"]["worker1"]["backend"], "opencode")

    # -- 10. status ----------------------------------------------------------

    def test_status_data_has_the_backend(self):
        data = status.status_data(self.project, Config.load(self.project))
        self.assertEqual(data["workers"][0]["backend"], "opencode")
        self.config('[workers.worker1]\nmodel = "sonnet"\n')
        data = status.status_data(self.project, Config.load(self.project))
        self.assertEqual(data["workers"][0]["backend"], "claude")

    def test_status_text_marks_claude_workers(self):
        code, out = self.cli("status")
        self.assertEqual(code, 0, out)
        self.assertNotIn("[claude]", out)
        self.config('[workers.worker1]\nmodel = "sonnet"\n')
        code, out = self.cli("status")
        self.assertEqual(code, 0, out)
        self.assertIn("[claude]", out)


class GuardWorkerHookTest(unittest.TestCase):
    HOOK = ROOT / "claude" / "hooks" / "guard_worker.py"

    def run_hook(self, path, project, tool="Write"):
        event = {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": project}
        return subprocess.run([sys.executable, str(self.HOOK)], input=json.dumps(event),
                              capture_output=True, text=True,
                              env={**os.environ, "AAAGENT_PROJECT": project})

    def test_planner_files_are_read_only_for_workers(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_hook(".aaagent/tasks/001-x/plan.md", d).returncode, 2)
            self.assertEqual(self.run_hook(".aaagent/tasks/001-x/review-1.md", d).returncode, 2)
            self.assertEqual(self.run_hook(".aaagent/tasks/001-x/state.json", d).returncode, 2)
            self.assertEqual(self.run_hook(".aaagent/config.toml", d).returncode, 2)
            r = self.run_hook(f"{d}/.aaagent/tasks/001-x/plan.md", d, tool="Edit")
            self.assertEqual(r.returncode, 2)
            self.assertIn("read-only", r.stderr)

    def test_a_worker_may_write_its_report_and_source(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_hook(".aaagent/tasks/001-x/report-1.md", d).returncode, 0)
            self.assertEqual(self.run_hook("src/app.py", d).returncode, 0)

    def test_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_hook(".env", d, tool="Read").returncode, 2)
            self.assertEqual(self.run_hook(".env.local", d, tool="Read").returncode, 2)
            self.assertEqual(self.run_hook(".env.example", d, tool="Read").returncode, 0)
            r = self.run_hook("config/.env", d, tool="Read")
            self.assertEqual(r.returncode, 2)
            self.assertIn("secrets", r.stderr)

    def test_reading_the_planner_files_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_hook(".aaagent/tasks/001-x/plan.md", d, tool="Read").returncode, 0)

    def test_bad_input_never_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, str(self.HOOK)], input="not json",
                               capture_output=True, text=True,
                               env={**os.environ, "AAAGENT_PROJECT": d})
            self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
