"""`aaagent reviewer`: showing and changing who reviews each round."""

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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import models, status  # noqa: E402
from aaagent.config import Config, project_config_path  # noqa: E402

FAKE = ROOT / "tests" / "fake_opencode.py"


class ReviewerTest(unittest.TestCase):
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
        # No interactive prompt, even when the test runner has a terminal.
        with redirect_stdout(out), redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO()):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_show_defaults_to_the_chat(self):
        code, out = self.cli("reviewer")
        self.assertEqual(code, 0, out)
        self.assertIn("reviewer: orchestrator (the chat reviews each round)", out)

    def test_set_a_worker_in_the_user_config(self):
        code, out = self.cli("reviewer", "glm")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.user_config.is_file())
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(data["flow"]["reviewer"], "glm")
        self.assertEqual(Config.load(self.project).reviewer_worker, "glm")
        self.assertIn("reviewer: glm (opencode-go/glm-5.3-flash)", out)
        self.assertIn("one dispatch now runs implement → review → fix until approval", out)
        self.assertIn("applies from the next dispatch", out)

    def test_set_a_worker_in_the_project_config(self):
        code, out = self.cli("reviewer", "glm", "--project")
        self.assertEqual(code, 0, out)
        path = project_config_path(self.project)
        self.assertEqual(tomllib.loads(path.read_text())["flow"]["reviewer"], "glm")
        self.assertFalse(self.user_config.exists())
        self.assertEqual(Config.load(self.project).reviewer_worker, "glm")

    def test_an_alias_gives_the_job_back_to_the_chat(self):
        self.cli("reviewer", "glm")
        code, out = self.cli("reviewer", "planner")
        self.assertEqual(code, 0, out)
        self.assertEqual(tomllib.loads(self.user_config.read_text())["flow"]["reviewer"], "orchestrator")
        self.assertIsNone(Config.load(self.project).reviewer_worker)
        self.assertIn("reviewer: orchestrator (the chat reviews each round)", out)

    def test_unknown_name_exits_2(self):
        code, out = self.cli("reviewer", "nope")
        self.assertEqual(code, 2, out)
        self.assertFalse(self.user_config.exists())

    def test_a_worker_named_like_an_alias_wins(self):
        project_config_path(self.project).write_text('[workers.chat]\nmodel = "sonnet"\n')
        cfg = Config.load(self.project)
        self.assertEqual(models.resolve_reviewer(cfg, "chat"), "chat")
        self.assertEqual(models.resolve_reviewer(cfg, "planner"), models.ORCHESTRATOR)

    def test_json_has_the_reviewer_and_choices(self):
        code, out = self.cli("reviewer", "--json")
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertEqual(data["reviewer"], "orchestrator")
        self.assertEqual(data["configured"], "orchestrator")
        self.assertEqual(data["choices"], ["orchestrator", "deepseek", "glm"])
        self.assertEqual(sorted(data["workers"]), ["deepseek", "glm"])
        self.assertEqual(data["workers"]["glm"]["model"], "opencode-go/glm-5.3-flash")
        self.assertEqual(data["workers"]["glm"]["backend"], "opencode")

    def test_status_text_and_data(self):
        code, out = self.cli("status")
        self.assertEqual(code, 0, out)
        self.assertNotIn("reviewer:", out)
        self.assertEqual(status.status_data(self.project, Config.load(self.project))["reviewer"],
                         "orchestrator")

        self.cli("reviewer", "glm")
        code, out = self.cli("status")
        self.assertIn("reviewer: glm", out)
        self.assertEqual(status.status_data(self.project, Config.load(self.project))["reviewer"], "glm")

    def test_unknown_configured_reviewer_warns(self):
        project_config_path(self.project).write_text('[flow]\nreviewer = "nope"\n')
        code, out = self.cli("reviewer")
        self.assertEqual(code, 0, out)
        self.assertIn('warning: flow.reviewer = "nope" is not a configured worker', out)
        self.assertIn("reviewer: orchestrator (the chat reviews each round)", out)


if __name__ == "__main__":
    unittest.main()
