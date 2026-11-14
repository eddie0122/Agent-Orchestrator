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
from aaagent.config import REVIEWER, Config, project_config_path  # noqa: E402

FAKE = ROOT / "tests" / "fake_opencode.py"
DS, GLM = "opencode-go/deepseek-v4.1-flash", "opencode-go/glm-5.3-flash"


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
        self.last_err = err.getvalue()
        return code, out.getvalue()

    # -- showing ------------------------------------------------------------

    def test_show_defaults_to_the_chat(self):
        code, out = self.cli("reviewer")
        self.assertEqual(code, 0, out)
        self.assertIn("reviewer: planner (the chat reviews each round)", out)

    def test_show_a_model_reviewer(self):
        self.cli("reviewer", "sonnet", "--thinking", "high")
        code, out = self.cli("reviewer")
        self.assertEqual(code, 0, out)
        self.assertIn("reviewer: reviewer (sonnet, thinking high)", out)

    def test_a_legacy_flow_reviewer_names_the_chat_when_unknown(self):
        project_config_path(self.project).write_text('[flow]\nreviewer = "nope"\n')
        code, out = self.cli("reviewer")
        self.assertEqual(code, 0, out)
        self.assertIn('warning: flow.reviewer = "nope" is not a configured worker', out)
        self.assertIn("reviewer: planner (the chat reviews each round)", out)

    # -- changing -----------------------------------------------------------

    def test_set_a_model_in_the_user_config(self):
        code, out = self.cli("reviewer", GLM)
        self.assertEqual(code, 0, out)
        self.assertTrue(self.user_config.is_file())
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(data[REVIEWER]["model"], GLM)
        cfg = Config.load(self.project)
        self.assertEqual(cfg.reviewer_model, GLM)
        self.assertEqual(cfg.reviewer_worker, REVIEWER)
        self.assertFalse(cfg.reviewer_is_planner)
        self.assertIn(f"reviewer: reviewer ({GLM})", out)
        self.assertIn("one dispatch now runs implement → review → fix until approval", out)
        self.assertIn("applies from the next dispatch", out)

    def test_set_a_model_in_the_project_config(self):
        code, out = self.cli("reviewer", GLM, "--project")
        self.assertEqual(code, 0, out)
        path = project_config_path(self.project)
        self.assertEqual(tomllib.loads(path.read_text())[REVIEWER]["model"], GLM)
        self.assertFalse(self.user_config.exists())
        self.assertEqual(Config.load(self.project).reviewer_model, GLM)

    def test_set_a_worker_copies_its_model_and_thinking(self):
        self.user_config.write_text(f'[workers.worker1]\nmodel = "{DS}"\nthinking = "high"\n'
                                    f'[flow]\nreviewer = "worker1"\n')
        code, out = self.cli("reviewer", "worker1")
        self.assertEqual(code, 0, out)
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(data[REVIEWER],
                         {"model": DS, "thinking": "high", "backend": ""})
        self.assertEqual(data["flow"]["reviewer"], "",
                         "the legacy key is cleared in the same file")
        self.assertEqual(Config.load(self.project).reviewer_model, DS)
        self.assertIn("reviewer now uses worker1's model "
                      f"{DS} (a copy; changing worker1 later does not change the reviewer)", out)

    def test_set_a_worker_does_not_copy_an_inferred_backend(self):
        self.user_config.write_text(f'[workers.worker2]\nmodel = "{GLM}"\n')
        self.cli("reviewer", "worker2")
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(data[REVIEWER]["backend"], "",
                         "the backend is inferred from the model like a worker's")

    def test_copying_a_worker_clears_a_stale_thinking_and_backend(self):
        self.user_config.write_text('[reviewer]\nmodel = "sonnet"\nthinking = "high"\n'
                                    'backend = "claude"\n')
        cfg = Config.load(self.project)
        self.assertEqual((cfg.reviewer_thinking, cfg.reviewer_backend), ("high", "claude"))

        code, out = self.cli("reviewer", "worker1")
        self.assertEqual(code, 0, out)
        cfg = Config.load(self.project)
        self.assertEqual(cfg.reviewer_model, DS)
        self.assertEqual(cfg.reviewer_thinking, "", "worker1 has no thinking level")
        self.assertEqual(cfg.reviewer_backend, "opencode",
                         "no leftover backend: it is inferred from worker1's model")
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(data[REVIEWER], {"model": DS, "thinking": "", "backend": ""})

    def test_copying_a_worker_keeps_an_explicit_backend(self):
        self.user_config.write_text(f'[workers.worker2]\nmodel = "sonnet"\nbackend = "claude"\n')
        code, out = self.cli("reviewer", "worker2")
        self.assertEqual(code, 0, out)
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(data[REVIEWER],
                         {"model": "sonnet", "thinking": "", "backend": "claude"})

    def test_changing_the_reviewer_model_clears_a_stale_backend(self):
        self.user_config.write_text('[reviewer]\nmodel = "sonnet"\nbackend = "claude"\n')
        code, out = self.cli("model", "reviewer", GLM)
        self.assertEqual(code, 0, out)
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(data[REVIEWER]["model"], GLM)
        self.assertEqual(data[REVIEWER]["backend"], "")
        cfg = Config.load(self.project)
        self.assertEqual(cfg.reviewer_backend, "opencode",
                         "the backend is inferred from the new model")

    def test_planner_gives_the_job_back_to_the_chat(self):
        self.cli("reviewer", "worker2")
        code, out = self.cli("reviewer", "planner")
        self.assertEqual(code, 0, out)
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(data[REVIEWER]["model"], "planner")
        self.assertIsNone(Config.load(self.project).reviewer_worker)
        self.assertTrue(Config.load(self.project).reviewer_is_planner)
        self.assertIn("reviewer: planner (the chat reviews each round)", out)

    def test_aliases_all_mean_the_planner(self):
        for alias in ("orchestrator", "chat"):
            code, out = self.cli("reviewer", alias)
            self.assertEqual(code, 0, out)
            data = tomllib.loads(self.user_config.read_text())
            self.assertEqual(data[REVIEWER]["model"], "planner")

    def test_a_worker_named_like_an_alias_wins(self):
        project_config_path(self.project).write_text('[workers.chat]\nmodel = "sonnet"\n')
        code, out = self.cli("reviewer", "chat", "--project")
        self.assertEqual(code, 0, out)
        self.assertEqual(tomllib.loads(project_config_path(self.project).read_text())
                         [REVIEWER]["model"], "sonnet")

    def test_set_with_thinking(self):
        code, out = self.cli("reviewer", "sonnet", "--thinking", "high")
        self.assertEqual(code, 0, out)
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(data[REVIEWER],
                         {"model": "sonnet", "thinking": "high", "backend": ""})
        cfg = Config.load(self.project)
        self.assertEqual(cfg.reviewer_backend, "claude")
        self.assertEqual(cfg.reviewer_thinking, "high")

    def test_set_thinking_on_a_model_reviewer(self):
        self.cli("reviewer", GLM)
        code, out = self.cli("thinking", "reviewer", "high")
        self.assertEqual(code, 0, out)
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(data[REVIEWER],
                         {"model": GLM, "thinking": "high", "backend": ""})

    def test_thinking_for_the_planner_reviewer_is_rejected(self):
        code, out = self.cli("thinking", "reviewer", "high")
        self.assertEqual(code, 2, out)
        self.assertIn("the planner reviews", self.last_err)

    def test_empty_value_is_an_error(self):
        from aaagent.tasks import FlowError
        with self.assertRaises(FlowError):
            models.set_reviewer(self.project, False, "  ")

    # -- model / thinking commands on the reviewer --------------------------

    def test_model_reviewer_sets_the_model(self):
        code, out = self.cli("model", "reviewer", GLM)
        self.assertEqual(code, 0, out)
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(data[REVIEWER]["model"], GLM)
        self.assertIn(f"reviewer: reviewer ({GLM})", out)

    def test_model_reviewer_planner_alias(self):
        self.cli("reviewer", "sonnet")
        code, out = self.cli("model", "reviewer", "planner")
        self.assertEqual(code, 0, out)
        self.assertEqual(tomllib.loads(self.user_config.read_text())[REVIEWER]["model"], "planner")
        self.assertTrue(Config.load(self.project).reviewer_is_planner)

    # -- json ---------------------------------------------------------------

    def test_json_has_the_reviewer_and_choices(self):
        code, out = self.cli("reviewer", "--json")
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertEqual(data["reviewer"], "orchestrator")
        self.assertTrue(data["planner_reviews"])
        self.assertEqual(data["model"], "planner")
        self.assertEqual(data["thinking"], "")
        self.assertIsNone(data["backend"])
        self.assertEqual(data["choices"], ["planner", DS, GLM, "opencode-go/kimi-k3",
                                           "opus", "sonnet", "haiku", "fable",
                                           "claude-opus-5-5", "claude-sonnet-5-5",
                                           "claude-haiku-5-5", "claude-fable-5-2"])
        self.assertEqual(sorted(data["workers"]), ["worker1", "worker2"])
        self.assertEqual(data["workers"]["worker2"]["model"], GLM)
        self.assertEqual(data["workers"]["worker2"]["backend"], "opencode")

    def test_json_with_a_model_reviewer(self):
        self.cli("reviewer", "sonnet", "--thinking", "high")
        code, out = self.cli("reviewer", "--json")
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertEqual(data["reviewer"], REVIEWER)
        self.assertFalse(data["planner_reviews"])
        self.assertEqual(data["model"], "sonnet")
        self.assertEqual(data["thinking"], "high")
        self.assertEqual(data["backend"], "claude")
        self.assertIn("sonnet", data["choices"])

    # -- status -------------------------------------------------------------

    def test_status_text_and_data(self):
        code, out = self.cli("status")
        self.assertEqual(code, 0, out)
        self.assertIn("reviewer: planner (the chat reviews each round)", out)
        data = status.status_data(self.project, Config.load(self.project))
        self.assertEqual(data["reviewer"], "orchestrator")
        self.assertTrue(data["review"]["planner_reviews"])
        self.assertEqual(data["review"]["model"], "planner")
        self.assertEqual(data["review"]["state"], "stopped")
        self.assertIsNone(data["review"]["pid"])
        self.assertIsNone(data["review"]["backend"])

        self.cli("reviewer", GLM)
        code, out = self.cli("status")
        self.assertIn(f"reviewer: reviewer ({GLM})", out)
        self.assertIn(f"  reviewer  {GLM}", out, "the reviewer daemon line is shown like a worker's")
        data = status.status_data(self.project, Config.load(self.project))
        self.assertEqual(data["reviewer"], REVIEWER)
        review = data["review"]
        self.assertFalse(review["planner_reviews"])
        self.assertEqual(review["model"], GLM)
        self.assertEqual(review["backend"], "opencode")
        self.assertEqual(review["state"], "stopped")
        self.assertIn(f"{self.project}/.aaagent/workers/reviewer.log", review["log"])

    def test_reviewer_row_of_the_dashboard(self):
        data = status.status_data(self.project, Config.load(self.project))
        self.assertEqual(status.reviewer_summary(data), "planner (the chat)")
        self.cli("reviewer", GLM, "--thinking", "high")
        data = status.status_data(self.project, Config.load(self.project))
        self.assertEqual(status.reviewer_summary(data), "reviewer · glm-5.3-flash · high")

    # -- the reserved name --------------------------------------------------

    def test_reviewer_is_not_a_worker(self):
        project_config_path(self.project).write_text('[workers.reviewer]\nmodel = "sonnet"\n')
        cfg = Config.load(self.project)
        self.assertEqual(cfg.worker_names(), ["worker1", "worker2"],
                         "[workers.reviewer] is dropped: reviewer is a reserved name")
        with self.assertRaises(KeyError):
            cfg.worker_model("reviewer")

    def test_workers_add_never_creates_a_reviewer(self):
        code, out = self.cli("workers", "add", "opencode-go/kimi-k3")
        self.assertEqual(code, 0, out)
        self.assertEqual(Config.load(self.project).worker_names(),
                         ["worker1", "worker2", "worker3"])

    def test_remove_worker_rejects_the_name(self):
        code, out = self.cli("workers", "remove", "reviewer")
        self.assertEqual(code, 2)
        self.assertIn("unknown worker 'reviewer'", self.last_err)

    # -- legacy ---------------------------------------------------------------

    def test_legacy_flow_reviewer_falls_back_to_the_workers_model(self):
        project_config_path(self.project).write_text('[flow]\nreviewer = "worker2"\n')
        cfg = Config.load(self.project)
        self.assertEqual(cfg.reviewer_model, GLM)
        self.assertEqual(cfg.reviewer_thinking, "")
        self.assertEqual(cfg.reviewer_backend, "opencode")
        self.assertEqual(cfg.reviewer_worker, REVIEWER)
        self.assertEqual(cfg.reviewer, REVIEWER)
        self.assertEqual(cfg.worker_label(REVIEWER), GLM)

    def test_setting_the_reviewer_clears_a_legacy_key_in_the_same_file(self):
        project_config_path(self.project).write_text('[flow]\nreviewer = "worker2"\n')
        code, out = self.cli("reviewer", "sonnet", "--project")
        self.assertEqual(code, 0, out)
        data = tomllib.loads(project_config_path(self.project).read_text())
        self.assertEqual(data[REVIEWER]["model"], "sonnet")
        self.assertEqual(data["flow"]["reviewer"], "")
        self.assertIn('note: flow.reviewer was "worker2"; cleared', out)


if __name__ == "__main__":
    unittest.main()
