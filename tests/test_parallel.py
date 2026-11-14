"""Parallel tasks: declared files, several active tasks, scoped diffs, one commit at a time."""

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

from aaagent import dispatch, gitutil, session, status, tasks, tui, worker  # noqa: E402
from aaagent.config import Config  # noqa: E402
from aaagent.tasks import FlowError  # noqa: E402

FAKE = ROOT / "tests" / "fake_opencode.py"
DS, GLM = "opencode-go/deepseek-v4.1-flash", "opencode-go/glm-5.3-flash"


def plan(files=None, worker=""):
    lines = ["---", "task: x", f"worker: {worker}"]
    if files is not None:
        lines.append(f"files: {files}")
    lines += ["---", "# Goal", "Fake work."]
    return "\n".join(lines) + "\n"


def review(verdict, rnd):
    return f"---\nverdict: {verdict}\nround: {rnd}\n---\n## Required changes\n1. more\n"


class RepoCase(unittest.TestCase):
    """A temporary git project, a fake opencode and an empty user config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.args_log = Path(self.tmp.name) / "args.jsonl"
        self.env_backup = dict(os.environ)

        def restore():
            os.environ.clear()
            os.environ.update(self.env_backup)
        self.addCleanup(restore)
        os.environ.update({
            "AAAGENT_OPENCODE_BIN": str(FAKE),
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "FAKE_OC_ARGS_LOG": str(self.args_log),
            "FAKE_OC_FAIL": "", "FAKE_OC_NOREPORT": "",
            "FAKE_OC_REVIEW": "", "FAKE_OC_NOREVIEW": "",
            "AAAGENT_PROJECT": str(self.project),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        self.git("init", "-q")
        (self.project / "README.md").write_text("hi\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "init")
        session.init_project(self.project, Config.load(self.project))
        self.cfg = Config.load(self.project)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.project, check=True,
                              capture_output=True, text=True).stdout

    def new_task(self, title, files=None) -> tasks.Task:
        task = tasks.create(self.project, title)
        task.plan.write_text(plan(files))
        return task


class ScopeTest(RepoCase):
    def test_scope_parses_files_into_clean_entries(self):
        task = self.new_task("one", "lib/a.py, tests/, ./docs/x.md, lib/a.py")
        self.assertEqual(task.scope(), ["lib/a.py", "tests/", "docs/x.md"])
        self.assertEqual(task.pathspecs(), ["lib/a.py", "tests/", "docs/x.md"])

    def test_a_glob_entry_becomes_a_git_glob_pathspec(self):
        task = self.new_task("one", "lib/*.py, docs/?-*.md")
        self.assertEqual(task.pathspecs(), [":(glob)lib/*.py", ":(glob)docs/?-*.md"])

    def test_no_files_key_means_exclusive(self):
        self.assertIsNone(self.new_task("one").scope())
        self.assertIsNone(self.new_task("two", " # a comment only").scope())

    def test_bad_entries_are_refused(self):
        for entry, message in (("/etc/passwd", "is absolute"), ("../outside", "contains '\\.\\.'"),
                               ("src/../../x", "contains '\\.\\.'"), (".aaagent/state.json",
                                                                       "is inside .aaagent/")):
            task = self.new_task("bad", entry)
            with self.assertRaisesRegex(FlowError, message):
                task.checked_scope()
            with self.assertRaisesRegex(FlowError, message):
                tasks.next_step(task, self.cfg)

    def test_in_scope(self):
        scope = ["lib/a.py", "tests/", "docs/*.md"]
        self.assertTrue(tasks.in_scope("lib/a.py", scope))
        self.assertFalse(tasks.in_scope("lib/b.py", scope))
        self.assertTrue(tasks.in_scope("tests/test_x.py", scope))
        self.assertTrue(tasks.in_scope("tests", scope))
        self.assertFalse(tasks.in_scope("test_x.py", scope))
        self.assertTrue(tasks.in_scope("docs/guide.md", scope))
        self.assertFalse(tasks.in_scope("other/guide.md", scope))
        self.assertTrue(tasks.in_scope("anything", None), "an undeclared task may change anything")

    def test_glob_directories_cover_their_contents(self):
        self.assertTrue(tasks.in_scope("build-x/a.o", ["build-*/"]))
        self.assertFalse(tasks.in_scope("dist/a.o", ["build-*/"]))

    def test_scopes_overlap(self):
        self.assertEqual(tasks.scopes_overlap(["a.txt"], ["a.txt"]), ["a.txt overlaps a.txt"])
        self.assertTrue(tasks.scopes_overlap(["docs/a.md"], ["docs/"]))
        self.assertTrue(tasks.scopes_overlap(["docs/"], ["docs/a.md"]))
        self.assertTrue(tasks.scopes_overlap(["lib/*.py"], ["lib/tui.py"]))
        self.assertTrue(tasks.scopes_overlap(["lib/tui.py"], ["lib/*.py"]))
        self.assertTrue(tasks.scopes_overlap(["docs/*.md"], ["docs/"]))
        self.assertEqual(tasks.scopes_overlap(["docs/*.md"], ["tests/*.py"]), [],
                         "two globs with disjoint prefixes do not overlap")
        self.assertTrue(tasks.scopes_overlap(["lib/*.py"], ["lib/a/*.py"]),
                        "globs sharing a literal prefix may overlap")
        self.assertEqual(tasks.scopes_overlap(["lib/a/*.py"], ["lib/b/*.py"]), [],
                         "globs under different directories do not overlap")
        self.assertEqual(tasks.scopes_overlap(["a.txt"], ["b.txt"]), [])


class ActiveTasksTest(RepoCase):
    def setUp(self):
        super().setUp()
        self.one = self.new_task("one", "alpha.txt")
        self.two = self.new_task("two", "beta.txt")

    def test_two_scoped_disjoint_tasks_are_both_active(self):
        tasks.claim_active(self.project, self.one)
        tasks.claim_active(self.project, self.two)
        self.assertEqual(tasks.active_tasks(self.project), [self.one.id, self.two.id])
        self.assertEqual(tasks.active_task(self.project), self.one.id)
        on_disk = json.loads(tasks.active_tasks_file(self.project).read_text())
        self.assertEqual(on_disk, [self.one.id, self.two.id])

    def test_claiming_the_same_task_twice_changes_nothing(self):
        tasks.claim_active(self.project, self.one)
        tasks.claim_active(self.project, self.one)
        self.assertEqual(tasks.active_tasks(self.project), [self.one.id])

    def test_overlapping_tasks_are_refused(self):
        other = self.new_task("other", "alpha.txt, docs/")
        tasks.claim_active(self.project, self.one)
        with self.assertRaisesRegex(FlowError, "overlap this task's: alpha.txt overlaps alpha.txt"):
            tasks.claim_active(self.project, other)
        self.assertEqual(tasks.active_tasks(self.project), [self.one.id])

    def test_a_scope_and_a_directory_overlap(self):
        other = self.new_task("other", "lib/")
        inside = self.new_task("inside", "lib/aaagent/x.py")
        tasks.claim_active(self.project, other)
        with self.assertRaisesRegex(FlowError, "lib/aaagent/x.py overlaps lib/"):
            tasks.claim_active(self.project, inside)

    def test_an_undeclared_task_needs_the_whole_tree(self):
        exclusive = self.new_task("exclusive")
        tasks.claim_active(self.project, self.one)
        with self.assertRaisesRegex(FlowError, "does not declare files:"):
            tasks.claim_active(self.project, exclusive)
        tasks.release_active(self.project, self.one)
        tasks.claim_active(self.project, exclusive)
        with self.assertRaisesRegex(FlowError, "does not declare files:"):
            tasks.claim_active(self.project, self.two)

    def test_the_legacy_active_task_file_is_migrated(self):
        tasks.active_file(self.project).write_text(self.one.id + "\n")
        self.assertEqual(tasks.active_tasks(self.project), [self.one.id])
        self.assertFalse(tasks.active_file(self.project).exists())
        self.assertEqual(json.loads(tasks.active_tasks_file(self.project).read_text()), [self.one.id])

    def test_final_and_missing_tasks_are_dropped(self):
        tasks.claim_active(self.project, self.one)
        tasks.claim_active(self.project, self.two)
        self.one.update(status="committed")
        self.assertEqual(tasks.active_tasks(self.project), [self.two.id])
        self.assertEqual(json.loads(tasks.active_tasks_file(self.project).read_text()), [self.two.id])

        tasks.active_tasks_file(self.project).write_text(json.dumps(["999-gone", self.two.id]))
        self.assertEqual(tasks.active_tasks(self.project), [self.two.id])

    def test_release_removes_only_its_own_id(self):
        tasks.claim_active(self.project, self.one)
        tasks.claim_active(self.project, self.two)
        tasks.release_active(self.project, self.one)
        self.assertEqual(tasks.active_tasks(self.project), [self.two.id])
        tasks.release_active(self.project, self.one)  # already gone: harmless
        self.assertEqual(tasks.active_tasks(self.project), [self.two.id])


class FreshBaseTest(RepoCase):
    def test_changes_that_were_committed_meanwhile_are_not_preexisting(self):
        one = self.new_task("one")
        (self.project / "work.txt").write_text("half done\n")
        two = self.new_task("two")
        self.assertEqual(two.load()["preexisting_dirty"], ["work.txt"])

        self.git("add", "work.txt")
        self.git("commit", "-q", "-m", "one's work")
        head = gitutil.head(self.project)
        dispatch.refresh_base(self.project, two)
        state = two.load()
        self.assertEqual(state["preexisting_dirty"], [])
        self.assertEqual(state["base_commit"], head)
        self.assertTrue(state.get("started"))

    def test_files_in_another_active_scope_are_not_preexisting(self):
        one = self.new_task("one", "alpha.txt")
        two = self.new_task("two", "beta.txt")
        tasks.claim_active(self.project, one)
        (self.project / "alpha.txt").write_text("one's work\n")
        (self.project / "notes.txt").write_text("the user's\n")
        tasks.claim_active(self.project, two)
        dispatch.refresh_base(self.project, two)
        self.assertEqual(two.load()["preexisting_dirty"], ["notes.txt"])

    def test_the_base_is_refreshed_only_once(self):
        one = self.new_task("one", "alpha.txt")
        tasks.claim_active(self.project, one)
        dispatch.refresh_base(self.project, one)
        started = one.load()["started"]
        (self.project / "later.txt").write_text("x\n")
        dispatch.refresh_base(self.project, one)
        self.assertEqual(one.load()["started"], started)
        self.assertNotIn("later.txt", one.load()["preexisting_dirty"])

    def test_changed_outside_scope_names_undeclared_files(self):
        one = self.new_task("one", "alpha.txt")
        tasks.claim_active(self.project, one)
        (self.project / "alpha.txt").write_text("fine\n")
        (self.project / "stray.txt").write_text("scope creep\n")
        state = one.load()
        self.assertEqual(dispatch.changed_outside_scope(self.project, one, state), ["stray.txt"])
        two = self.new_task("two")
        tasks.release_active(self.project, one)
        tasks.claim_active(self.project, two)
        self.assertEqual(dispatch.changed_outside_scope(self.project, two, two.load()), [],
                         "an exclusive task has no declared-file warning")


class WorkerPickTest(RepoCase):
    def pending(self, task, name):
        worker.enqueue(self.project, {"id": f"pending-{task.id}-{name}", "task": task.id,
                                      "phase": "implement", "round": 1, "worker": name})

    def test_picks_an_idle_worker_when_the_default_is_busy(self):
        one = self.new_task("one", "alpha.txt")
        two = self.new_task("two", "beta.txt")
        self.pending(one, "worker1")
        self.assertEqual(dispatch.pick_free_worker(self.project, self.cfg, "worker1", two.id),
                         ("worker2", f"worker1 is busy with {one.id}"))

    def test_keeps_the_default_when_every_worker_is_busy(self):
        one = self.new_task("one", "alpha.txt")
        two = self.new_task("two", "beta.txt")
        self.pending(one, "worker1")
        self.pending(one, "worker2")
        self.assertEqual(dispatch.pick_free_worker(self.project, self.cfg, "worker1", two.id),
                         ("worker1", "all workers are busy; worker1 takes this when it is free"))

    def test_a_worker_with_its_own_task_is_free(self):
        one = self.new_task("one", "alpha.txt")
        self.pending(one, "worker1")
        self.assertEqual(dispatch.pick_free_worker(self.project, self.cfg, "worker1", one.id),
                         ("worker1", ""))


class ScopedGitTest(RepoCase):
    def setUp(self):
        super().setUp()
        (self.project / "a.txt").write_text("a\n")
        (self.project / "b.txt").write_text("b\n")
        (self.project / "docs").mkdir()
        (self.project / "docs" / "guide.md").write_text("guide\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "files")
        self.base = gitutil.head(self.project)
        (self.project / "a.txt").write_text("a changed\n")
        (self.project / "b.txt").write_text("b changed\n")
        (self.project / "docs" / "guide.md").write_text("guide changed\n")
        (self.project / "new.txt").write_text("new\n")
        (self.project / "docs" / "new.md").write_text("new doc\n")

    def test_a_file_scope_limits_tracked_and_untracked_changes(self):
        task = self.new_task("one", "a.txt")
        paths = task.pathspecs()
        diff = gitutil.full_diff(self.project, self.base, paths)
        self.assertIn("a changed", diff)
        self.assertNotIn("b changed", diff)
        self.assertNotIn("new.txt", diff)
        stat = gitutil.diff_stat(self.project, self.base, paths)
        self.assertIn("a.txt", stat)
        self.assertNotIn("b.txt", stat)
        self.assertEqual(gitutil.untracked(self.project, paths), [])
        self.assertIn("new.txt", gitutil.untracked(self.project))
        self.assertEqual(gitutil.dirty_paths(self.project, paths), ["a.txt"])

    def test_a_directory_scope_covers_everything_below(self):
        task = self.new_task("one", "docs/")
        paths = task.pathspecs()
        self.assertEqual(gitutil.untracked(self.project, paths), ["docs/new.md"])
        diff = gitutil.full_diff(self.project, self.base, paths)
        self.assertIn("guide changed", diff)
        self.assertNotIn("a changed", diff)

    def test_a_glob_scope_matches_files_anywhere(self):
        task = self.new_task("one", "*.txt")
        paths = task.pathspecs()
        self.assertEqual(sorted(gitutil.untracked(self.project, paths)), ["new.txt"])
        diff = gitutil.full_diff(self.project, self.base, paths)
        self.assertIn("a changed", diff)
        self.assertIn("b changed", diff)
        self.assertIn("new.txt", diff)
        self.assertNotIn("new.md", diff)

    def test_dirty_paths_accepts_no_scope(self):
        task = self.new_task("one", "a.txt")
        self.assertEqual(gitutil.dirty_paths(self.project, task.pathspecs()), ["a.txt"])
        self.assertEqual(sorted(gitutil.dirty_paths(self.project)),
                         ["a.txt", "b.txt", "docs/guide.md", "docs/new.md", "new.txt"])


class StatusTest(RepoCase):
    def test_status_has_active_tasks_and_each_tasks_scope(self):
        one = self.new_task("one", "alpha.txt")
        two = self.new_task("two", "beta.txt")
        tasks.claim_active(self.project, one)
        tasks.claim_active(self.project, two)
        data = status.status_data(self.project, self.cfg)
        self.assertEqual(data["active_tasks"], [one.id, two.id])
        self.assertEqual(data["active_task"], one.id)
        scopes = {t["id"]: t["scope"] for t in data["tasks"]}
        self.assertEqual(scopes, {one.id: ["alpha.txt"], two.id: ["beta.txt"]})
        self.assertEqual([f["kind"] for f in data["tasks"][0]["files"]], ["plan"],
                         "the task's plan/report files are still `files`")

    def test_the_dashboard_expands_every_active_task(self):
        one = self.new_task("one", "alpha.txt")
        two = self.new_task("two", "beta.txt")
        tasks.claim_active(self.project, one)
        tasks.claim_active(self.project, two)
        data = status.status_data(self.project, self.cfg)
        dash = tui.Dashboard(object(), self.project)
        with mock.patch.object(tui.status, "status_data", return_value=data):
            dash.load()
        self.assertEqual(dash.ui.expanded, {one.id, two.id})

    def test_status_line_lists_several_active_tasks(self):
        from aaagent import cli
        one = self.new_task("one", "alpha.txt")
        two = self.new_task("two", "beta.txt")
        tasks.claim_active(self.project, one)
        tasks.claim_active(self.project, two)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["status"])
        self.assertIn(f"active: {one.id}, {two.id}", buf.getvalue())


class PromptTest(RepoCase):
    def test_scoped_implement_prompt_lists_the_files(self):
        task = self.new_task("one", "alpha.txt, docs/")
        prompt = dispatch.build_prompt(task, tasks.Step("implement", 1, "worker1"), self.cfg)
        self.assertIn("This task may only change these files: alpha.txt, docs/", prompt)
        self.assertIn("never touch, revert or format files outside that list", prompt)

    def test_exclusive_task_prompts_have_no_scope_note(self):
        task = self.new_task("one")
        base = task.load()["base_commit"]
        prompt = dispatch.build_prompt(task, tasks.Step("implement", 1, "worker1"), self.cfg)
        self.assertNotIn("may only change these files", prompt)
        prompt = dispatch.build_prompt(task, tasks.Step("review", 1, "reviewer"), self.cfg)
        self.assertIn(f"git diff {base} -- .", prompt)
        self.assertIn("git status --short -- .", prompt)
        self.assertNotIn("Changes outside the declared files are scope creep", prompt)

    def test_scoped_review_prompt_is_limited_to_the_files(self):
        task = self.new_task("one", "alpha.txt, docs/")
        base = task.load()["base_commit"]
        prompt = dispatch.build_prompt(task, tasks.Step("review", 1, "reviewer"), self.cfg)
        self.assertIn(f"git diff {base} -- alpha.txt docs/", prompt)
        self.assertIn("git status --short -- alpha.txt docs/", prompt)
        self.assertIn("Changes outside the declared files are scope creep", prompt)

    def test_scoped_commit_prompt_stages_only_the_declared_files(self):
        task = self.new_task("one", "alpha.txt")
        prompt = dispatch.build_prompt(task, tasks.Step("commit", 1, "worker1"), self.cfg)
        self.assertIn("Stage only changed files inside this task's declared files: alpha.txt.", prompt)


class CommitSerialisationTest(RepoCase):
    def setUp(self):
        super().setUp()
        self.one = self.new_task("one", "alpha.txt")
        self.two = self.new_task("two", "beta.txt")
        tasks.claim_active(self.project, self.one)
        tasks.claim_active(self.project, self.two)

    def test_a_commit_waits_for_another_tasks_commit(self):
        self.one.update(status="running", current_job={"id": "j1", "phase": "commit", "round": 1})
        step = tasks.Step("commit", 1, "worker1")
        buf = io.StringIO()
        with mock.patch.object(dispatch, "_enqueue") as enqueue, redirect_stdout(buf):
            result = dispatch.enqueue_commit(self.project, self.two, step, self.cfg,
                                             sleep=lambda seconds: None, timeout=0)
        self.assertIsNone(result)
        enqueue.assert_not_called()
        self.assertIn(f"waiting for {self.one.id}'s commit to finish", buf.getvalue())

    def test_a_commit_starts_when_nobody_else_is_committing(self):
        step = tasks.Step("commit", 1, "worker2")
        with mock.patch.object(dispatch, "_enqueue", return_value={"id": "job-2"}) as enqueue:
            result = dispatch.enqueue_commit(self.project, self.two, step, self.cfg,
                                             sleep=lambda seconds: None, timeout=0)
        self.assertEqual(result, {"id": "job-2"})
        enqueue.assert_called_once()

    def test_another_tasks_implement_does_not_block_the_commit(self):
        self.one.update(status="running", current_job={"id": "j1", "phase": "implement", "round": 1})
        with mock.patch.object(dispatch, "_enqueue", return_value={"id": "job-2"}):
            result = dispatch.enqueue_commit(self.project, self.two, tasks.Step("commit", 1, "worker2"),
                                             self.cfg, sleep=lambda seconds: None, timeout=0)
        self.assertEqual(result, {"id": "job-2"})


class ParallelFlowTest(RepoCase):
    def stop_workers(self):
        for name in ("worker1", "worker2"):
            worker.stop(self.project, name)
        for _ in range(50):
            if not any(worker.worker_pid(self.project, n) for n in ("worker1", "worker2")):
                break
            time.sleep(0.1)
        self.addCleanup(self.stop_workers_once)

    def stop_workers_once(self):
        for name in ("worker1", "worker2"):
            worker.stop(self.project, name)

    def run_dispatch(self, task) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = dispatch.dispatch(self.project, task.id)
        return code, buf.getvalue()

    def test_two_scoped_tasks_run_side_by_side_and_commit_their_own_files(self):
        one = self.new_task("one", "alpha.txt")
        two = self.new_task("two", "beta.txt")

        code, out = self.run_dispatch(one)
        self.assertEqual(code, 0, out)
        self.assertIn("worker1", out)
        self.assertIn(DS, (self.project / "alpha.txt").read_text())
        self.assertFalse((self.project / "beta.txt").exists(), "two has not run yet")
        self.assertEqual(one.load()["status"], "awaiting_review")
        one.review(1).write_text(review("approved", 1))

        # Make worker1 look busy with one's next job, so two goes to worker2.
        self.stop_workers()
        pending = worker.enqueue(self.project, {
            "id": "pending-one", "task": one.id, "phase": "commit", "round": 1,
            "worker": "worker1", "session": None, "prompt": "", "log": str(one.logs / "pending")})
        self.addCleanup((worker.queue_dir(self.project, "worker1") / f"{pending['id']}.json").unlink,
                        missing_ok=True)

        code, out = self.run_dispatch(two)
        self.assertEqual(code, 0, out)
        self.assertIn("worker2", out)
        self.assertIn("worker1 is busy with", out)
        self.assertIn(GLM, two.report(1).read_text())
        self.assertEqual(two.load()["worker"], "worker2")
        self.assertNotIn("alpha.txt", out.split("changes since task start:")[1])
        self.assertIn("beta.txt", out.split("changes since task start:")[1])
        self.assertEqual(one.load()["status"], "awaiting_review", "one is still waiting for its commit")
        self.assertEqual(tasks.active_tasks(self.project), [one.id, two.id])

        (worker.queue_dir(self.project, "worker1") / f"{pending['id']}.json").unlink()
        two.review(1).write_text(review("approved", 1))

        code, out = self.run_dispatch(one)
        self.assertEqual(code, 0, out)
        self.assertIn("committed", one.load()["status"])
        one_files = self.git("show", "--name-only", "--pretty=format:", one.load()["commit"]).split()
        self.assertEqual(one_files, ["alpha.txt"])

        code, out = self.run_dispatch(two)
        self.assertEqual(code, 0, out)
        self.assertIn("committed", two.load()["status"])
        two_files = self.git("show", "--name-only", "--pretty=format:", two.load()["commit"]).split()
        self.assertEqual(two_files, ["beta.txt"])

        self.assertEqual(tasks.active_tasks(self.project), [])
        self.assertNotIn(".aaagent", self.git("show", "--name-only", "HEAD"))

    def test_a_task_planned_before_another_commit_gets_a_fresh_base(self):
        one = self.new_task("one")          # no files: -> writes work.txt
        code, out = self.run_dispatch(one)
        self.assertEqual(code, 0, out)
        two = self.new_task("two")          # created while one's work.txt is uncommitted
        self.assertEqual(two.load()["preexisting_dirty"], ["work.txt"])

        one.review(1).write_text(review("approved", 1))
        code, out = self.run_dispatch(one)  # commit one
        self.assertEqual(code, 0, out)
        self.assertEqual(gitutil.dirty_paths(self.project), [])

        code, out = self.run_dispatch(two)
        self.assertEqual(code, 0, out)
        self.assertEqual(two.load()["preexisting_dirty"], [])
        self.assertTrue(two.load().get("started"))

    def test_an_overlapping_third_task_is_refused_while_two_run(self):
        one = self.new_task("one", "alpha.txt")
        two = self.new_task("two", "beta.txt")
        third = self.new_task("third", "alpha.txt, gamma.txt")
        self.run_dispatch(one)
        self.run_dispatch(two)
        with self.assertRaisesRegex(FlowError, "alpha.txt overlaps alpha.txt"):
            self.run_dispatch(third)
        self.assertEqual(tasks.active_tasks(self.project), [one.id, two.id])


if __name__ == "__main__":
    unittest.main()
