"""`claude/hooks/chat_stop.py`: the new chat `aaagent pr` asks for.

The hook runs when the planner's reply ends. In VSCode it hands the marker to the extension,
in the tmux layout it starts a detached restart, and in a plain terminal it drops the marker.
"""

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "claude" / "hooks" / "chat_stop.py"
RESTART = ROOT / "claude" / "hooks" / "chat_restart.py"
sys.path.insert(0, str(ROOT / "lib"))

from aaagent import session  # noqa: E402

MARKER = {"reason": "pull-request", "branch": "aaagent/001-health-endpoint", "base": "main",
          "pr_url": "https://example.invalid/pr/1", "pr_error": None,
          "prompt": "the pull request is open; what next?", "at": "2026-11-16T10:00:00+0900"}


def load_hook():
    spec = importlib.util.spec_from_file_location("chat_stop", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chat_stop = load_hook()


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.agent_dir = self.project / ".aaagent"
        self.agent_dir.mkdir()
        self.pending = self.agent_dir / "new-chat-pending.json"
        self.inbox = self.agent_dir / "new-chat.json"
        self.env_backup = dict(os.environ)
        os.environ.update({
            "AAAGENT_PROJECT": str(self.project),
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
        })
        os.environ.pop("AAAGENT_VSCODE", None)
        os.environ.pop("AAAGENT_TMUX_CLOSE", None)
        os.environ.pop("TMUX_PANE", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def run_hook(self, **env):
        """Run the hook the way Claude Code does: the event on stdin, the chat's env."""
        full = {k: v for k, v in os.environ.items()
                if k not in ("AAAGENT_PROJECT", "AAAGENT_VSCODE", "TMUX_PANE")}
        full.update(env)
        return subprocess.run([sys.executable, str(HOOK)], input="{}", text=True,
                              capture_output=True, env=full)

    def write_pending(self, **data) -> None:
        self.pending.write_text(json.dumps(data or MARKER) + "\n")


class StopHookProcessTest(HookTestCase):
    """The hook as Claude Code runs it: a fresh process, exit code 0 only."""

    def test_vscode_turns_the_marker_into_a_request_for_the_extension(self):
        self.write_pending()
        done = self.run_hook(AAAGENT_PROJECT=str(self.project), AAAGENT_VSCODE="1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.pending.exists())
        self.assertEqual(json.loads(self.inbox.read_text())["prompt"], MARKER["prompt"])

    def test_a_plain_terminal_drops_the_marker(self):
        self.write_pending()
        done = self.run_hook(AAAGENT_PROJECT=str(self.project))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.pending.exists(), "the marker must not fire a restart later")
        self.assertFalse(self.inbox.exists())

    def test_without_a_marker_nothing_changes(self):
        before = sorted(p.name for p in self.agent_dir.iterdir())
        done = self.run_hook(AAAGENT_PROJECT=str(self.project), AAAGENT_VSCODE="1")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(p.name for p in self.agent_dir.iterdir()), before)

    def test_without_a_project_it_does_nothing(self):
        self.write_pending()
        env = {k: v for k, v in os.environ.items() if k != "AAAGENT_PROJECT"}
        done = subprocess.run([sys.executable, str(HOOK)], input="{}", text=True,
                              capture_output=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(self.pending.exists())


class StopHookTest(HookTestCase):
    """The branches that decide where the marker goes."""

    def setUp(self):
        super().setUp()
        self.pane_file = self.agent_dir / "chat-pane"
        self.pane_file.write_text("%9\n")

    def test_inside_the_recorded_pane_it_starts_a_detached_restart(self):
        self.write_pending()
        os.environ["TMUX_PANE"] = "%9"
        shown = mock.Mock(returncode=0, stdout="%9\n", stderr="")
        with mock.patch.object(chat_stop.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(chat_stop.subprocess, "run", return_value=shown), \
                mock.patch.object(chat_stop, "restart_detached") as restart:
            self.assertEqual(chat_stop.main(), 0)
        restart.assert_called_once_with(self.project)
        self.assertTrue(self.pending.exists(), "the restart removes the marker, not the hook")

    def test_the_restart_runs_detached(self):
        self.write_pending()
        os.environ["TMUX_PANE"] = "%9"
        shown = mock.Mock(returncode=0, stdout="%9\n", stderr="")
        with mock.patch.object(chat_stop.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(chat_stop.subprocess, "run", return_value=shown), \
                mock.patch.object(chat_stop.subprocess, "Popen") as popen:
            self.assertEqual(chat_stop.main(), 0)
        self.assertEqual(popen.call_args.args[0],
                         [sys.executable, str(RESTART), str(self.project)])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_a_different_pane_is_a_plain_terminal(self):
        self.write_pending()
        os.environ["TMUX_PANE"] = "%8"      # a live pane, but not the one that recorded this
        with mock.patch.object(chat_stop, "restart_detached") as restart, \
                mock.patch.object(chat_stop.subprocess, "run") as run:
            self.assertEqual(chat_stop.main(), 0)
        restart.assert_not_called()
        run.assert_not_called()             # the recorded pane is not even looked up
        self.assertFalse(self.pending.exists(), "the marker must not fire a restart later")

    def test_without_tmux_pane_it_is_a_plain_terminal(self):
        self.write_pending()
        os.environ.pop("TMUX_PANE", None)
        with mock.patch.object(chat_stop, "restart_detached") as restart:
            self.assertEqual(chat_stop.main(), 0)
        restart.assert_not_called()
        self.assertFalse(self.pending.exists(), "a plain terminal has nothing to restart")

    def test_a_dead_tmux_pane_is_a_plain_terminal(self):
        self.write_pending()
        os.environ["TMUX_PANE"] = "%9"
        with mock.patch.object(chat_stop.shutil, "which", return_value=None), \
                mock.patch.object(chat_stop, "restart_detached") as restart:
            self.assertEqual(chat_stop.main(), 0)
        restart.assert_not_called()
        self.assertFalse(self.pending.exists())

    def test_a_pane_that_answers_with_another_id_is_a_plain_terminal(self):
        self.write_pending()
        os.environ["TMUX_PANE"] = "%9"
        reused = mock.Mock(returncode=0, stdout="%10\n", stderr="")
        with mock.patch.object(chat_stop.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(chat_stop.subprocess, "run", return_value=reused), \
                mock.patch.object(chat_stop, "restart_detached") as restart:
            self.assertEqual(chat_stop.main(), 0)
        restart.assert_not_called()
        self.assertFalse(self.pending.exists())

    def test_a_broken_project_path_never_fails_the_chat(self):
        not_a_folder = Path(self.tmp.name) / "file"
        not_a_folder.write_text("x")
        done = self.run_hook(AAAGENT_PROJECT=str(not_a_folder), AAAGENT_VSCODE="1")
        self.assertEqual(done.returncode, 0, done.stderr)


class RestartPendingChatTest(HookTestCase):
    """`session.restart_pending_chat`: the detached restart's work."""

    def setUp(self):
        super().setUp()
        self.pane = self.agent_dir / "chat-pane"
        self.pane.write_text("%9\n")
        self.script = self.agent_dir / "run-orchestrator.sh"
        self.done = mock.Mock(returncode=0, stdout="%9\n", stderr="")

    def command_argv(self) -> list[str]:
        """The chat command in run-orchestrator.sh, as the arguments Claude Code receives."""
        lines = self.script.read_text().splitlines()
        start = next(i for i, line in enumerate(lines) if "--permission-mode" in line)
        stop = next(i for i, line in enumerate(lines) if "stop >/dev/null" in line)
        return shlex.split("\n".join(lines[start:stop]))

    def test_it_respawns_the_chat_on_the_prompt(self):
        self.write_pending()
        with mock.patch.object(session, "find_claude", return_value="/usr/bin/claude"), \
                mock.patch.object(session.subprocess, "run", return_value=self.done) as run:
            self.assertTrue(session.restart_pending_chat(self.project, delay=0))

        respawn = [c.args[0] for c in run.call_args_list
                   if c.args[0][:3] == ["tmux", "respawn-pane", "-k"]]
        self.assertEqual(len(respawn), 1)
        self.assertIn("%9", respawn[0])
        self.assertFalse(self.pending.exists(), "one restart per marker")

        lines = self.script.read_text().splitlines()
        command = self.command_argv()
        self.assertEqual(command[-1], MARKER["prompt"], "the prompt is the chat's first message")
        self.assertNotIn("--resume", command)
        self.assertTrue(any("--permission-mode" in line for line in lines))

    def test_it_uses_the_exported_close_command(self):
        self.write_pending(prompt="go on")
        os.environ["AAAGENT_TMUX_CLOSE"] = "tmux kill-session -t aaagent-proj"
        with mock.patch.object(session, "find_claude", return_value="/usr/bin/claude"), \
                mock.patch.object(session.subprocess, "run", return_value=self.done):
            self.assertTrue(session.restart_pending_chat(self.project, delay=0))
        text = self.script.read_text()
        self.assertIn(f"AAAGENT_TMUX_CLOSE={shlex.quote(os.environ['AAAGENT_TMUX_CLOSE'])}", text)
        self.assertIn("tmux kill-session -t aaagent-proj", text.splitlines()[-1])

    def test_an_empty_prompt_starts_the_chat_without_a_message(self):
        self.write_pending(prompt="")
        with mock.patch.object(session, "find_claude", return_value="/usr/bin/claude"), \
                mock.patch.object(session.subprocess, "run", return_value=self.done):
            self.assertTrue(session.restart_pending_chat(self.project, delay=0))
        self.assertEqual(self.command_argv()[-2:], ["-n", "aaagent proj"],
                         "no first message is passed")

    def test_two_restarts_start_one_chat(self):
        """Two Stop events inside the delay: the marker is claimed by exactly one of them."""
        self.write_pending()
        with mock.patch.object(session, "find_claude", return_value="/usr/bin/claude"), \
                mock.patch.object(session.subprocess, "run", return_value=self.done) as run:
            self.assertTrue(session.restart_pending_chat(self.project, delay=0))
            self.assertFalse(session.restart_pending_chat(self.project, delay=0))
        respawns = [c for c in run.call_args_list
                    if c.args[0][:3] == ["tmux", "respawn-pane", "-k"]]
        self.assertEqual(len(respawns), 1, "exactly one new chat")
        self.assertFalse(self.pending.exists())
        self.assertEqual(list(self.agent_dir.glob("new-chat-claimed*")), [])

    def test_without_a_marker_there_is_nothing_to_do(self):
        with mock.patch.object(session, "respawn_chat") as respawn:
            self.assertFalse(session.restart_pending_chat(self.project, delay=0))
        respawn.assert_not_called()

    def test_a_broken_marker_is_dropped(self):
        self.pending.write_text("{not json")
        with mock.patch.object(session, "respawn_chat") as respawn:
            self.assertFalse(session.restart_pending_chat(self.project, delay=0))
        respawn.assert_not_called()
        self.assertFalse(self.pending.exists(), "a broken marker must not fire later")

    def test_without_a_chat_pane_nothing_is_restarted(self):
        self.write_pending()
        self.pane.unlink()
        with mock.patch.object(session, "respawn_chat") as respawn:
            self.assertFalse(session.restart_pending_chat(self.project, delay=0))
        respawn.assert_not_called()
        self.assertFalse(self.pending.exists(), "the marker is dropped, not left for later")


class ChatSettingsTest(HookTestCase):
    """The chat knows how to close itself, and Claude Code runs the Stop hook."""

    def test_claude_settings_registers_the_stop_hook(self):
        hooks = session.claude_settings(self.project)["hooks"]
        self.assertIn("chat_stop.py", hooks["Stop"][0]["hooks"][0]["command"])

    def test_the_chat_script_exports_how_the_window_closes(self):
        env = session.session_env(self.project)
        script = session._write_chat_script(self.project, ["claude", "hi"], env,
                                            "tmux kill-window")
        text = script.read_text()
        self.assertIn(f"AAAGENT_TMUX_CLOSE={shlex.quote('tmux kill-window')}", text)
        self.assertIn("claude hi", text)


if __name__ == "__main__":
    unittest.main()
