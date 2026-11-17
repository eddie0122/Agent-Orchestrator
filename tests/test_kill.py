"""`aaagent kill`: find and kill a project's aaagent processes.

Every test uses a fresh temporary folder as the project and gives its children their env
through `Popen(..., env=...)` (/proc/<pid>/environ is the env at exec time). Never point
these tests at a real project: the test process itself runs inside one.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import killer  # noqa: E402

AAAGENT = ROOT / "bin" / "aaagent"
SLEEP = "import time; time.sleep(60)"
IGNORE_SIGTERM = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
MARKER = "KILL_TEST_MARKER"


def wait_until(predicate, timeout=5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def environ_of(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return b""


def cmdline_of(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return b""


@unittest.skipUnless(Path("/proc/self/environ").exists(), "process discovery needs /proc")
class KillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.procs: list[subprocess.Popen] = []
        self.extra_pids: list[int] = []
        self.marker = f"{MARKER}-{os.getpid()}-{time.time_ns()}"

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for pid in self.extra_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    # -- helpers -------------------------------------------------------

    def env(self, project=None, drop=False, marker=True) -> dict:
        env = dict(os.environ)
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        if drop:
            env.pop("AAAGENT_PROJECT", None)
        else:
            env["AAAGENT_PROJECT"] = str(project if project is not None else self.project)
        if marker:
            env[MARKER] = self.marker
        return env

    def spawn(self, code: str, env: dict | None = None) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", code], env=env or self.env(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def ready(self, proc: subprocess.Popen, env: dict) -> None:
        """Wait until the child has exec'd with our env, so /proc shows the right environ."""
        needle = f"{MARKER}={env[MARKER]}".encode()
        self.assertTrue(wait_until(lambda: needle in environ_of(proc.pid)),
                        "the child did not start with the test env")

    def assert_alive(self, proc: subprocess.Popen) -> None:
        self.assertIsNone(proc.poll(), f"pid {proc.pid} should be alive")

    def assert_dead(self, proc: subprocess.Popen) -> None:
        self.assertIsNotNone(proc.poll(), f"pid {proc.pid} should be dead")

    def pids(self, targets) -> set[int]:
        return {target.pid for target in targets}

    def await_child(self, pid_file: Path) -> int:
        """A child started by one of ours: read its pid, remember it for tearDown, wait for it."""
        self.assertTrue(wait_until(lambda: pid_file.is_file() and pid_file.read_text().strip()),
                        f"the child pid file {pid_file} was not written")
        pid = int(pid_file.read_text().strip())
        self.extra_pids.append(pid)
        self.assertTrue(wait_until(lambda: f"{MARKER}={self.marker}".encode() in environ_of(pid)),
                        "the child did not start with the test env")
        return pid

    # -- tests ---------------------------------------------------------

    def test_project_and_worktree_jobs_are_killed_others_survive(self):
        here = self.spawn(SLEEP)
        self.ready(here, self.env())
        worktree = self.project / ".aaagent" / "worktrees" / "001-x"
        other = self.spawn(SLEEP, self.env(project=worktree))
        self.ready(other, self.env(project=worktree))
        elsewhere = self.spawn(SLEEP, self.env(project=Path(self.tmp.name) / "elsewhere"))
        self.ready(elsewhere, self.env(project=Path(self.tmp.name) / "elsewhere"))
        no_project = self.spawn(SLEEP, self.env(drop=True))
        self.ready(no_project, self.env(drop=True))

        result = killer.kill_project(self.project, grace=2.0)

        self.assert_dead(here)
        self.assert_dead(other)
        self.assert_alive(elsewhere)
        self.assert_alive(no_project)
        self.assertIn(here.pid, self.pids(result.killed))
        self.assertIn(other.pid, self.pids(result.killed))
        self.assertNotIn(elsewhere.pid, self.pids(result.killed))
        self.assertEqual(result.survivors, [])

    def test_a_daemon_from_its_pid_file_and_its_job(self):
        workers = self.project / ".aaagent" / "workers"
        workers.mkdir(parents=True)
        job_pid_file = Path(self.tmp.name) / "grandchild.pid"
        daemon_code = (
            "import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', {SLEEP!r}])\n"
            f"open({str(job_pid_file)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n"
        )
        daemon = self.spawn(daemon_code, self.env(drop=True))
        (workers / "worker1.pid").write_text(str(daemon.pid))

        self.assertTrue(wait_until(lambda: job_pid_file.is_file()
                                   and job_pid_file.read_text().strip()))
        job_pid = int(job_pid_file.read_text().strip())
        self.extra_pids.append(job_pid)
        self.assertTrue(wait_until(lambda: b"time.sleep" in cmdline_of(job_pid)),
                        "the grandchild did not start")

        result = killer.kill_project(self.project, grace=2.0)

        roles = {target.pid: target.role for target in result.killed}
        self.assertEqual(roles.get(daemon.pid), "worker1")
        self.assertEqual(roles.get(job_pid), "worker1 job")
        self.assert_dead(daemon)
        self.assertTrue(wait_until(lambda: not Path(f"/proc/{job_pid}").exists())
                        or cmdline_of(job_pid) == b"")

    def test_escalates_to_sigkill(self):
        stubborn = self.spawn(IGNORE_SIGTERM)
        self.ready(stubborn, self.env())

        result = killer.kill_project(self.project, grace=0.5)

        self.assertIn(stubborn.pid, self.pids(result.killed))
        self.assert_dead(stubborn)
        self.assertEqual(result.survivors, [])

    def test_dry_run_signals_nothing(self):
        child = self.spawn(SLEEP)
        self.ready(child, self.env())

        result = killer.kill_project(self.project, dry_run=True)

        self.assertIn(child.pid, self.pids(result.killed))
        self.assertEqual(result.survivors, [])
        self.assertIsNone(result.window)
        self.assert_alive(child)

    def test_exclude_leaves_a_process_alone(self):
        child = self.spawn(SLEEP)
        self.ready(child, self.env())

        result = killer.kill_project(self.project, exclude={child.pid}, grace=2.0)

        self.assert_alive(child)
        self.assertNotIn(child.pid, self.pids(result.killed))
        self.assertIn(child.pid, self.pids(result.spared))

    def test_a_spared_parent_does_not_take_its_children_with_it(self):
        child_pid_file = Path(self.tmp.name) / "spared_child.pid"
        parent_code = (
            "import os, subprocess, sys, time\n"
            "env = dict(os.environ)\n"
            "env.pop('AAAGENT_PROJECT', None)\n"
            f"p = subprocess.Popen([sys.executable, '-c', {SLEEP!r}], env=env)\n"
            f"open({str(child_pid_file)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n"
        )
        parent = self.spawn(parent_code)
        self.ready(parent, self.env())
        child_pid = self.await_child(child_pid_file)

        result = killer.kill_project(self.project, exclude={parent.pid}, grace=2.0)

        self.assert_alive(parent)
        self.assertIn(parent.pid, self.pids(result.spared))
        self.assertNotIn(child_pid, self.pids(result.killed))
        self.assertIn(b"time.sleep", cmdline_of(child_pid), "a spared parent's child is not ours")

    def test_tmux_is_never_a_target_and_is_not_walked(self):
        sleep_bin = "/bin/sleep" if Path("/bin/sleep").exists() else shutil.which("sleep")
        self.assertIsNotNone(sleep_bin, "needs sleep(1)")
        client = subprocess.Popen(["tmux", "60"], executable=sleep_bin, env=self.env(),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        self.procs.append(client)
        self.ready(client, self.env())

        child_pid_file = Path(self.tmp.name) / "tmux_child.pid"
        server_code = (
            "import os, subprocess, time\n"
            f"python = {sys.executable!r}\n"
            "env = dict(os.environ)\n"
            "env.pop('AAAGENT_PROJECT', None)\n"
            f"p = subprocess.Popen([python, '-c', {SLEEP!r}], env=env)\n"
            f"open({str(child_pid_file)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n"
        )
        server = subprocess.Popen(["tmux: server", "-c", server_code],
                                  executable=sys.executable, env=self.env(),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        self.procs.append(server)
        self.ready(server, self.env())
        child_pid = self.await_child(child_pid_file)

        found, spared = killer.find_targets(self.project)
        found_pids = self.pids(found)

        self.assertNotIn(client.pid, found_pids, "a tmux client is never a target")
        self.assertNotIn(server.pid, found_pids, "the tmux server is never a target")
        self.assertNotIn(child_pid, found_pids, "nothing below tmux may be walked")
        self.assertNotIn(client.pid, self.pids(spared))
        self.assertNotIn(server.pid, self.pids(spared))
        self.assertIn(b"time.sleep", cmdline_of(child_pid))

    def test_the_command_kills_and_prints_the_summary_last(self):
        child = self.spawn(SLEEP)
        self.ready(child, self.env())
        env = self.env()
        env["AAAGENT_USER_CONFIG"] = str(Path(self.tmp.name) / "none.toml")

        done = subprocess.run([sys.executable, str(AAAGENT), "kill"], cwd=self.project,
                              env=env, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)

        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.strip().splitlines()
        self.assertEqual(lines[-1], "killed 1 aaagent process(es)", done.stdout)
        self.assertIn(f"pid {child.pid}", done.stdout)
        self.assert_dead(child)

    def test_a_project_without_processes(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        env = self.env(project=empty)
        env["AAAGENT_USER_CONFIG"] = str(Path(self.tmp.name) / "none.toml")

        done = subprocess.run([sys.executable, str(AAAGENT), "kill"], cwd=empty,
                              env=env, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip().splitlines()[-1],
                         "no aaagent processes are running in this project")


if __name__ == "__main__":
    unittest.main()
