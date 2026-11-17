"""`.aaagent/lessons.md` is appended to implement, fix and review prompts."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import dispatch, tasks  # noqa: E402
from aaagent.config import Config  # noqa: E402
from aaagent.tasks import Step  # noqa: E402

PLAN = """---
task: x
worker:
---
# Goal
Append the model name to work.txt.
"""

LESSONS = "- run the full suite\n- {braces} stay\n"


class LessonsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.env_backup = dict(os.environ)
        os.environ.update({
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
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
        self.cfg = Config.load(self.project)
        self.task = tasks.create(self.project, "lessons")
        self.task.plan.write_text(PLAN)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.project, check=True,
                              capture_output=True, text=True).stdout

    def lessons_file(self) -> Path:
        return tasks.agent_dir(self.project) / "lessons.md"

    def prompt(self, phase: str, rnd: int = 1) -> str:
        return dispatch.build_prompt(self.task, Step(phase, rnd, "worker1"), self.cfg)

    # -------------------------------------------------------------------------

    def test_no_lessons_file_adds_nothing(self):
        self.assertFalse(self.lessons_file().exists())
        self.assertNotIn("PROJECT LESSONS", self.prompt("implement"))

    def test_lessons_in_implement_fix_and_review_but_not_commit(self):
        self.lessons_file().write_text(LESSONS)
        for phase in ("implement", "fix", "review"):
            text = self.prompt(phase)
            self.assertIn("PROJECT LESSONS", text, phase)
            self.assertIn("- run the full suite", text, phase)
            self.assertIn("- {braces} stay", text, phase)
        self.assertNotIn("PROJECT LESSONS", self.prompt("commit"))

    def test_blank_lessons_file_adds_nothing(self):
        self.lessons_file().write_text("  \n\n\t\n")
        for phase in ("implement", "fix", "review"):
            self.assertNotIn("PROJECT LESSONS", self.prompt(phase), phase)

    def test_long_lessons_file_is_truncated(self):
        self.lessons_file().write_text("- lesson\n" * 2000 + "TAIL-MARKER\n")
        text = self.prompt("implement")
        self.assertIn("PROJECT LESSONS", text)
        self.assertIn("truncated", text)
        self.assertNotIn("TAIL-MARKER", text)

    def test_lessons_note_is_empty_without_a_file(self):
        self.assertEqual(dispatch.lessons_note(self.project), "")


if __name__ == "__main__":
    unittest.main()
