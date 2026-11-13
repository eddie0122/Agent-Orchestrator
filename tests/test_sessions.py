"""Chat sessions: the history in .aaagent/sessions.json, the hook, and the links from tasks."""

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

from aaagent import sessions, tasks  # noqa: E402
from aaagent.config import Config  # noqa: E402
from aaagent.tasks import FlowError  # noqa: E402

S1 = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
S2 = "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
S3 = "33333333-cccc-4ccc-8ccc-cccccccccccc"


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.agent = self.project / ".aaagent"
        self.agent.mkdir()
        self.env_backup = dict(os.environ)
        os.environ.update({
            "AAAGENT_USER_CONFIG": str(Path(self.tmp.name) / "none.toml"),
            "AAAGENT_PROJECT": str(self.project),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        })
        subprocess.run(["git", "init", "-q"], cwd=self.project, check=True)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def transcript(self, name: str) -> str:
        path = Path(self.tmp.name) / name
        path.write_text("{}\n")
        return str(path)

    def write_transcript(self, name: str, records: list[dict]) -> str:
        path = Path(self.tmp.name) / name
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return str(path)

    def write_history(self, entries: list[dict], current: str | None = None) -> None:
        sessions.sessions_file(self.project).write_text(json.dumps(
            {"current": current, "sessions": entries}))

    def cli(self, *argv) -> tuple[int, str]:
        from aaagent import cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    # -- the history ---------------------------------------------------------

    def test_record_start_keeps_the_history_oldest_first(self):
        t1 = self.transcript("s1.jsonl")
        sessions.record_start(self.project, S1, t1, now="2026-11-13T21:00:00+0900")
        data = sessions.load(self.project)
        self.assertEqual(data["current"], S1)
        self.assertEqual(len(data["sessions"]), 1)
        self.assertEqual(data["sessions"][0], {
            "id": S1, "transcript_path": t1, "started": "2026-11-13T21:00:00+0900",
            "last_active": "2026-11-13T21:00:00+0900", "title": ""})
        self.assertEqual(sessions.current_session(self.project), S1)

        t2 = self.transcript("s2.jsonl")
        sessions.record_start(self.project, S2, t2, now="2026-11-13T22:00:00+0900")
        # resuming a known chat only moves its last_active, and does not duplicate it
        sessions.record_start(self.project, S1, t2, now="2026-11-13T22:30:00+0900")
        data = sessions.load(self.project)
        self.assertEqual([s["id"] for s in data["sessions"]], [S1, S2], "oldest first")
        self.assertEqual(data["current"], S1)
        self.assertEqual(data["sessions"][0]["started"], "2026-11-13T21:00:00+0900")
        self.assertEqual(data["sessions"][0]["last_active"], "2026-11-13T22:30:00+0900")
        self.assertEqual(data["sessions"][0]["transcript_path"], t2)

    def test_record_prompt_titles_the_session_once(self):
        t1 = self.transcript("s1.jsonl")
        sessions.record_start(self.project, S1, t1, now="2026-11-13T21:00:00+0900")
        sessions.record_prompt(self.project, S1, "\n  Add   a health\ncheck please  \n",
                               now="2026-11-13T21:01:00+0900")
        entry = sessions.load(self.project)["sessions"][0]
        self.assertEqual(entry["title"], "Add a health")
        self.assertEqual(entry["last_active"], "2026-11-13T21:01:00+0900")
        self.assertEqual(entry["started"], "2026-11-13T21:00:00+0900")

        sessions.record_prompt(self.project, S1, "a later ask", now="2026-11-13T21:02:00+0900")
        entry = sessions.load(self.project)["sessions"][0]
        self.assertEqual(entry["title"], "Add a health", "only the first prompt titles a chat")
        self.assertEqual(entry["last_active"], "2026-11-13T21:02:00+0900")

    def test_record_prompt_ignores_slash_commands_and_cuts_long_titles(self):
        sessions.record_start(self.project, S1, self.transcript("s1.jsonl"))
        sessions.record_prompt(self.project, S1, "/clear")
        self.assertEqual(sessions.load(self.project)["sessions"][0]["title"], "")

        long = "x" * 200
        sessions.record_prompt(self.project, S2, long)
        entry = next(s for s in sessions.load(self.project)["sessions"] if s["id"] == S2)
        self.assertEqual(len(entry["title"]), 80)
        self.assertTrue(entry["title"].endswith("…"))
        self.assertEqual(entry["title"], "x" * 79 + "…")
        self.assertEqual(entry["started"], entry["last_active"], "an unknown chat is added")

    def test_keeps_at_most_100_sessions(self):
        for i in range(105):
            sessions.record_start(self.project, f"session-{i:03d}", None,
                                  now=f"2026-11-13T2{i // 60}:{i % 60:02d}:00+0900")
        data = sessions.load(self.project)
        self.assertEqual(len(data["sessions"]), 100)
        self.assertEqual(data["sessions"][0]["id"], "session-005", "the oldest ones are dropped")
        self.assertEqual(data["sessions"][-1]["id"], "session-104")
        self.assertEqual(data["current"], "session-104")

    def test_migrates_from_chat_json(self):
        t1 = self.transcript("s1.jsonl")
        (self.agent / "chat.json").write_text(json.dumps({
            "session_id": S2, "transcript_path": t1, "at": "2026-11-13T22:00:00+0900",
            "previous": {"session_id": S1, "transcript_path": t1, "at": "2026-11-13T21:00:00+0900"},
        }))
        data = sessions.load(self.project)
        self.assertEqual(data["current"], S2)
        self.assertEqual([s["id"] for s in data["sessions"]], [S1, S2], "previous, then the current chat")
        self.assertEqual(data["sessions"][0]["started"], "2026-11-13T21:00:00+0900")
        self.assertEqual(data["sessions"][1]["last_active"], "2026-11-13T22:00:00+0900")
        self.assertEqual([s["title"] for s in data["sessions"]], ["", ""])
        self.assertFalse(sessions.sessions_file(self.project).exists(), "load() only reads")

        sessions.record_start(self.project, S3, None)
        on_disk = json.loads(sessions.sessions_file(self.project).read_text())
        self.assertEqual([s["id"] for s in on_disk["sessions"]], [S1, S2, S3])

    def test_a_corrupt_file_is_an_empty_history(self):
        sessions.sessions_file(self.project).write_text("not json")
        self.assertEqual(sessions.load(self.project), {"current": None, "sessions": []})

    # -- titles recovered from the conversation ------------------------------

    def test_title_from_transcript_takes_the_first_real_prompt(self):
        path = self.write_transcript("t.jsonl", [
            {"type": "user", "message": {"role": "user",
                                         "content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta prompt"}},
            {"type": "user", "message": {"role": "user",
                                         "content": "<command-name>/clear</command-name>"}},
            {"type": "user", "message": {"role": "user", "content": "/clear"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "not a prompt"}},
            {"type": "user", "message": {"role": "user", "content": "  Add   a health\ncheck  "}},
            {"type": "user", "message": {"role": "user", "content": "a later prompt"}},
        ])
        self.assertEqual(sessions.title_from_transcript(path), "Add a health")

    def test_title_from_transcript_reads_the_text_part_of_a_list(self):
        path = self.write_transcript("t.jsonl", [
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "done"},
                {"type": "text", "text": "Fix the failing session test"}]}},
        ])
        self.assertEqual(sessions.title_from_transcript(path), "Fix the failing session test")

    def test_title_from_transcript_skips_junk_and_missing_files(self):
        path = self.write_transcript("t.jsonl", [
            {"type": "user", "message": {"role": "user",
                                         "content": [{"type": "tool_result", "content": "only"}]}},
        ])
        path = str(path)
        Path(path).write_text("not json\n{}\n[]\n" + Path(path).read_text())
        self.assertEqual(sessions.title_from_transcript(path), "")
        self.assertEqual(sessions.title_from_transcript(str(Path(self.tmp.name) / "missing.jsonl")), "")
        self.assertEqual(sessions.title_from_transcript(None), "")

    def test_title_from_transcript_reads_only_the_head(self):
        records = [{"type": "assistant", "message": {"role": "assistant", "content": "filler"}}] * 400
        records.append({"type": "user", "message": {"role": "user", "content": "too far down"}})
        self.assertEqual(sessions.title_from_transcript(self.write_transcript("t.jsonl", records)), "")

    def test_record_start_titles_a_session_from_its_transcript(self):
        t1 = self.write_transcript("s1.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "opened with --session"}}])
        sessions.record_start(self.project, S1, t1)
        self.assertEqual(sessions.load(self.project)["sessions"][0]["title"], "opened with --session")

    def test_record_start_titles_the_migrated_previous_chat_and_keeps_it(self):
        t1 = self.write_transcript("s1.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "the earlier chat"}}])
        t2 = self.write_transcript("s2.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "the chat after it"}}])
        (self.agent / "chat.json").write_text(json.dumps({
            "session_id": S2, "transcript_path": t2, "at": "2026-11-13T22:00:00+0900",
            "previous": {"session_id": S1, "transcript_path": t1,
                         "at": "2026-11-13T21:00:00+0900"},
        }))
        sessions.record_start(self.project, S2, t2, now="2026-11-13T23:00:00+0900")
        data = sessions.load(self.project)
        self.assertEqual({s["id"]: s["title"] for s in data["sessions"]},
                         {S1: "the earlier chat", S2: "the chat after it"})

        Path(t1).unlink()
        Path(t2).unlink()
        data = sessions.load(self.project)
        self.assertEqual({s["id"]: s["title"] for s in data["sessions"]},
                         {S1: "the earlier chat", S2: "the chat after it"},
                         "the titles are stored, not read again")

    # -- the list the commands show ------------------------------------------

    def test_list_sessions_newest_first_with_tasks_and_availability(self):
        t1 = self.transcript("s1.jsonl")
        sessions.record_start(self.project, S1, t1, now="2026-11-13T21:00:00+0900")
        sessions.record_prompt(self.project, S1, "first job", now="2026-11-13T21:01:00+0900")
        sessions.record_start(self.project, S2, None, now="2026-11-13T22:00:00+0900")
        sessions.record_start(self.project, S3, self.transcript("s3.jsonl"),
                              now="2026-11-13T23:00:00+0900")

        first = tasks.create(self.project, "one")
        first.update(chat_session=S1)
        second = tasks.create(self.project, "two")
        second.update(chat_session=S1)
        tasks.create(self.project, "three")     # linked to the current session, S3

        shown = sessions.list_sessions(self.project)
        self.assertEqual([s["id"] for s in shown], [S3, S2, S1], "newest last_active first")
        by_id = {s["id"]: s for s in shown}
        self.assertEqual(by_id[S1]["short"], S1[:8])
        self.assertEqual(by_id[S1]["title"], "first job")
        self.assertEqual(by_id[S1]["tasks"], [first.id, second.id], "in task order")
        self.assertTrue(by_id[S1]["available"])
        self.assertFalse(by_id[S2]["available"], "no transcript on disk")
        self.assertTrue(by_id[S3]["current"])
        self.assertFalse(by_id[S1]["current"])
        self.assertEqual(by_id[S3]["tasks"], [tasks.all_tasks(self.project)[-1].id])
        self.assertEqual(by_id[S2]["tasks"], [])

        self.assertEqual([s["id"] for s in sessions.list_sessions(self.project, limit=2)], [S3, S2])

    def test_list_sessions_titles_an_untitled_session_from_its_transcript(self):
        t1 = self.write_transcript("s1.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "an old conversation"}}])
        self.write_history([{"id": S1, "transcript_path": t1,
                             "started": "2026-11-13T21:00:00+0900",
                             "last_active": "2026-11-13T21:00:00+0900", "title": ""}], current=S1)
        shown = sessions.list_sessions(self.project)
        self.assertEqual(shown[0]["title"], "an old conversation")
        self.assertEqual(json.loads(sessions.sessions_file(self.project).read_text())["sessions"][0]["title"],
                         "", "list_sessions() only reads")

    def test_find_by_id_prefix(self):
        sessions.record_start(self.project, S1, self.transcript("s1.jsonl"))
        self.assertEqual(sessions.find(self.project, S1)["id"], S1)
        self.assertEqual(sessions.find(self.project, S1[:8])["id"], S1)
        with self.assertRaisesRegex(FlowError, "no session matches '111'"):
            sessions.find(self.project, "111")         # needs the full id or >= 4 chars
        with self.assertRaisesRegex(FlowError, "no session matches"):
            sessions.find(self.project, "nope-nope")
        with self.assertRaisesRegex(FlowError, "no session matches"):
            sessions.find(self.project, "123")

    def test_find_refuses_ambiguous_and_gone_sessions(self):
        sessions.record_start(self.project, "abcd1111-aaaa", self.transcript("a.jsonl"))
        sessions.record_start(self.project, "abcd2222-bbbb", self.transcript("b.jsonl"))
        with self.assertRaisesRegex(FlowError, "matches several sessions"):
            sessions.find(self.project, "abcd")
        self.assertEqual(sessions.find(self.project, "abcd1111-aaaa")["id"], "abcd1111-aaaa")

        sessions.record_start(self.project, "gone0000-cccc", str(Path(self.tmp.name) / "missing.jsonl"))
        with self.assertRaisesRegex(FlowError, "can no longer be reopened"):
            sessions.find(self.project, "gone0000-cccc")

    # -- the hook ------------------------------------------------------------

    HOOK = ROOT / "claude" / "hooks" / "record_session.py"

    def run_hook(self, payload, project=None):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, str(self.HOOK)], input=text, capture_output=True,
                              text=True, env={**os.environ, "AAAGENT_PROJECT": str(project or self.project)})

    def test_the_hook_records_starts_and_prompts(self):
        t1 = self.transcript("s1.jsonl")
        result = self.run_hook({"session_id": S1, "transcript_path": t1, "cwd": str(self.project),
                                "hook_event_name": "SessionStart", "source": "startup"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        chat = json.loads((self.agent / "chat.json").read_text())
        self.assertEqual(chat["session_id"], S1)
        self.assertEqual(sessions.load(self.project)["current"], S1)

        result = self.run_hook({"session_id": S1, "prompt": "make the tests pass",
                                "cwd": str(self.project), "hook_event_name": "UserPromptSubmit"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(json.loads((self.agent / "chat.json").read_text()), chat,
                         "a prompt must not touch chat.json")
        entry = sessions.load(self.project)["sessions"][0]
        self.assertEqual(entry["title"], "make the tests pass")
        self.assertEqual(sessions.load(self.project)["current"], S1)

        self.run_hook({"session_id": S2, "transcript_path": self.transcript("s2.jsonl"),
                       "cwd": str(self.project), "hook_event_name": "SessionStart", "source": "clear"})
        self.assertEqual(sessions.load(self.project)["current"], S2)
        self.assertEqual([s["id"] for s in sessions.load(self.project)["sessions"]], [S1, S2])
        self.assertEqual(json.loads((self.agent / "chat.json").read_text())["session_id"], S2)

    def test_the_hook_still_never_fails(self):
        self.assertEqual(self.run_hook("not json").returncode, 0)
        elsewhere = Path(self.tmp.name) / "empty"
        elsewhere.mkdir()
        self.assertEqual(self.run_hook({"session_id": "x"}, project=elsewhere).returncode, 0)
        self.assertFalse((elsewhere / ".aaagent").exists())

    # -- the links to tasks and the commands ---------------------------------

    def test_create_stores_the_chat_session(self):
        self.assertIsNone(tasks.create(self.project, "before any chat").load()["chat_session"])
        sessions.record_start(self.project, S1, self.transcript("s1.jsonl"))
        task = tasks.create(self.project, "from the chat")
        self.assertEqual(task.load()["chat_session"], S1)
        self.assertIn("sessions", task.load(), "the worker sessions key is unchanged")

    def test_status_data_lists_sessions_and_each_task_session(self):
        from aaagent import status
        sessions.record_start(self.project, S1, self.transcript("s1.jsonl"))
        task = tasks.create(self.project, "linked")
        data = status.status_data(self.project, Config.load(self.project))
        self.assertEqual(data["current_session"], S1)
        self.assertEqual([s["id"] for s in data["sessions"]], [S1])
        self.assertEqual(data["sessions"][0]["tasks"], [task.id])
        self.assertEqual(data["tasks"][0]["session"], S1)

    def test_sessions_command(self):
        t1 = self.transcript("s1.jsonl")
        sessions.record_start(self.project, S1, t1, now="2026-11-13T21:00:00+0900")
        sessions.record_prompt(self.project, S1, "first job", now="2026-11-13T21:01:00+0900")
        sessions.record_start(self.project, S2, None, now="2026-11-13T22:00:00+0900")

        code, out = self.cli("sessions", "--json")
        self.assertEqual(code, 0)
        listed = json.loads(out)
        self.assertEqual([s["id"] for s in listed], [S2, S1])
        self.assertEqual(listed[1]["title"], "first job")
        self.assertEqual(listed[0]["available"], False)

        code, out = self.cli("sessions")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0].split()[0], "*", "the current session is marked")
        self.assertIn(S2[:8], lines[0])
        self.assertIn("2026-11-13 22:00", lines[0])
        self.assertIn("(no messages yet)", lines[0])
        self.assertIn("(gone)", lines[0])
        self.assertIn(S1[:8], lines[1])
        self.assertIn("2026-11-13 21:01", lines[1])
        self.assertIn("first job", lines[1])
        self.assertIn("reopen one with: aaagent --session <id>", out)

        # --all shows every session; the default stops at 20
        for i in range(21):
            sessions.record_start(self.project, f"extra-{i:03d}", None)
        self.assertEqual(len(json.loads(self.cli("sessions", "--json")[1])), 20)
        self.assertEqual(len(json.loads(self.cli("sessions", "--all", "--json")[1])), 23)

    def test_launch_reopens_the_named_session(self):
        from unittest import mock
        from aaagent import session
        sessions.record_start(self.project, S1, self.transcript("s1.jsonl"))
        with mock.patch.object(session, "find_claude", return_value="/bin/true"), \
                mock.patch.object(session, "claude_command", return_value=["true"]) as command:
            code = session.launch(self.project, use_tmux=False, manage_workers=False,
                                  session_ref=S1[:8])
        self.assertEqual(code, 0)
        self.assertEqual(command.call_args.args[4], S1, "claude resumes the session that matched")
        with mock.patch.object(session, "find_claude", return_value="/bin/true"):
            with self.assertRaisesRegex(FlowError, "no session matches"):
                session.launch(self.project, use_tmux=False, manage_workers=False,
                               session_ref="nope-nope")

    def test_claude_settings_registers_the_hook_for_prompts(self):
        from aaagent import session
        hooks = session.claude_settings(self.project)["hooks"]
        commands = [h["command"] for h in hooks["UserPromptSubmit"][0]["hooks"]]
        self.assertTrue(any("local_images.py" in c for c in commands), "images still run first")
        self.assertTrue(any("record_session.py" in c for c in commands))
        self.assertIn("record_session.py", hooks["SessionStart"][0]["hooks"][0]["command"])

    def test_session_flag_routes_to_start(self):
        from unittest import mock
        from aaagent import cli, session
        with mock.patch.object(session, "launch", return_value=0) as launch:
            self.assertEqual(cli.main(["--session", "abcd"]), 0)
        self.assertEqual(launch.call_args.kwargs["session_ref"], "abcd")


if __name__ == "__main__":
    unittest.main()
