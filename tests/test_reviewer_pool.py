"""The reviewer daemon runs several review jobs at once: [reviewer] parallel."""

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
os.environ.setdefault("AAAGENT_POLL_SECONDS", "0.05")

from aaagent import session, tasks, worker  # noqa: E402
from aaagent.config import REVIEWER, Config, project_config_path  # noqa: E402

FAKE_OC = ROOT / "tests" / "fake_opencode.py"
GLM = "opencode-go/glm-5.3-flash"


class SlotsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        (self.project / ".aaagent").mkdir()
        self.env_backup = dict(os.environ)
        os.environ.update({
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "AAAGENT_PROJECT": str(self.project),
        })

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def config(self, text: str) -> Config:
        project_config_path(self.project).write_text(text)
        return Config.load(self.project)

    def test_slots(self):
        cfg = Config.load(self.project)
        self.assertEqual(worker.slots(cfg, REVIEWER), 1, "the default is one review at a time")
        self.assertEqual(worker.slots(cfg, "worker1"), 1)

        cfg = self.config('[reviewer]\nparallel = 3\n')
        self.assertEqual(worker.slots(cfg, REVIEWER), 3)
        self.assertEqual(worker.slots(cfg, "worker1"), 1, "workers stay one job at a time")

        cfg = self.config('[reviewer]\nparallel = 99\n')
        self.assertEqual(worker.slots(cfg, REVIEWER), worker.MAX_PARALLEL)

        for raw in ('"x"', "0"):
            cfg = self.config(f"[reviewer]\nparallel = {raw}\n")
            self.assertEqual(worker.slots(cfg, REVIEWER), 1, raw)


class ReviewerPoolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.env_backup = dict(os.environ)
        os.environ.update({
            "AAAGENT_OPENCODE_BIN": str(FAKE_OC),
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "AAAGENT_PROJECT": str(self.project),
            "FAKE_OC_FAIL": "", "FAKE_OC_NOREPORT": "",
            "FAKE_OC_REVIEW": "", "FAKE_OC_NOREVIEW": "", "FAKE_OC_REVIEW_SLEEP": "",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        self.git("init", "-q")
        (self.project / "README.md").write_text("hi\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "init")
        session.init_project(self.project, Config.load(self.project))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------------

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.project, check=True,
                              capture_output=True, text=True).stdout

    def config(self, text: str) -> None:
        project_config_path(self.project).write_text(text)

    def enqueue_review(self, task: tasks.Task) -> dict:
        return worker.enqueue(self.project, {
            "id": worker.new_job_id(),
            "task": task.id,
            "phase": "review",
            "round": 1,
            "worker": REVIEWER,
            "model": GLM,
            "backend": "opencode",
            "agent": "aaagent-reviewer",
            "session": None,
            "thinking": "",
            "prompt": f"Write your review to: {task.rel(task.review(1))}\nround: 1\n",
            "timeout": 60,
            "log": str(task.logs / "r1-review.jsonl"),
        })

    def run_serve(self) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = worker.serve(self.project, REVIEWER, once=True)
        self.assertEqual(code, 0, buf.getvalue())
        return buf.getvalue()

    def results(self, jobs: list[dict]) -> list[dict]:
        return [json.loads(worker.result_path(self.project, job["id"]).read_text())
                for job in jobs]

    # -- two reviews at once -------------------------------------------------

    def test_two_reviews_run_at_the_same_time(self):
        self.config(f'[reviewer]\nmodel = "{GLM}"\nparallel = 2\n')
        os.environ["FAKE_OC_REVIEW_SLEEP"] = "1.5"
        first = tasks.create(self.project, "first")
        second = tasks.create(self.project, "second")
        jobs = [self.enqueue_review(first), self.enqueue_review(second)]

        out = self.run_serve()

        for task in (first, second):
            self.assertTrue(task.review(1).is_file(), task.id)
        a, b = self.results(jobs)
        for result in (a, b):
            self.assertEqual(result["status"], "ok", result)
        self.assertLess(a["started"], b["finished"], out)
        self.assertLess(b["started"], a["finished"], out)

    def test_one_slot_runs_them_one_after_another(self):
        self.config(f'[reviewer]\nmodel = "{GLM}"\nparallel = 1\n')
        os.environ["FAKE_OC_REVIEW_SLEEP"] = "0.5"
        first = tasks.create(self.project, "first")
        second = tasks.create(self.project, "second")
        jobs = [self.enqueue_review(first), self.enqueue_review(second)]

        self.run_serve()

        for task in (first, second):
            self.assertTrue(task.review(1).is_file(), task.id)
        a, b = self.results(jobs)
        for result in (a, b):
            self.assertEqual(result["status"], "ok", result)
        self.assertFalse(a["started"] < b["finished"] and b["started"] < a["finished"],
                         (a, b))


if __name__ == "__main__":
    unittest.main()
