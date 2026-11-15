"""Generic worker names: the seed rule, `aaagent workers`, add/remove, and the migration."""

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

from aaagent import config, models, tasks, worker  # noqa: E402
from aaagent.config import (Config, project_config_path, remove_toml_section,  # noqa: E402
                            rename_toml_section, seed_workers, set_toml_value,
                            user_config_path)
from aaagent.tasks import FlowError  # noqa: E402

FAKE = ROOT / "tests" / "fake_opencode.py"
DS, GLM = "opencode-go/deepseek-v4.1-flash", "opencode-go/glm-5.3-flash"
PLAN = "---\ntask: x\nworker:\n---\n# Goal\nAppend the model name to work.txt.\n"


class TomlHelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def test_remove_keeps_other_sections_and_comments(self):
        self.path.write_text('# top\n[workers]\nopencode_bin = ""\n\n'
                             '[workers.worker1]  # the first one\nmodel = "a"\nthinking = ""\n\n'
                             '[flow]\nreviewer = "orchestrator"\n')
        remove_toml_section(self.path, "workers.worker1")
        text = self.path.read_text()
        data = tomllib.loads(text)
        self.assertEqual(data["workers"]["opencode_bin"], "")
        self.assertNotIn("worker1", data["workers"])
        self.assertEqual(data["flow"]["reviewer"], "orchestrator")
        self.assertIn("# top", text)
        self.assertNotIn("the first one", text)

    def test_remove_the_last_section(self):
        self.path.write_text('[flow]\nreviewer = "orchestrator"\n\n[workers.worker2]\nmodel = "b"\n')
        remove_toml_section(self.path, "workers.worker2")
        data = tomllib.loads(self.path.read_text())
        self.assertEqual(data, {"flow": {"reviewer": "orchestrator"}})

    def test_remove_a_missing_section_fails(self):
        self.path.write_text('[flow]\nreviewer = "orchestrator"\n')
        original = self.path.read_text()
        with self.assertRaises(ValueError):
            remove_toml_section(self.path, "workers.worker9")
        self.assertEqual(self.path.read_text(), original)

    def test_rename_keeps_comments_and_the_table_body(self):
        self.path.write_text('[workers.worker1]  # first\nmodel = "a"\n\n# between\n[flow]\n'
                             'default_worker = "worker1"\n')
        rename_toml_section(self.path, "workers.worker1", "workers.worker9")
        text = self.path.read_text()
        data = tomllib.loads(text)
        self.assertEqual(data["workers"]["worker9"]["model"], "a")
        self.assertNotIn("worker1", data["workers"])
        self.assertEqual(data["flow"]["default_worker"], "worker1")
        self.assertIn("# first", text)
        self.assertIn("[workers.worker9]  # first", text)
        self.assertIn("# between", text)

    def test_rename_a_missing_or_inline_table_fails(self):
        self.path.write_text('workers = { worker1 = { model = "a" } }\n')
        original = self.path.read_text()
        with self.assertRaises(ValueError):
            rename_toml_section(self.path, "workers.worker1", "workers.worker2")
        self.assertEqual(self.path.read_text(), original)
        with self.assertRaises(ValueError):
            rename_toml_section(self.path, "workers.nope", "workers.worker2")


