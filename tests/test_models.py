"""`aaagent model`: writing model choices to the config files, and the CLI around it."""

import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import models, status  # noqa: E402
from aaagent.config import (PLANNER_CHOICES, Config, project_config_path,  # noqa: E402
                            set_toml_value)

FAKE = ROOT / "tests" / "fake_opencode.py"


class TomlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_file(self):
        set_toml_value(self.path, "orchestrator", "model", "sonnet")
        self.assertEqual(self.path.read_text(), '[orchestrator]\nmodel = "sonnet"\n')

    def test_existing_section_replaces_the_key_and_keeps_the_rest(self):
        self.path.write_text('# top\n[orchestrator]\n# the model\nmodel = "opus"  # current\n'
                             'extra_args = []\n\n[flow]\ndefault_worker = "deepseek"\n')
        set_toml_value(self.path, "orchestrator", "model", "sonnet")
        text = self.path.read_text()
        data = tomllib.loads(text)
        self.assertEqual(data["orchestrator"]["model"], "sonnet")
        self.assertEqual(data["orchestrator"]["extra_args"], [])
        self.assertEqual(data["flow"]["default_worker"], "deepseek")
        self.assertIn("# top", text)
        self.assertIn("# the model", text)
        self.assertIn('model = "sonnet"', text)

    def test_existing_section_without_the_key_inserts_after_it(self):
        self.path.write_text('[orchestrator]\nextra_args = ["--effort", "high"]\n\n'
                             '[flow]\ndefault_worker = "deepseek"\n')
        set_toml_value(self.path, "orchestrator", "model", "sonnet")
        data = tomllib.loads(self.path.read_text())
        self.assertEqual(data["orchestrator"]["model"], "sonnet")
        self.assertEqual(data["orchestrator"]["extra_args"], ["--effort", "high"])
        self.assertEqual(data["flow"]["default_worker"], "deepseek")
        self.assertLess(self.path.read_text().index('model = "sonnet"'),
                        self.path.read_text().index("[flow]"))

    def test_existing_section_gets_a_new_key(self):
        self.path.write_text('[orchestrator]\nmodel = "opus"\n\n[flow]\ndefault_worker = "x"\n')
        set_toml_value(self.path, "orchestrator", "claude_bin", "/opt/claude")
        data = tomllib.loads(self.path.read_text())
        self.assertEqual(data["orchestrator"], {"model": "opus", "claude_bin": "/opt/claude"})
        self.assertEqual(data["flow"]["default_worker"], "x")

    def test_inline_table_cannot_be_updated(self):
        original = 'workers = { deepseek = { model = "x" } }\n'
        self.path.write_text(original)
        with self.assertRaises(ValueError):
            set_toml_value(self.path, "workers.deepseek", "model", "y")
        self.assertEqual(self.path.read_text(), original)


class ModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.user_config = Path(self.tmp.name) / "user.toml"
        self.env_backup = dict(os.environ)
        os.environ.update({
            "AAAGENT_OPENCODE_BIN": str(FAKE),
            "AAAGENT_USER_CONFIG": str(self.user_config),
            "AAAGENT_PROJECT": str(self.project),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        (self.project / "README.md").write_text("hi\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.project, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.project, check=True)
        from aaagent import session
        session.init_project(self.project, Config.load(self.project))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def cli(self, *argv) -> tuple[int, str]:
        from aaagent import cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue()

    # -- planner -------------------------------------------------------------

    def test_set_planner_in_the_user_config(self):
        code, out = self.cli("model", "planner", "sonnet")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.user_config.is_file())
        self.assertEqual(tomllib.loads(self.user_config.read_text())["orchestrator"]["model"], "sonnet")
        self.assertEqual(Config.load(self.project).planner_model, "sonnet")
        self.assertIn("saved to", out)
        self.assertIn("/model sonnet", out)

    def test_set_planner_in_the_project_config(self):
        code, out = self.cli("model", "planner", "haiku", "--project")
        self.assertEqual(code, 0, out)
        path = project_config_path(self.project)
        self.assertEqual(tomllib.loads(path.read_text())["orchestrator"]["model"], "haiku")
        self.assertFalse(self.user_config.exists())
        self.assertEqual(Config.load(self.project).planner_model, "haiku")

    def test_project_config_overrides_the_user_config(self):
        path = project_config_path(self.project)
        path.write_text('[orchestrator]\nmodel = "haiku"\n')
        code, out = self.cli("model", "planner", "sonnet")
        self.assertEqual(code, 0, out)
        self.assertIn("overrides this", out)
        self.assertEqual(Config.load(self.project).planner_model, "haiku")

    # -- workers -------------------------------------------------------------

    def test_set_worker_model(self):
        code, out = self.cli("model", "deepseek", "opencode-go/kimi-k3")
        self.assertEqual(code, 0, out)
        cfg = Config.load(self.project)
        self.assertEqual(cfg.worker_model("deepseek"), "opencode-go/kimi-k3")
        self.assertEqual(cfg.worker_model("glm"), "opencode-go/glm-5.3-flash")
        self.assertIn("next dispatch", out)
        self.assertNotIn("warning", out)

    def test_unknown_target_exits_2(self):
        self.assertEqual(self.cli("model", "nope", "x")[0], 2)

    def test_unknown_model_warns_but_is_saved(self):
        code, out = self.cli("model", "deepseek", "opencode-go/nope")
        self.assertEqual(code, 0, out)
        self.assertIn("warning", out)
        self.assertEqual(Config.load(self.project).worker_model("deepseek"), "opencode-go/nope")

    # -- listing -------------------------------------------------------------

    def test_json_has_planner_workers_and_choices(self):
        code, out = self.cli("model", "--json")
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertEqual(data["planner"]["model"], "opus")
        self.assertIn("opus", data["planner"]["choices"])
        self.assertEqual(sorted(data["workers"]), ["deepseek", "glm"])
        self.assertEqual(data["workers"]["deepseek"]["model"], "opencode-go/deepseek-v4.1-flash")
        self.assertIn("opencode-go/kimi-k3", data["worker_choices"])

    def test_list_prints_the_choices(self):
        code, out = self.cli("model", "--list", "planner")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), PLANNER_CHOICES)

    def test_pick_interactive(self):
        cfg = Config.load(self.project)
        printed = []

        def ok_input(answer):
            return lambda prompt: answer

        self.assertEqual(models.pick_interactive(cfg, models.PLANNER, ok_input("2"), printed.append),
                         PLANNER_CHOICES[1])
        self.assertIn(f"  1. {PLANNER_CHOICES[0]}  (current)", printed)
        self.assertEqual(models.pick_interactive(cfg, models.PLANNER, ok_input("claude-opus-5-5"),
                                                 printed.append), "claude-opus-5-5")
        self.assertIsNone(models.pick_interactive(cfg, models.PLANNER, ok_input(""), printed.append))

        answers = iter(["9", "99", "3"])
        self.assertEqual(models.pick_interactive(cfg, models.PLANNER, lambda prompt: next(answers),
                                                 printed.append), PLANNER_CHOICES[2])
        self.assertTrue(any(line.startswith("error:") for line in printed))
        self.assertIsNone(models.pick_interactive(cfg, models.PLANNER,
                                                  lambda prompt: "9", printed.append))

    # -- one chat and status -------------------------------------------------

    def test_model_flag_routes_to_start(self):
        from unittest import mock
        from aaagent import cli, session
        with mock.patch.object(session, "launch", return_value=0) as launch:
            self.assertEqual(cli.main(["--model", "sonnet"]), 0)
        self.assertEqual(launch.call_args.kwargs["model"], "sonnet")

    def test_claude_command_uses_the_chat_model(self):
        from aaagent import session
        cfg = Config.load(self.project)
        cmd = session.claude_command(self.project, cfg, "claude", None, model="haiku")
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        cmd = session.claude_command(self.project, cfg, "claude", None)
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")

    def test_status_data_has_the_planner_model(self):
        cfg = Config.load(self.project)
        self.assertEqual(status.status_data(self.project, cfg)["planner"]["model"], "opus")


if __name__ == "__main__":
    unittest.main()
