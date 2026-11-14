"""`aaagent attach`: image validation, naming, copying, mentions and the tmux chat pane."""

import io
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

from aaagent import attach, cli, session  # noqa: E402
from aaagent.config import Config  # noqa: E402
from aaagent.tasks import FlowError  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"png data"
JPG = b"\xff\xd8\xff" + b"jpg data"
GIF = b"GIF89a" + b"gif data"
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"webp data"
NOW = time.strptime("2026-11-13 09:05:07", "%Y-%m-%d %H:%M:%S")


class AttachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.cwd = os.getcwd()
        os.chdir(self.project)
        self.env_backup = dict(os.environ)
        os.environ.update({
            "AAAGENT_PROJECT": str(self.project),
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)
        session.init_project(self.project, Config.load(self.project))

    def tearDown(self):
        os.chdir(self.cwd)
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def cli(self, *argv) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def names(self) -> list[str]:
        return sorted(p.name for p in attach.attachments_dir(self.project).iterdir())

    # -- sniff and names -----------------------------------------------------------

    def test_sniff(self):
        self.assertEqual(attach.sniff(PNG), "png")
        self.assertEqual(attach.sniff(JPG), "jpg")
        self.assertEqual(attach.sniff(GIF), "gif")
        self.assertEqual(attach.sniff(WEBP), "webp")
        self.assertIsNone(attach.sniff(b""))
        self.assertIsNone(attach.sniff(b"hello world"))
        self.assertIsNone(attach.sniff(b"RIFF\x00\x00\x00\x00NOPE"))

    def test_safe_name(self):
        self.assertEqual(attach.safe_name("shot.png", "png", NOW), "20261113-090507-shot.png")
        self.assertEqual(attach.safe_name("My Holiday Photo.PNG", "png", NOW),
                         "20261113-090507-My-Holiday-Photo.png")
        self.assertEqual(attach.safe_name("스크린샷.png", "png", NOW),
                         "20261113-090507-image.png")
        self.assertEqual(attach.safe_name("a -- b.png", "png", NOW), "20261113-090507-a-b.png")
        self.assertEqual(attach.safe_name("x" * 100 + ".png", "png", NOW),
                         "20261113-090507-" + "x" * 40 + ".png")
        self.assertEqual(attach.safe_name("clip.bin", "webp", NOW), "20261113-090507-clip.webp")
        self.assertEqual(attach.safe_name("photo.jpeg", "jpg", NOW), "20261113-090507-photo.jpeg")

    # -- saving --------------------------------------------------------------------

    def test_save_refuses_empty_oversize_and_non_images(self):
        with self.assertRaisesRegex(FlowError, "empty"):
            attach.save(self.project, b"", "shot.png")
        with mock.patch.object(attach, "MAX_BYTES", 8):
            with self.assertRaisesRegex(FlowError, "limit"):
                attach.save(self.project, PNG, "shot.png")
        with self.assertRaisesRegex(FlowError, "not a PNG, JPEG, GIF or WebP image"):
            attach.save(self.project, b"hello", "shot.png")
        self.assertEqual(self.names(), [], "nothing is written for a refused file")

    def test_save_adds_a_suffix_on_a_collision(self):
        with mock.patch.object(attach, "safe_name", return_value="20261113-090507-shot.png"):
            first = attach.save(self.project, PNG, "shot.png")
            second = attach.save(self.project, PNG, "shot.png")
            third = attach.save(self.project, PNG, "shot.png")
        self.assertEqual(first, attach.attachments_dir(self.project) / "20261113-090507-shot.png")
        self.assertEqual(second.name, "20261113-090507-shot-1.png")
        self.assertEqual(third.name, "20261113-090507-shot-2.png")

    def test_mention_is_relative_and_quotes_spaces(self):
        path = attach.attachments_dir(self.project) / "shot.png"
        self.assertEqual(attach.mention(self.project, path), "@.aaagent/attachments/shot.png ")
        spaced = attach.attachments_dir(self.project) / "my shot.png"
        self.assertEqual(attach.mention(self.project, spaced), '@".aaagent/attachments/my shot.png" ')

    # -- the command ---------------------------------------------------------------

    def test_attach_two_files_prints_mentions(self):
        (self.project / "a.png").write_bytes(PNG)
        (self.project / "b.jpg").write_bytes(JPG)
        code, out = self.cli("attach", "a.png", "b.jpg", "--no-send")
        self.assertEqual(code, 0)
        names = self.names()
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].endswith("-a.png") and names[1].endswith("-b.jpg"))
        self.assertIn("attached .aaagent/attachments/", out)
        self.assertIn("@.aaagent/attachments/", out)
        self.assertIn("paste this into the chat:", out)

    def test_attach_reads_stdin(self):
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(PNG))):
            code, out = self.cli("attach", "-", "--no-send")
        self.assertEqual(code, 0)
        self.assertTrue(self.names()[0].endswith("-pasted.png"))
        self.assertIn("@.aaagent/attachments/", out)

    def test_missing_file_is_an_error(self):
        with self.assertRaisesRegex(FlowError, "no such file"):
            attach.attach_files(self.project, ["nope.png"])
        code, _ = self.cli("attach", "nope.png", "--no-send")
        self.assertEqual(code, 2)

    def test_a_file_already_in_attachments_is_not_copied_again(self):
        first = attach.save(self.project, PNG, "shot.png")
        before = self.names()
        paths = attach.attach_files(self.project, [str(first)])
        self.assertEqual(paths, [first])
        self.assertEqual(self.names(), before)

    # -- the chat pane -------------------------------------------------------------

    def test_type_into_chat_without_a_pane_file(self):
        self.assertFalse(attach.type_into_chat(self.project, "@x "))

    def test_type_into_chat_sends_literal_keys(self):
        attach.chat_pane_file(self.project).write_text("%9\n")
        ok = mock.Mock(returncode=0, stdout="%9\n", stderr="")
        with mock.patch.object(attach.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(attach.subprocess, "run", return_value=ok) as run:
            self.assertTrue(attach.type_into_chat(self.project, "@.aaagent/attachments/x.png "))
        calls = run.call_args_list
        self.assertEqual(calls[0].args[0],
                         ["tmux", "display-message", "-p", "-t", "%9", "#{pane_id}"])
        self.assertEqual(calls[1].args[0],
                         ["tmux", "send-keys", "-t", "%9", "-l", "@.aaagent/attachments/x.png "])

    def test_type_into_chat_stops_when_the_pane_is_gone(self):
        attach.chat_pane_file(self.project).write_text("%9\n")
        wrong = mock.Mock(returncode=0, stdout="%8\n", stderr="")
        with mock.patch.object(attach.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(attach.subprocess, "run", return_value=wrong) as run:
            self.assertFalse(attach.type_into_chat(self.project, "@x "))
        self.assertEqual(len(run.call_args_list), 1, "nothing is typed into a stale pane")

    # -- respawning the chat pane --------------------------------------------------

    def test_respawn_without_a_session_id_starts_a_fresh_chat(self):
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(session, "find_claude", return_value="/usr/bin/claude"), \
                mock.patch.object(session.subprocess, "run", return_value=done) as run:
            session.respawn_chat(self.project, "%9", None, "tmux kill-window")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["tmux", "respawn-pane", "-k"])
        self.assertIn("%9", argv)
        script = (self.project / ".aaagent" / "run-orchestrator.sh").read_text()
        self.assertNotIn("--resume", script)
        self.assertIn("tmux kill-window", script)


if __name__ == "__main__":
    unittest.main()