class WorkersTest(unittest.TestCase):
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

    def cli(self, *argv) -> tuple[int, str, str]:
        from aaagent import cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    # -- the seed rule --------------------------------------------------------

    def test_seed_is_used_when_no_config_defines_workers(self):
        cfg = Config.load(self.project)
        self.assertEqual(cfg.worker_names(), ["worker1", "worker2"])
        self.assertEqual(cfg.worker_model("worker1"), DS)
        self.assertEqual(cfg.worker_model("worker2"), GLM)

    def test_the_seed_is_dropped_once_the_user_config_defines_a_worker(self):
        self.user_config.write_text('[workers.worker1]\nmodel = "opencode-go/kimi-k3"\n')
        cfg = Config.load(self.project)
        self.assertEqual(cfg.worker_names(), ["worker1"])
        self.assertEqual(cfg.worker_model("worker1"), "opencode-go/kimi-k3")

    def test_the_project_config_defines_workers_too(self):
        project_config_path(self.project).write_text(
            '[workers.worker1]\nmodel = "sonnet"\n')
        cfg = Config.load(self.project)
        self.assertEqual(cfg.worker_names(), ["worker1"])
        self.assertEqual(cfg.worker_model("worker1"), "sonnet")

    def test_user_and_project_worker_tables_merge_per_field(self):
        self.user_config.write_text('[workers.worker1]\nmodel = "opencode-go/kimi-k3"\n'
                                    'thinking = "high"\n')
        project_config_path(self.project).write_text('[workers.worker1]\nthinking = "max"\n')
        cfg = Config.load(self.project)
        self.assertEqual(cfg.worker_model("worker1"), "opencode-go/kimi-k3")
        self.assertEqual(cfg.worker_thinking("worker1"), "max")

    def test_workers_settings_still_merge_from_all_files(self):
        self.user_config.write_text('[workers]\nimplement_agent = "mine"\n')
        cfg = Config.load(self.project)
        self.assertEqual(cfg.implement_agent, "mine")
        self.assertEqual(cfg.worker_names(), ["worker1", "worker2"])

    # -- default, fallback and alternate --------------------------------------

    def test_defaults_come_from_the_first_and_next_worker(self):
        cfg = Config.load(self.project)
        self.assertEqual((cfg.default_worker, cfg.fallback_worker), ("worker1", "worker2"))
        self.assertEqual(cfg.alternate("worker1"), "worker2")
        self.assertEqual(cfg.alternate("worker2"), "worker1")

    def test_one_worker_means_no_fallback(self):
        self.user_config.write_text('[workers.solo]\nmodel = "sonnet"\n')
        cfg = Config.load(self.project)
        self.assertEqual((cfg.default_worker, cfg.fallback_worker), ("solo", "solo"))
        self.assertIsNone(cfg.alternate("solo"))

    def test_three_workers_derive_the_roles(self):
        self.user_config.write_text('[workers.worker1]\nmodel = "a"\n'
                                    '[workers.worker2]\nmodel = "b"\n'
                                    '[workers.worker3]\nmodel = "c"\n')
        cfg = Config.load(self.project)
        self.assertEqual((cfg.default_worker, cfg.fallback_worker), ("worker1", "worker2"))
        self.assertEqual(cfg.alternate("worker2"), "worker1")

    def test_configured_names_win_over_the_derivation(self):
        self.user_config.write_text('[workers.worker1]\nmodel = "a"\n'
                                    '[workers.worker2]\nmodel = "b"\n'
                                    '[flow]\ndefault_worker = "worker2"\n')
        cfg = Config.load(self.project)
        self.assertEqual((cfg.default_worker, cfg.fallback_worker), ("worker2", "worker1"))

    def test_an_unknown_configured_name_falls_back_to_the_list(self):
        self.user_config.write_text('[workers.worker1]\nmodel = "a"\n'
                                    '[flow]\ndefault_worker = "gone"\nfallback_worker = "gone"\n')
        cfg = Config.load(self.project)
        self.assertEqual((cfg.default_worker, cfg.fallback_worker), ("worker1", "worker1"))

    # -- the CLI --------------------------------------------------------------

    def test_workers_table(self):
        code, out, err = self.cli("workers")
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("name"), lines)
        self.assertIn("worker1", lines[1])
        self.assertIn("opencode-go/deepseek-v4.1-flash", lines[1])
        self.assertIn("default", lines[1])
        self.assertIn("worker2", lines[2])
        self.assertIn("opencode-go/glm-5.3-flash", lines[2])
        self.assertIn("fallback", lines[2])

    def test_workers_json(self):
        code, out, err = self.cli("workers", "--json")
        self.assertEqual(code, 0, out + err)
        data = json.loads(out)
        self.assertEqual([w["name"] for w in data["workers"]], ["worker1", "worker2"])
        self.assertEqual(data["workers"][0]["model"], DS)
        self.assertTrue(data["workers"][0]["default"])
        self.assertFalse(data["workers"][0]["fallback"])
        self.assertEqual((data["default"], data["fallback"]), ("worker1", "worker2"))

    def test_add_creates_the_next_free_name_and_materializes_the_seed(self):
        code, out, err = self.cli("workers", "add", "opencode-go/kimi-k3")
        self.assertEqual(code, 0, out + err)
        self.assertIn("added worker3: opencode-go/kimi-k3", out)
        self.assertIn("saved to", out)
        self.assertIn("next time aaagent starts", out)
        user = tomllib.loads(self.user_config.read_text())
        self.assertEqual(user["workers"]["worker3"]["model"], "opencode-go/kimi-k3")
        self.assertEqual(sorted(user["workers"]), ["worker1", "worker2", "worker3"])
        self.assertEqual(Config.load(self.project).worker_names(),
                         ["worker1", "worker2", "worker3"])
        data = json.loads(self.cli("workers", "--json")[1])
        self.assertEqual([w["name"] for w in data["workers"]],
                         ["worker1", "worker2", "worker3"])

    def test_add_needs_a_model(self):
        self.assertEqual(self.cli("workers", "add")[0], 2)

    def test_add_with_thinking(self):
        code, out, err = self.cli("workers", "add", "opencode-go/kimi-k3", "--thinking", "high")
        self.assertEqual(code, 0, out + err)
        user = tomllib.loads(self.user_config.read_text())
        self.assertEqual(user["workers"]["worker3"]["thinking"], "high")

    def test_add_in_the_project_config(self):
        code, out, err = self.cli("workers", "add", "sonnet", "--project")
        self.assertEqual(code, 0, out + err)
        project = tomllib.loads(project_config_path(self.project).read_text())
        self.assertEqual(project["workers"]["worker3"]["model"], "sonnet")
        # the seed was materialized, so the other workers survive the project table
        self.assertEqual(Config.load(self.project).worker_names(),
                         ["worker1", "worker2", "worker3"])

    def test_remove_a_worker(self):
        code, out, err = self.cli("workers", "remove", "worker2")
        self.assertEqual(code, 0, out + err)
        self.assertIn("removed worker2", out)
        user = tomllib.loads(self.user_config.read_text())
        self.assertEqual(sorted(user["workers"]), ["worker1"])
        self.assertEqual(Config.load(self.project).worker_names(), ["worker1"])

    def test_removing_one_of_the_seed_workers_leaves_the_other(self):
        code, out, err = self.cli("workers", "remove", "worker1")
        self.assertEqual(code, 0, out + err)
        self.assertIn("removed worker1", out)
        self.assertEqual(Config.load(self.project).worker_names(), ["worker2"])

    def test_remove_the_last_worker_fails(self):
        self.user_config.write_text('[workers.solo]\nmodel = "sonnet"\n')
        code, out, err = self.cli("workers", "remove", "solo")
        self.assertEqual(code, 2, out + err)
        self.assertIn("only worker", err)
        self.assertEqual(Config.load(self.project).worker_names(), ["solo"])

    def test_remove_an_unknown_worker_fails(self):
        code, out, err = self.cli("workers", "remove", "worker9")
        self.assertEqual(code, 2, err)
        self.assertIn("unknown worker", err)

    def test_remove_a_worker_with_a_pending_job_fails(self):
        queue = self.project / ".aaagent" / "queue" / "worker2"
        queue.mkdir(parents=True)
        (queue / "job.json").write_text("{}\n")
        code, out, err = self.cli("workers", "remove", "worker2")
        self.assertEqual(code, 2, out + err)
        self.assertIn("queued jobs", err)
        self.assertEqual(Config.load(self.project).worker_names(), ["worker1", "worker2"])

    def test_remove_a_worker_with_a_running_job_fails(self):
        queue = self.project / ".aaagent" / "queue" / "worker2"
        queue.mkdir(parents=True)
        (queue / "job.running.json").write_text('{"id": "j", "worker": "worker2"}\n')
        self.assertEqual(self.cli("workers", "remove", "worker2")[0], 2)
        self.assertEqual(Config.load(self.project).worker_names(), ["worker1", "worker2"])

    def test_remove_a_worker_with_an_unfinished_task_job_fails(self):
        task = tasks.create(self.project, "busy")
        task.plan.write_text(PLAN)
        task.update(status="running",
                    current_job={"id": "j", "phase": "implement", "round": 1, "worker": "worker2"})
        code, out, err = self.cli("workers", "remove", "worker2")
        self.assertEqual(code, 2, out + err)
        self.assertIn(task.id, err)
        self.assertEqual(Config.load(self.project).worker_names(), ["worker1", "worker2"])

    def test_removing_the_default_worker_clears_it_and_falls_back_to_the_first(self):
        self.user_config.write_text('[workers.worker1]\nmodel = "a"\n'
                                    '[workers.worker2]\nmodel = "b"\n'
                                    '[workers.worker3]\nmodel = "c"\n'
                                    '[flow]\ndefault_worker = "worker2"\n'
                                    'fallback_worker = "worker3"\n')
        code, out, err = self.cli("workers", "remove", "worker2")
        self.assertEqual(code, 0, out + err)
        self.assertIn("flow.default_worker was worker2", out)
        user = tomllib.loads(self.user_config.read_text())
        self.assertEqual(user["flow"]["default_worker"], "")
        self.assertEqual(user["flow"]["fallback_worker"], "worker3")
        cfg = Config.load(self.project)
        self.assertEqual((cfg.default_worker, cfg.fallback_worker), ("worker1", "worker3"))

    def test_removing_the_reviewer_gives_the_job_back_to_the_chat(self):
        self.user_config.write_text('[workers.worker1]\nmodel = "a"\n'
                                    '[workers.worker2]\nmodel = "b"\n'
                                    '[flow]\nreviewer = "worker2"\n')
        code, out, err = self.cli("workers", "remove", "worker2")
        self.assertEqual(code, 0, out + err)
        self.assertIn("flow.reviewer was worker2", out)
        self.assertEqual(tomllib.loads(self.user_config.read_text())["flow"]["reviewer"],
                         "orchestrator")

    def test_remove_removes_the_worker_from_wherever_it_is_defined(self):
        self.user_config.write_text('[workers.worker2]\nmodel = "opencode-go/glm-5.3-flash"\n')
        project_config_path(self.project).write_text('[workers.worker1]\nmodel = "a"\n')
        code, out, err = self.cli("workers", "remove", "worker1")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"removed worker1  (from {project_config_path(self.project)})", out)
        self.assertNotIn("worker1", tomllib.loads(project_config_path(self.project).read_text())
                         .get("workers", {}))
        self.assertEqual(Config.load(self.project).worker_names(), ["worker2"])

    def test_model_in_the_project_config_keeps_the_seed_workers(self):
        code, out, err = self.cli("model", "worker1", "opencode-go/kimi-k3", "--project")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(Config.load(self.project).worker_names(), ["worker1", "worker2"])
        self.assertEqual(Config.load(self.project).worker_model("worker1"), "opencode-go/kimi-k3")

    def test_set_default_and_fallback(self):
        self.cli("workers", "add", "opencode-go/kimi-k3")      # worker3
        code, out, err = self.cli("workers", "default", "worker2")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(tomllib.loads(self.user_config.read_text())["flow"]["default_worker"],
                         "worker2")
        self.assertEqual(Config.load(self.project).default_worker, "worker2")
        self.assertEqual(Config.load(self.project).fallback_worker, "worker3",
                         "the fallback is the next worker after the default")

        code, out, err = self.cli("workers", "fallback", "worker3")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(tomllib.loads(self.user_config.read_text())["flow"]["fallback_worker"],
                         "worker3")
        self.assertEqual(Config.load(self.project).fallback_worker, "worker3")

        code, out, err = self.cli("workers", "fallback", "worker2")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(Config.load(self.project).fallback_worker, "worker3",
                         "naming the default worker as fallback is ignored")

    def test_default_and_fallback_validate_the_name(self):
        self.assertEqual(self.cli("workers", "default", "nope")[0], 2)
        self.assertEqual(self.cli("workers", "fallback", "nope")[0], 2)

    def test_default_and_fallback_in_the_project_config(self):
        code, out, err = self.cli("workers", "default", "worker2", "--project")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(tomllib.loads(project_config_path(self.project).read_text())
                         ["flow"]["default_worker"], "worker2")

    def test_unknown_worker_in_dispatch_is_a_flowerror(self):
        task = tasks.create(self.project, "x")
        task.plan.write_text(PLAN)
        code, out, err = self.cli("dispatch", task.id, "--worker", "nope")
        self.assertEqual(code, 2, out)
        self.assertIn("nope is not configured", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_worker_in_the_plan_is_a_flowerror(self):
        task = tasks.create(self.project, "x")
        task.plan.write_text(PLAN.replace("worker:", "worker: nope"))
        code, out, err = self.cli("dispatch", task.id)
        self.assertEqual(code, 2, out)
        self.assertIn("plan.md says worker: nope", err)

    def test_a_stale_worker_in_the_state_is_ignored(self):
        task = tasks.create(self.project, "x")
        task.plan.write_text(PLAN)
        task.update(worker="gone", status="awaiting_review")
        task.report(1).write_text("done\n")
        task.review(1).write_text("---\nverdict: approved\nround: 1\n---\n## ok\n")
        step = tasks.next_step(task, Config.load(self.project))
        self.assertEqual((step.phase, step.worker), ("commit", "worker1"))


class MigrationTest(unittest.TestCase):
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

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def test_renames_the_tables_and_the_flow_values_keeping_comments(self):
        self.user_config.write_text('# my config\n[workers.deepseek]  # first\n'
                                    'model = "opencode-go/deepseek-v4.1-flash"\n'
                                    'thinking = "high"\n\n[workers.glm]\n'
                                    'model = "opencode-go/glm-5.3-flash"\n\n'
                                    '[flow]\ndefault_worker = "deepseek"\n'
                                    'fallback_worker = "glm"\nreviewer = "glm"\n')
        notes = config.migrate_legacy_workers(self.project)
        text = self.user_config.read_text()
        data = tomllib.loads(text)
        self.assertEqual(data["workers"]["worker1"],
                         {"model": "opencode-go/deepseek-v4.1-flash", "thinking": "high"})
        self.assertEqual(data["workers"]["worker2"]["model"], "opencode-go/glm-5.3-flash")
        self.assertEqual(data["flow"],
                         {"default_worker": "worker1", "fallback_worker": "worker2",
                          "reviewer": "worker2"})
        self.assertIn("# my config", text)
        self.assertIn("[workers.worker1]  # first", text)
        self.assertTrue(any("renamed worker deepseek → worker1" in n for n in notes), notes)
        self.assertTrue(any("flow.default_worker deepseek → worker1" in n for n in notes), notes)

    def test_is_idempotent(self):
        self.user_config.write_text('[workers.deepseek]\nmodel = "a"\n[workers.glm]\nmodel = "b"\n')
        self.assertTrue(config.migrate_legacy_workers(self.project))
        self.assertEqual(config.migrate_legacy_workers(self.project), [])
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(sorted(data["workers"]), ["worker1", "worker2"])

    def test_renames_unfinished_task_state_and_sessions(self):
        from aaagent import tasks
        self.user_config.write_text('[workers.deepseek]\nmodel = "a"\n[workers.glm]\nmodel = "b"\n')
        task = tasks.create(self.project, "half done")
        task.save({"id": task.id, "status": "awaiting_review", "worker": "glm",
                   "sessions": {"deepseek": "s1", "deepseek:review": "s2", "glm:review": "s3"}})
        done = tasks.create(self.project, "finished")
        done.update(status="committed", worker="deepseek")
        config.migrate_legacy_workers(self.project)
        state = task.load()
        self.assertEqual(state["worker"], "worker2")
        self.assertEqual(state["sessions"], {"worker1": "s1", "worker1:review": "s2",
                                             "worker2:review": "s3"})
        self.assertEqual(done.load()["worker"], "deepseek", "finished tasks are left alone")

    def test_leaves_a_config_that_is_already_generic_alone(self):
        self.user_config.write_text('[workers.worker1]\nmodel = "a"\n')
        self.assertEqual(config.migrate_legacy_workers(self.project), [])
        self.assertEqual(sorted(tomllib.loads(self.user_config.read_text())["workers"]),
                         ["worker1"])

    def test_a_new_target_name_is_not_overwritten(self):
        self.user_config.write_text('[workers.deepseek]\nmodel = "a"\n'
                                    '[workers.worker1]\nmodel = "b"\n')
        config.migrate_legacy_workers(self.project)
        data = tomllib.loads(self.user_config.read_text())
        self.assertEqual(sorted(data["workers"]), ["deepseek", "worker1"])


if __name__ == "__main__":
    unittest.main()
