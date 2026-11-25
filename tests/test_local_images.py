"""`claude/hooks/local_images.py`: finding the user's file paths and the fetch round trip."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "claude" / "hooks" / "local_images.py"
os.environ.setdefault("AAAGENT_POLL_SECONDS", "0.05")

PNG = b"\x89PNG\r\n\x1a\n" + b"png data"


def load_hook():
    spec = importlib.util.spec_from_file_location("local_images", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


local_images = load_hook()


class FindPathsTest(unittest.TestCase):
    """The prompt forms a Finder drop or a typed path produces."""

    def test_single_quoted_mac_path(self):
        prompt = "'/Users/hanul/Downloads/band.jpg' describe attached image"
        self.assertEqual(local_images.find_paths(prompt), ["/Users/hanul/Downloads/band.jpg"])

    def test_double_quoted_path_with_spaces(self):
        self.assertEqual(local_images.find_paths('"/Users/hanul/Pictures/a b.jpeg"'),
                         ["/Users/hanul/Pictures/a b.jpeg"])

    def test_quoted_path_with_a_narrow_no_break_space(self):
        prompt = "'/Users/hanul/Desktop/Screenshot\u202f2026-11-13.png' what is this"
        self.assertEqual(local_images.find_paths(prompt),
                         ["/Users/hanul/Desktop/Screenshot\u202f2026-11-13.png"])

    def test_backslash_escaped_spaces(self):
        prompt = "/Users/hanul/Desktop/Screenshot\\ 2026-11-13\\ at\\ 9.41.22\\ PM.png what is this"
        self.assertEqual(local_images.find_paths(prompt),
                         ["/Users/hanul/Desktop/Screenshot 2026-11-13 at 9.41.22 PM.png"])

    def test_a_dropped_screenshot_name_with_a_narrow_no_break_space(self):
        # macOS names screenshots with U+202F before "PM", which is not a token separator.
        prompt = "/Users/hanul/Desktop/Screenshot\\ 2026-11-13\\ at\\ 9.41.22\u202fPM.png what is this"
        self.assertEqual(local_images.find_paths(prompt),
                         ["/Users/hanul/Desktop/Screenshot 2026-11-13 at 9.41.22\u202fPM.png"])

    def test_file_url_is_decoded(self):
        self.assertEqual(local_images.find_paths("file:///Users/hanul/x%20y.png"),
                         ["/Users/hanul/x y.png"])

    def test_home_relative_path(self):
        self.assertEqual(local_images.find_paths("~/Downloads/x.png"), ["~/Downloads/x.png"])

    def test_paths_keep_their_order_and_are_deduplicated(self):
        prompt = "/Users/a/one.png and '/Users/a/one.png' and /Users/a/two.webp"
        self.assertEqual(local_images.find_paths(prompt),
                         ["/Users/a/one.png", "/Users/a/two.webp"])

    def test_a_windows_drop_with_forward_slashes(self):
        # Exactly what dropping refresh-pages.js from a Windows PC typed into the chat.
        prompt = "'c:/Users/hanul/Downloads/refresh-pages.js' please explain"
        self.assertEqual(local_images.find_paths(prompt),
                         ["c:/Users/hanul/Downloads/refresh-pages.js"])

    def test_an_unquoted_windows_path_keeps_its_drive_letter(self):
        prompt = "C:\\Users\\a\\b.js what does this do"
        self.assertEqual(local_images.find_paths(prompt), ["C:/Users/a/b.js"])

    def test_a_windows_file_url(self):
        self.assertEqual(local_images.find_paths("file:///C:/x/y.png"), ["C:/x/y.png"])

    def test_a_non_image_posix_path_is_found(self):
        self.assertEqual(local_images.find_paths("/Users/a/script.js please read"),
                         ["/Users/a/script.js"])

    def test_paths_without_an_extension_are_ignored(self):
        self.assertEqual(local_images.find_paths("/usr/bin and /etc/hosts and ~/Downloads"), [])

    def test_a_windows_path_is_not_dropped_by_the_project_check(self):
        # On Linux `Path("c:/Users/x").resolve()` is relative to the cwd, which is the project.
        self.assertEqual(local_images.find_paths("'C:\\Users\\a\\b.js'", Path.cwd()),
                         ["C:/Users/a/b.js"])

    def test_a_path_that_exists_on_the_server_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            here = Path(tmp) / "here.png"
            here.write_bytes(PNG)
            self.assertEqual(local_images.find_paths(f"'{here}' describe", Path(tmp) / "proj"), [])

    def test_a_path_inside_the_project_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "proj"
            project.mkdir()
            dropped = project / "gone.png"
            self.assertEqual(local_images.find_paths(f"'{dropped}'", project), [])


class FetchTimeoutTest(unittest.TestCase):
    """The single deadline every fetch of one hook run shares."""

    def env(self, value):
        env = {k: v for k, v in os.environ.items() if k != "AAAGENT_FETCH_TIMEOUT"}
        if value is not None:
            env["AAAGENT_FETCH_TIMEOUT"] = value
        return mock.patch.dict(os.environ, env, clear=True)

    def test_a_huge_timeout_is_clamped_below_the_hook_limit(self):
        with self.env("100"):
            self.assertEqual(local_images._fetch_timeout(), 25.0)

    def test_the_default_is_fifteen_seconds(self):
        with self.env(None):
            self.assertEqual(local_images._fetch_timeout(), 15.0)

    def test_a_bad_value_falls_back_to_the_default(self):
        with self.env("soon"):
            self.assertEqual(local_images._fetch_timeout(), 15.0)


class HookTest(unittest.TestCase):
    """The hook as Claude Code runs it: JSON on stdin, JSON on stdout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        (self.project / ".aaagent").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("AAAGENT_VSCODE", "AAAGENT_FETCH_TIMEOUT")}
        env["AAAGENT_PROJECT"] = str(self.project)
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run_hook(self, prompt, **extra):
        payload = json.dumps({"prompt": prompt, "cwd": str(self.project)})
        return subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True,
                              text=True, env=self.env(**extra), timeout=60)

    def context(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        output = data["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        return output["additionalContext"]

    def test_without_the_extension_the_user_is_told_to_attach(self):
        result = self.run_hook("'/Users/nobody/band.jpg' describe attached image")
        context = self.context(result)
        self.assertIn("/Users/nobody/band.jpg", context)
        self.assertIn("on their own computer", context)
        self.assertIn("Attach Image", context)
        self.assertIn("aaagent attach", context)

    def answer_once(self, answer, seen):
        """A fake extension: answer the first fetch request with `answer`."""
        folder = self.project / ".aaagent" / "fetch"

        def extension():
            deadline = time.time() + 10
            request = None
            while time.time() < deadline:
                found = list(folder.glob("*.request.json")) if folder.is_dir() else []
                if found:
                    request = found[0]
                    break
                time.sleep(0.02)
            if request is None:
                seen["error"] = "no request"
                return
            seen["request"] = json.loads(request.read_text())
            result = request.with_name(request.name.replace(".request.json", ".result.json"))
            result.write_text(json.dumps(answer))

        thread = threading.Thread(target=extension)
        thread.start()
        return thread

    def answer_all(self, count, make_answer, seen):
        """A fake extension: answer every fetch request with `make_answer(path)`."""
        folder = self.project / ".aaagent" / "fetch"

        def extension():
            deadline = time.time() + 10
            answered = []
            while time.time() < deadline and len(answered) < count:
                found = sorted(folder.glob("*.request.json")) if folder.is_dir() else []
                for request in found:
                    if request.name in answered:
                        continue
                    try:
                        requested = json.loads(request.read_text())["path"]
                    except (OSError, KeyError, json.JSONDecodeError):
                        continue  # the hook cleaned up an answered one while we scanned
                    seen.setdefault("paths", []).append(requested)
                    answered.append(request.name)
                    result = request.with_name(request.name.replace(".request.json", ".result.json"))
                    try:
                        result.write_text(json.dumps(make_answer(requested)))
                    except OSError:
                        pass
                if len(answered) < count:
                    time.sleep(0.02)
            if len(answered) < count:
                seen["error"] = f"only {len(answered)} of {count} requests"

        thread = threading.Thread(target=extension)
        thread.start()
        return thread

    def test_the_extension_copies_a_windows_drop_of_any_file(self):
        seen = {}
        thread = self.answer_once({"ok": True, "rel": ".aaagent/attachments/x.js"}, seen)
        try:
            result = self.run_hook("'c:/Users/hanul/Downloads/refresh-pages.js' please explain",
                                   AAAGENT_VSCODE=1, AAAGENT_FETCH_TIMEOUT=5)
        finally:
            thread.join()
        context = self.context(result)
        self.assertNotIn("error", seen, seen.get("error"))
        self.assertEqual(seen["request"]["path"], "c:/Users/hanul/Downloads/refresh-pages.js")
        self.assertIn(".aaagent/attachments/x.js", context)
        self.assertIn("Read tool", context)
        self.assertIn("the user's file", context)

    def test_two_silent_fetches_share_one_deadline(self):
        start = time.time()
        result = self.run_hook("'/Users/hanul/one.jpg' and '/Users/hanul/two.png' describe",
                               AAAGENT_VSCODE=1, AAAGENT_FETCH_TIMEOUT="0.5")
        elapsed = time.time() - start
        context = self.context(result)
        self.assertLess(elapsed, 0.9, "the fetches share one deadline instead of one each")
        self.assertIn("/Users/hanul/one.jpg", context)
        self.assertIn("/Users/hanul/two.png", context)
        self.assertEqual(context.count("cannot open it"), 2)
        self.assertEqual(list((self.project / ".aaagent" / "fetch").iterdir()), [],
                         "nothing is left behind after a shared timeout")

    def test_the_extension_answers_every_path_in_prompt_order(self):
        seen = {}
        thread = self.answer_all(
            2, lambda path: {"ok": True, "rel": f".aaagent/attachments/{Path(path).name}"}, seen)
        try:
            result = self.run_hook("'/Users/hanul/one.jpg' and '/Users/hanul/two.js' explain",
                                   AAAGENT_VSCODE=1, AAAGENT_FETCH_TIMEOUT=5)
        finally:
            thread.join()
        context = self.context(result)
        self.assertNotIn("error", seen, seen.get("error"))
        # Requests are written at once, so the fake extension sees them in any order; the
        # paragraphs below still keep the prompt order.
        self.assertEqual(sorted(seen["paths"]),
                         ["/Users/hanul/one.jpg", "/Users/hanul/two.js"])
        self.assertIn(".aaagent/attachments/one.jpg", context)
        self.assertIn(".aaagent/attachments/two.js", context)
        self.assertNotIn("cannot open it", context)
        self.assertLess(context.index("attachments/one.jpg"), context.index("attachments/two.js"),
                        "the paragraphs keep the prompt order")
        self.assertEqual(list((self.project / ".aaagent" / "fetch").iterdir()), [],
                         "both request and result files are cleaned up")

    def test_a_path_that_is_not_a_local_file_is_silent(self):
        seen = {}
        thread = self.answer_once({"ok": False, "missing": True, "error": "no such file"}, seen)
        try:
            result = self.run_hook("'/etc/foo.conf' what is this", AAAGENT_VSCODE=1,
                                   AAAGENT_FETCH_TIMEOUT=5)
        finally:
            thread.join()
        self.assertNotIn("error", seen, seen.get("error"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_without_the_extension_a_non_image_path_is_silent(self):
        result = self.run_hook("'/etc/foo.conf' what is this")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_without_the_extension_a_windows_path_is_reported(self):
        result = self.run_hook("'c:/Users/hanul/Downloads/refresh-pages.js' what is this")
        context = self.context(result)
        self.assertIn("c:/Users/hanul/Downloads/refresh-pages.js", context)
        self.assertIn("the file", context)
        self.assertIn("scp", context)
        self.assertNotIn("Attach Image", context)

    def test_the_extension_copies_the_image(self):
        folder = self.project / ".aaagent" / "fetch"
        seen = {}

        def extension():
            deadline = time.time() + 10
            request = None
            while time.time() < deadline:
                found = list(folder.glob("*.request.json")) if folder.is_dir() else []
                if found:
                    request = found[0]
                    break
                time.sleep(0.02)
            if request is None:
                seen["error"] = "no request"
                return
            seen["request"] = json.loads(request.read_text())
            answer = request.with_name(request.name.replace(".request.json", ".result.json"))
            answer.write_text(json.dumps({"ok": True, "rel": ".aaagent/attachments/x.jpg"}))

        thread = threading.Thread(target=extension)
        thread.start()
        try:
            result = self.run_hook("'/Users/hanul/Downloads/band.jpg' describe",
                                   AAAGENT_VSCODE=1, AAAGENT_FETCH_TIMEOUT=5)
        finally:
            thread.join()
        context = self.context(result)
        self.assertNotIn("error", seen, seen.get("error"))
        self.assertEqual(seen["request"]["path"], "/Users/hanul/Downloads/band.jpg")
        self.assertIn(".aaagent/attachments/x.jpg", context)
        self.assertIn(str(self.project / ".aaagent" / "attachments" / "x.jpg"), context)
        self.assertIn("Read tool", context)
        self.assertEqual(list(folder.iterdir()), [], "the request and the result are cleaned up")

    def test_a_silent_extension_is_reported(self):
        result = self.run_hook("'/Users/hanul/band.jpg'", AAAGENT_VSCODE=1, AAAGENT_FETCH_TIMEOUT="0.3")
        context = self.context(result)
        self.assertIn("cannot open it", context)
        self.assertIn("did not answer", context)
        self.assertEqual(list((self.project / ".aaagent" / "fetch").iterdir()), [],
                         "nothing is left behind after a timeout")

    def test_a_prompt_without_image_paths_prints_nothing(self):
        result = self.run_hook("add a /health endpoint with a test")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_invalid_json_exits_cleanly(self):
        result = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True,
                                text=True, env=self.env(), timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
