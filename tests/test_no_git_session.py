"""The chat side of No Git mode: the start-up choice, the chat env, and the prompt.

`session` builds the planner chat: it picks the project (`ensure_repo`), gives the chat an
environment and a prompt, and exports the same variables into the tmux script. In No Git
mode (`[git] branch_mode = "none"`) all of that follows the shadow repository in
`.aaagent/shadow.git` instead of a real one.
"""

import builtins
import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import gitutil, session  # noqa: E402
from aaagent.config import Config, project_config_path, set_toml_value  # noqa: E402


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class Case(unittest.TestCase):
    """A temp folder, and the caller's environment restored afterwards."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.env_backup = dict(os.environ)

        def restore():
            os.environ.clear()
            os.environ.update(self.env_backup)
        self.addCleanup(restore)
        os.environ["AAAGENT_USER_CONFIG"] = str(Path(self.tmp.name) / "none.toml")
        os.environ.update({
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
        return subprocess.run(["git", *args], cwd=self.project, check=True,
                              capture_output=True, text=True).stdout

    def answer(self, text: str):
        """Run `ensure_repo` on a TTY, answering the question with `text`."""
        with mock.patch.object(sys, "stdin", FakeTTY()), \
                mock.patch.object(builtins, "input", return_value=text), \
                redirect_stdout(io.StringIO()):
            return session.ensure_repo(self.project)


class EnsureRepoTest(Case):
    def test_the_no_git_choice_configures_the_folder(self):
        self.assertEqual(self.answer("n"), self.project)

        self.assertEqual(Config.load(self.project).branch_mode, "none")
        self.assertIn('branch_mode = "none"', project_config_path(self.project).read_text())
        self.assertTrue((gitutil.shadow_dir(self.project) / "HEAD").is_file())
        self.assertFalse((self.project / ".git").exists(), "nothing real is created")
        self.assertNotIn(".git", [p.name for p in self.project.iterdir()])

    def test_the_git_init_choice_creates_a_repository(self):
        self.assertEqual(self.answer("g"), self.project)

        self.assertTrue((self.project / ".git").is_dir())
        self.assertFalse((gitutil.shadow_dir(self.project) / "HEAD").exists(),
                         "no shadow repository outside No Git mode")
        self.assertEqual(Config.load(self.project).branch_mode, "current")

    def test_anything_else_cancels(self):
        self.assertIsNone(self.answer(""))
        self.assertFalse((self.project / ".git").exists())
        self.assertFalse((self.project / ".aaagent").exists())

    def test_without_a_terminal_it_asks_nothing(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO()), \
                mock.patch.object(builtins, "input") as ask, redirect_stdout(out):
            self.assertIsNone(session.ensure_repo(self.project))
        ask.assert_not_called()
        self.assertIn("Start aaagent in a terminal to choose, or run `git init` first.",
                      out.getvalue())
        self.assertFalse((self.project / ".aaagent").exists())

    def test_a_subfolder_of_a_no_git_project_needs_no_question(self):
        (self.project / ".aaagent").mkdir()
        project_config_path(self.project).write_text('[git]\nbranch_mode = "none"\n')
        sub = self.project / "sub" / "dir"
        sub.mkdir(parents=True)

        with mock.patch.object(sys, "stdin", FakeTTY()), \
                mock.patch.object(builtins, "input") as ask, redirect_stdout(io.StringIO()):
            self.assertEqual(session.ensure_repo(sub), self.project.resolve())
        ask.assert_not_called()


class InitProjectTest(Case):
    def test_no_git_mode_creates_the_shadow(self):
        self.write_mode("none")
        session.init_project(self.project, Config.load(self.project))
        self.assertTrue((gitutil.shadow_dir(self.project) / "HEAD").is_file())

    def test_no_git_mode_excludes_the_agent_dir_from_a_real_repository(self):
        self.git("init", "-q")
        self.write_mode("none")
        session.init_project(self.project, Config.load(self.project))

        exclude = (self.project / ".git" / "info" / "exclude").read_text()
        self.assertIn("/.aaagent/", exclude.split(),
                      "the user's own `git status` must not list .aaagent/")
        self.assertTrue((gitutil.shadow_dir(self.project) / "HEAD").is_file())

    def test_git_mode_still_excludes_the_agent_dir(self):
        self.git("init", "-q")
        session.init_project(self.project, Config.load(self.project))
        exclude = (self.project / ".git" / "info" / "exclude").read_text()
        self.assertIn("/.aaagent/", exclude.split())


class SessionEnvTest(Case):
    def test_a_no_git_env_points_at_the_shadow(self):
        self.write_mode("none")
        env = session.session_env(self.project)

        self.assertEqual(env["GIT_DIR"], str(gitutil.shadow_dir(self.project)))
        self.assertEqual(env["GIT_WORK_TREE"], str(self.project))
        self.assertEqual(env["AAAGENT_PROJECT"], str(self.project))
        self.assertTrue((gitutil.shadow_dir(self.project) / "HEAD").is_file())

    def test_a_git_project_has_no_git_dir_even_when_the_caller_has_one(self):
        self.git("init", "-q")
        os.environ["GIT_DIR"] = str(Path(self.tmp.name) / "elsewhere.git")
        os.environ["GIT_WORK_TREE"] = str(self.project)
        env = session.session_env(self.project)

        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("GIT_WORK_TREE", env)
        self.assertEqual(env["AAAGENT_PROJECT"], str(self.project))

    def test_exports_carry_the_shadow_in_no_git_mode(self):
        self.write_mode("none")
        env = session.session_env(self.project)
        script = session._exports(self.project, env)

        self.assertIn(f"GIT_DIR={shlex.quote(env['GIT_DIR'])}", script)
        self.assertIn(f"GIT_WORK_TREE={shlex.quote(env['GIT_WORK_TREE'])}", script)
        self.assertNotIn("unset GIT_DIR", script)

    def test_exports_unset_a_stale_git_dir_in_git_mode(self):
        self.git("init", "-q")
        script = session._exports(self.project, session.session_env(self.project))

        self.assertTrue(script.startswith("unset GIT_DIR GIT_WORK_TREE; "))
        self.assertNotIn("GIT_DIR=", script)
        self.assertIn(f"AAAGENT_PROJECT={shlex.quote(str(self.project))}", script)

    def test_exports_keep_their_extra_variables(self):
        self.write_mode("none")
        script = session._exports(self.project, session.session_env(self.project),
                                  AAAGENT_CHAT_PANE="%9")
        self.assertIn("AAAGENT_CHAT_PANE=%9", script)


class PromptAndSettingsTest(Case):
    def test_the_prompt_explains_no_git_only_in_that_mode(self):
        self.write_mode("none")
        prompt = session.orchestrator_prompt(Config.load(self.project), self.project)
        self.assertIn("This project runs without git", prompt)
        self.assertNotIn("## Open branch from an earlier chat", prompt)

        self.git("init", "-q")
        self.write_mode("current")
        prompt = session.orchestrator_prompt(Config.load(self.project), self.project)
        self.assertNotIn("This project runs without git", prompt)

    def test_the_prompt_without_a_project_has_no_no_git_section(self):
        self.write_mode("none")
        self.assertNotIn("This project runs without git",
                         session.orchestrator_prompt(Config.load(None)))

    def test_git_init_is_denied_only_in_no_git_mode(self):
        self.write_mode("none")
        deny = session.claude_settings(self.project)["permissions"]["deny"]
        self.assertIn("Bash(git init*)", deny)
        self.assertIn("Bash(git commit*)", deny)

        self.git("init", "-q")
        self.write_mode("current")
        deny = session.claude_settings(self.project)["permissions"]["deny"]
        self.assertNotIn("Bash(git init*)", deny)
        self.assertIn("Bash(git commit*)", deny)


if __name__ == "__main__":
    unittest.main()
