"""`aaagent thinking`: thinking levels in the config, the planner command, worker jobs and the CLI."""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import models, session, status, worker  # noqa: E402
from aaagent.config import (PLANNER_THINKING, WORKER_THINKING_FALLBACK, Config,  # noqa: E402
                            project_config_path)

FAKE = ROOT / "tests" / "fake_opencode.py"
DS = "opencode-go/deepseek-v4.1-flash"


class ThinkingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.user_config = Path(self.tmp.name) / "user.toml"
        self.args_log = Path(self.tmp.name) / "args.jsonl"
        self.env_backup = dict(os.environ)
        os.environ.update({
            "AAAGENT_OPENCODE_BIN": str(FAKE),
            "AAAGENT_USER_CONFIG": str(self.user_config),
            "AAAGENT_PROJECT": str(self.project),
            "FAKE_OC_ARGS_LOG": str(self.args_log),
            "FAKE_OC_FAIL": "", "FAKE_OC_NOREPORT": "",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        (self.project / "README.md").write_text("hi\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.project, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.project, check=True)
        session.init_project(self.project, Config.load(self.project))

    def tearDown(self):
        for name in ("deepseek", "glm"):
            worker.stop(self.project, name)
        for _ in range(50):
            if not any(worker.worker_pid(self.project, n) for n in ("deepseek", "glm")):
                break
            time.sleep(0.1)
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def cli(self, *argv) -> tuple[int, str]:
        from aaagent import cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def calls(self) -> list[list[str]]:
        if not self.args_log.exists():
            return []
        return [json.loads(line) for line in self.args_log.read_text().splitlines()]

    # -- the config ----------------------------------------------------------

    def test_defaults_are_empty(self):
        cfg = Config.load(self.project)
        self.assertEqual(cfg.planner_thinking, "")
        self.assertEqual(cfg.worker_thinking("deepseek"), "")
        self.assertEqual(cfg.worker_label("deepseek"), DS)

    def test_unknown_worker_raises(self):
        cfg = Config.load(self.project)
        with self.assertRaises(KeyError):
            cfg.worker_thinking("nope")

    def test_worker_label_includes_the_level(self):
        self.user_config.write_text('[workers.deepseek]\nthinking = "high"\n')
        cfg = Config.load(self.project)
        self.assertEqual(cfg.worker_thinking("deepseek"), "high")
        self.assertEqual(cfg.worker_label("deepseek"), f"{DS}, thinking high")

    # -- choices -------------------------------------------------------------

    def test_choices_come_from_the_opencode_api(self):
        cfg = Config.load(self.project)
        self.assertEqual(models.thinking_choices(cfg, models.PLANNER), PLANNER_THINKING)
        self.assertEqual(models.thinking_choices(cfg, "deepseek"), ["low", "high", "max"])
        self.assertEqual(models.thinking_current(cfg, "deepseek"), "")

    def test_choices_fall_back_when_opencode_is_missing(self):
        os.environ["AAAGENT_OPENCODE_BIN"] = str(Path(self.tmp.name) / "no-opencode")
        cfg = Config.load(self.project)
        self.assertEqual(models.thinking_choices(cfg, "deepseek"), WORKER_THINKING_FALLBACK)

    def test_choices_fall_back_for_a_model_without_variants(self):
        self.user_config.write_text('[workers.deepseek]\nmodel = "opencode-go/nope"\n')
        cfg = Config.load(self.project)
        self.assertEqual(models.thinking_choices(cfg, "deepseek"), WORKER_THINKING_FALLBACK)

    # -- saving --------------------------------------------------------------

    def test_set_worker_thinking_and_clear_it(self):
        notes = models.set_thinking(self.project, False, "deepseek", "high")
        self.assertEqual(tomllib.loads(self.user_config.read_text())["workers"]["deepseek"]["thinking"], "high")
        self.assertEqual(Config.load(self.project).worker_thinking("deepseek"), "high")
        self.assertIn(f"saved to {self.user_config}", notes[0])
        self.assertIn("applies from the next dispatch", notes)
        self.assertFalse(any(line.startswith("warning:") for line in notes), notes)

        notes = models.set_thinking(self.project, False, "deepseek", "default")
        self.assertEqual(tomllib.loads(self.user_config.read_text())["workers"]["deepseek"]["thinking"], "")
        self.assertEqual(Config.load(self.project).worker_thinking("deepseek"), "")
        self.assertIn("thinking: default", notes[0])

    def test_set_planner_thinking_and_clear_it(self):
        code, out = self.cli("thinking", "planner", "xhigh")
        self.assertEqual(code, 0, out)
        self.assertEqual(tomllib.loads(self.user_config.read_text())["orchestrator"]["thinking"], "xhigh")
        self.assertIn("applies when the next chat starts", out)
        cfg = Config.load(self.project)
        cmd = session.claude_command(self.project, cfg, "claude", None)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "xhigh")

        code, out = self.cli("thinking", "planner", "default")
        self.assertEqual(code, 0, out)
        self.assertEqual(Config.load(self.project).planner_thinking, "")
        self.assertNotIn("--effort", session.claude_command(self.project, Config.load(self.project),
                                                           "claude", None))

    def test_set_thinking_in_the_project_config(self):
        code, out = self.cli("thinking", "glm", "low", "--project")
        self.assertEqual(code, 0, out)
        data = tomllib.loads(project_config_path(self.project).read_text())
        self.assertEqual(data["workers"]["glm"]["thinking"], "low")
        self.assertFalse(self.user_config.exists())
        self.assertEqual(Config.load(self.project).worker_thinking("glm"), "low")

    def test_project_config_overrides_the_user_config(self):
        project_config_path(self.project).write_text('[workers.deepseek]\nthinking = "max"\n')
        notes = models.set_thinking(self.project, False, "deepseek", "high")
        self.assertTrue(any("overrides this" in line for line in notes), notes)
        self.assertEqual(Config.load(self.project).worker_thinking("deepseek"), "max")

    def test_unknown_level_warns_but_is_saved(self):
        notes = models.set_thinking(self.project, False, "deepseek", "turbo")
        self.assertTrue(any(line.startswith("warning: turbo is not one of:") for line in notes), notes)
        self.assertEqual(Config.load(self.project).worker_thinking("deepseek"), "turbo")

    # -- the commands --------------------------------------------------------

    def test_claude_command_honours_the_level(self):
        cfg = Config.load(self.project)
        self.assertNotIn("--effort", session.claude_command(self.project, cfg, "claude", None))
        self.user_config.write_text('[orchestrator]\nthinking = "high"\n')
        cfg = Config.load(self.project)
        cmd = session.claude_command(self.project, cfg, "claude", None)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")

    def test_effort_in_extra_args_wins(self):
        self.user_config.write_text('[orchestrator]\nthinking = "high"\n'
                                    'extra_args = ["--effort", "xhigh"]\n')
        cfg = Config.load(self.project)
        cmd = session.claude_command(self.project, cfg, "claude", None)
        self.assertEqual(cmd.count("--effort"), 1)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "xhigh")

    def test_worker_job_passes_the_variant(self):
        cfg = Config.load(self.project)
        report = self.project / ".aaagent" / "tasks" / "t" / "report-1.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        job = {
            "id": "j1", "task": "001-x", "phase": "implement", "round": 1, "worker": "deepseek",
            "model": DS, "thinking": "high", "agent": "aaagent-worker",
            "prompt": f"Write your report to: {report}\nround: 1\n",
            "log": str(report.parent / "log.jsonl"), "timeout": 30,
        }
        result = worker.run_job(self.project, job, cfg)
        self.assertEqual(result["status"], "ok", result.get("error"))
        run = [c for c in self.calls() if c and c[0] == "run"][-1]
        self.assertEqual(run[run.index("-m") + 1], f"{DS}#high")

    def test_worker_job_without_thinking_keeps_the_model(self):
        cfg = Config.load(self.project)
        report = self.project / ".aaagent" / "tasks" / "t" / "report-1.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        job = {
            "id": "j2", "task": "001-x", "phase": "implement", "round": 1, "worker": "deepseek",
            "model": DS, "thinking": "", "agent": "aaagent-worker",
            "prompt": f"Write your report to: {report}\nround: 1\n",
            "log": str(report.parent / "log.jsonl"), "timeout": 30,
        }
        result = worker.run_job(self.project, job, cfg)
        self.assertEqual(result["status"], "ok", result.get("error"))
        run = [c for c in self.calls() if c and c[0] == "run"][-1]
        self.assertEqual(run[run.index("-m") + 1], DS)

    def test_dispatch_passes_the_level_to_the_worker(self):
        from aaagent import dispatch, tasks
        project_config_path(self.project).write_text('[workers.deepseek]\nthinking = "high"\n')
        task = tasks.create(self.project, "add work")
        task.plan.write_text("---\ntask: x\nworker:\n---\n# Goal\nAppend the model name.\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = dispatch.dispatch(self.project, task.id)
        out = buf.getvalue()
        self.assertEqual(code, 0, out)
        self.assertIn(f"{DS}, thinking high", out)
        run = [c for c in self.calls() if c and c[0] == "run"][-1]
        self.assertEqual(run[run.index("-m") + 1], f"{DS}#high")

    # -- the CLI -------------------------------------------------------------

    def test_json_has_thinking_and_choices(self):
        self.user_config.write_text('[orchestrator]\nthinking = "high"\n')
        code, out = self.cli("thinking", "--json")
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertEqual(data["planner"]["model"], "opus")
        self.assertEqual(data["planner"]["thinking"], "high")
        self.assertEqual(data["planner"]["choices"], PLANNER_THINKING)
        self.assertEqual(sorted(data["workers"]), ["deepseek", "glm"])
        self.assertEqual(data["workers"]["deepseek"]["model"], DS)
        self.assertEqual(data["workers"]["deepseek"]["thinking"], "")
        self.assertEqual(data["workers"]["deepseek"]["choices"], ["low", "high", "max"])

    def test_list_prints_the_choices(self):
        code, out = self.cli("thinking", "--list", "planner")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), PLANNER_THINKING)
        code, out = self.cli("thinking", "--list", "deepseek")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), ["low", "high", "max"])

    def test_table_has_a_target_model_thinking_header(self):
        code, out = self.cli("thinking")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("target"), lines)
        self.assertRegex(lines[1], r"^planner\s+opus\s+default$")
        self.assertRegex(lines[2], r"^deepseek\s+\S+\s+default$")

    def test_unknown_target_exits_2(self):
        self.assertEqual(self.cli("thinking", "nope", "high")[0], 2)

    def test_pick_interactive(self):
        cfg = Config.load(self.project)
        printed = []

        def answer(value):
            return lambda prompt: value

        self.assertIsNone(models.pick_thinking_interactive(cfg, "deepseek", answer(""), printed.append))
        self.assertEqual(models.pick_thinking_interactive(cfg, "deepseek", answer("0"), printed.append),
                         "default")
        self.assertEqual(models.pick_thinking_interactive(cfg, "deepseek", answer("2"), printed.append),
                         "high")
        self.assertEqual(models.pick_thinking_interactive(cfg, "deepseek", answer("turbo"), printed.append),
                         "turbo")
        self.assertIsNone(models.pick_thinking_interactive(cfg, "deepseek", answer("9"), printed.append))
        self.assertIn("  0. default  (current)", printed)
        self.assertIn("  2. high", printed)
        self.assertTrue(any(line.startswith("error:") for line in printed))

    # -- status --------------------------------------------------------------

    def test_status_data_has_thinking(self):
        self.user_config.write_text('[orchestrator]\nthinking = "xhigh"\n'
                                    '[workers.deepseek]\nthinking = "high"\n')
        cfg = Config.load(self.project)
        data = status.status_data(self.project, cfg)
        self.assertEqual(data["planner"]["thinking"], "xhigh")
        by_name = {w["name"]: w for w in data["workers"]}
        self.assertEqual(by_name["deepseek"]["thinking"], "high")
        self.assertEqual(by_name["glm"]["thinking"], "")


if __name__ == "__main__":
    unittest.main()
