"""A worker daemon restarts itself when aaagent's own code changes (between jobs)."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import worker  # noqa: E402


def completed(returncode: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode)


class FingerprintTest(unittest.TestCase):
    def test_fingerprint_is_the_newest_py_mtime(self):
        newest = max(p.stat().st_mtime for p in Path(__file__).resolve().parents[1]
                     .joinpath("lib", "aaagent").glob("*.py"))
        self.assertEqual(worker.code_fingerprint(), newest)

    def test_the_import_check_runs_the_new_code(self):
        with mock.patch.object(worker.subprocess, "run", return_value=completed(0)) as run:
            self.assertTrue(worker._code_imports())
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], [sys.executable, "-c"])
        self.assertIn("import aaagent.cli, aaagent.worker", argv[2])
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["capture_output"])
        self.assertIsNotNone(kwargs["timeout"])
        self.assertIn(str(ROOT / "lib"), kwargs["env"]["PYTHONPATH"])


class RestartIfCodeChangedTest(unittest.TestCase):
    def test_restart_execs_when_code_is_newer(self):
        with mock.patch.object(worker, "code_fingerprint", return_value=200.0), \
                mock.patch.object(worker.subprocess, "run", return_value=completed(0)), \
                mock.patch.object(worker.os, "execv",
                                  side_effect=SystemExit("execv")) as execv, \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                worker._restart_if_code_changed(100.0, "[w]", [0.0])
        execv.assert_called_once()
        args, kwargs = execv.call_args
        self.assertEqual(args[0], sys.executable)
        self.assertEqual(args[1][0], sys.executable)

    def test_no_restart_when_unchanged_or_too_soon(self):
        with mock.patch.object(worker, "code_fingerprint", return_value=100.0), \
                mock.patch.object(worker.os, "execv") as execv:
            worker._restart_if_code_changed(100.0, "[w]", [0.0])
        execv.assert_not_called()

        with mock.patch.object(worker, "code_fingerprint", return_value=200.0), \
                mock.patch.object(worker.os, "execv") as execv:
            worker._restart_if_code_changed(100.0, "[w]", [worker.time.monotonic()])
        execv.assert_not_called()

    def test_execv_failure_keeps_running(self):
        out = io.StringIO()
        with mock.patch.object(worker, "code_fingerprint", return_value=200.0), \
                mock.patch.object(worker.subprocess, "run", return_value=completed(0)), \
                mock.patch.object(worker.os, "execv", side_effect=OSError("nope")), \
                redirect_stdout(out):
            worker._restart_if_code_changed(100.0, "[w]", [0.0])
        self.assertIn("restart", out.getvalue().lower())

    def test_half_written_code_is_not_executed_and_warned_about_once(self):
        """Another task may still be editing: a broken version must not replace the daemon."""
        out = io.StringIO()
        last_check = [0.0]
        with mock.patch.object(worker, "code_fingerprint", return_value=200.0), \
                mock.patch.object(worker, "RELOAD_CHECK_SECONDS", 0), \
                mock.patch.object(worker.subprocess, "run",
                                  return_value=completed(1)) as run, \
                mock.patch.object(worker.os, "execv") as execv, \
                redirect_stdout(out):
            worker._restart_if_code_changed(100.0, "[w]", last_check)
            worker._restart_if_code_changed(100.0, "[w]", last_check)
        execv.assert_not_called()
        run.assert_called_once()  # the same fingerprint is not checked over and over
        self.assertEqual(out.getvalue().count("does not import cleanly"), 1, out.getvalue())

    def test_a_broken_version_that_then_imports_cleanly_is_used(self):
        out = io.StringIO()
        last_check = [0.0]
        with mock.patch.object(worker, "RELOAD_CHECK_SECONDS", 0), \
                mock.patch.object(worker, "code_fingerprint", return_value=200.0), \
                mock.patch.object(worker.subprocess, "run", return_value=completed(1)), \
                mock.patch.object(worker.os, "execv") as execv, \
                redirect_stdout(out):
            worker._restart_if_code_changed(100.0, "[w]", last_check)
        execv.assert_not_called()

        with mock.patch.object(worker, "RELOAD_CHECK_SECONDS", 0), \
                mock.patch.object(worker, "code_fingerprint", return_value=300.0), \
                mock.patch.object(worker.subprocess, "run", return_value=completed(0)), \
                mock.patch.object(worker.os, "execv",
                                  side_effect=SystemExit("execv")) as execv, \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                worker._restart_if_code_changed(100.0, "[w]", last_check)
        execv.assert_called_once()

    def test_idle_single_slot_daemon_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "proj"
            project.mkdir()
            env_backup = dict(os.environ)
            os.environ.update({
                "AAAGENT_USER_CONFIG": str(Path(tmp) / "none.toml"),
                "AAAGENT_PROJECT": str(project),
            })
            calls = {"n": 0}

            def fingerprint():
                calls["n"] += 1
                return 1.0 if calls["n"] == 1 else 2.0

            try:
                with mock.patch.object(worker, "code_fingerprint", side_effect=fingerprint), \
                        mock.patch.object(worker, "RELOAD_CHECK_SECONDS", 0), \
                        mock.patch.object(worker, "POLL_SECONDS", 0.01), \
                        mock.patch.object(worker.subprocess, "run", return_value=completed(0)), \
                        mock.patch.object(worker.os, "execv",
                                          side_effect=SystemExit("execv")), \
                        redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        worker.serve(project, "worker1", quiet_wait=True)
            finally:
                os.environ.clear()
                os.environ.update(env_backup)
                worker.pid_file(project, "worker1").unlink(missing_ok=True)
            self.assertFalse(worker.pid_file(project, "worker1").exists())

    def test_idle_pool_daemon_checks_for_a_code_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "proj"
            project.mkdir()
            (project / ".aaagent").mkdir()
            (project / ".aaagent" / "config.toml").write_text(
                '[reviewer]\nmodel = "opencode-go/glm-5.3-flash"\nparallel = 2\n')
            env_backup = dict(os.environ)
            os.environ.update({
                "AAAGENT_USER_CONFIG": str(Path(tmp) / "none.toml"),
                "AAAGENT_PROJECT": str(project),
            })
            try:
                self.assertEqual(worker.slots(worker.Config.load(project), "reviewer"), 2)
                with mock.patch.object(worker, "_restart_if_code_changed",
                                       side_effect=SystemExit("execv")) as check, \
                        redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        worker.serve(project, "reviewer", quiet_wait=True)
            finally:
                os.environ.clear()
                os.environ.update(env_backup)
                worker.pid_file(project, "reviewer").unlink(missing_ok=True)
            check.assert_called_once()
            self.assertIn("[reviewer]", check.call_args.args[1])
            self.assertIsInstance(check.call_args.args[2], list)


if __name__ == "__main__":
    unittest.main()
