"""CLI help, bad flags, column layout, and optional color. No phone, no network."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from phone_harness import android, cloud, term  # noqa: E402
from phone_harness.run import USAGE  # noqa: E402


def _run(*args, stdin=None, env=None):
    merged = os.environ.copy()
    merged["PYTHONPATH"] = SRC + os.pathsep + merged.get("PYTHONPATH", "")
    merged["PHONE_HARNESS_TELEMETRY"] = "0"
    merged.pop("NO_COLOR", None)
    if env:
        merged.update(env)
    return subprocess.run([sys.executable, "-m", "phone_harness.run", *args],
                          input=stdin, capture_output=True, text=True, env=merged)


class Tty(io.StringIO):
    def isatty(self):
        return True


class HelpAndFlags(unittest.TestCase):
    def test_group_help_matches_the_usage_text_and_exits_cleanly(self):
        top = _run("--help")
        self.assertEqual(top.returncode, 0, top.stderr)
        self.assertIn("Commands:", top.stdout)
        self.assertIn("phone-harness cloud", top.stdout)

        for args, needle in ((["cloud", "--help"], "cloud ls"),
                             (["cloud", "-h"], "cloud start"),
                             (["android", "--help"], "android pair"),
                             (["android", "-h"], "android awake")):
            got = _run(*args)
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertIn(needle, got.stdout)
            got.stdout.encode("ascii")

        unknown = _run("cloud", "nope")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("cloud ls", unknown.stdout)
        self.assertEqual(unknown.stderr, "")

        bogus = _run("--bogus")
        self.assertNotEqual(bogus.returncode, 0)
        self.assertIn("Commands:", bogus.stderr)

    def test_every_subcommand_has_help_and_rejects_a_bad_flag(self):
        cloud_cmds = {
            "login": "--no-browser",
            "logout": None,
            "whoami": "--json",
            "start": "--for",
            "stop": "--all",
            "ls": "--json",
            "show": "--json",
            "use": "SID",
            "watch": "--print",
            "open": "--print",
            "phone": "--yes",
            "keys": "--json",
            "history": "--limit",
        }
        for name, flag in cloud_cmds.items():
            help_out = _run("cloud", name, "--help")
            self.assertEqual(help_out.returncode, 0, help_out.stderr)
            self.assertIn(f"phone-harness cloud {name}", help_out.stdout)
            self.assertIn("--session SID", help_out.stdout)
            help_out.stdout.encode("ascii")
            if flag and flag.startswith("-"):
                self.assertIn(flag, help_out.stdout, name)
            bad = _run("cloud", name, "--no-such-flag")
            self.assertEqual(bad.returncode, 2, name)
            self.assertIn("unrecognized arguments: --no-such-flag", bad.stderr)
            self.assertIn(f"phone-harness cloud {name}", bad.stderr)

        for name, flag in (("awake", "--bg"), ("rest", None), ("pair", None),
                           ("connect", None), ("use", None), ("forget", None)):
            help_out = _run("android", name, "--help")
            self.assertEqual(help_out.returncode, 0, help_out.stderr)
            self.assertIn(f"phone-harness android {name}", help_out.stdout)
            help_out.stdout.encode("ascii")
            if flag:
                self.assertIn(flag, help_out.stdout)
            bad = _run("android", name, "--no-such-flag")
            self.assertEqual(bad.returncode, 2, name)
            self.assertIn("unrecognized arguments: --no-such-flag", bad.stderr)

        awake_help = _run("android", "awake", "--help")
        self.assertIn("--no-mirror", awake_help.stdout)
        self.assertNotIn("--fg", awake_help.stdout)
        parsed, leftover = android._android_parsers()["awake"].parse_known_args(["--fg", "--bg"])
        self.assertEqual(leftover, [])
        self.assertTrue(parsed.fg and parsed.bg)

    def test_removed_flags_aliases_and_session_keep_their_messages(self):
        removed = _run("cloud", "start", "--iphone")
        self.assertEqual(removed.returncode, 1)
        self.assertIn("--iphone was removed", removed.stderr)
        self.assertNotIn("unrecognized", removed.stderr)
        self.assertNotIn("\x1b", removed.stderr)

        both = _run("cloud", "start", "--minutes", "5", "--for", "90s")
        self.assertEqual(both.returncode, 1)
        self.assertIn("not both", both.stderr)

        use = _run("cloud", "use")
        self.assertEqual(use.returncode, 1)
        self.assertIn("Usage: phone-harness cloud use SID", use.stderr)

        missing = _run("--session")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("--session needs a SID", missing.stderr)

        os.environ.pop("PHONE_HARNESS_SESSION", None)
        err = io.StringIO()
        try:
            with _stderr(err):
                code = cloud.cli(["--session"])
            self.assertEqual(code, 1)
            self.assertIn("--session needs a value", err.getvalue())

            err = io.StringIO()
            with _stderr(err):
                code = cloud.cli(["--session", "abc123", "use"])
            self.assertEqual(code, 1)
            self.assertEqual(os.environ.get("PHONE_HARNESS_SESSION"), "abc123")
            self.assertIn("Usage: phone-harness cloud use SID", err.getvalue())
        finally:
            os.environ.pop("PHONE_HARNESS_SESSION", None)

        pinned_help = _run("--session", "abc123", "cloud", "ls", "--help")
        self.assertEqual(pinned_help.returncode, 0, pinned_help.stderr)
        self.assertIn("phone-harness cloud ls", pinned_help.stdout)

        android_help = _run("android", "awake", "--session", "abc123", "--help")
        self.assertEqual(android_help.returncode, 0, android_help.stderr)
        self.assertIn("--bg", android_help.stdout)

        no_primary = _run("android", "connect")
        self.assertEqual(no_primary.returncode, 1)
        self.assertIn("no primary phone yet", no_primary.stdout)
        self.assertNotIn("\x1b", no_primary.stdout)

        start_help = _run("cloud", "start", "--help")
        self.assertIn("-m", start_help.stdout)
        self.assertIn("--minutes", start_help.stdout)
        self.assertNotIn("--iphone", start_help.stdout)
        history_help = _run("cloud", "history", "--help")
        self.assertIn("-n", history_help.stdout)

    def test_history_limit_flags_still_reach_the_command(self):
        with mock.patch.object(cloud, "_api", return_value={"items": []}) as api, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cloud.cli(["history", "-n", "3"]), 0)
            self.assertEqual(cloud.cli(["history", "--limit", "4"]), 0)
        self.assertIn("limit=3", api.call_args_list[0].args[1])
        self.assertIn("limit=4", api.call_args_list[1].args[1])

    def test_a_script_on_stdin_is_still_a_script(self):
        home = Path(self.id().replace(".", "_"))
        # Keep the script off the checkout's workspace and the real home.
        env = {"PHONE_HARNESS_HOME": str(Path("/tmp") / home),
               "PH_AGENT_WORKSPACE": str(Path("/tmp") / home / "ws"),
               "PHONE_HARNESS_PLATFORM": "android"}
        got = _run(stdin="print(6 * 7)\n", env=env)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("42", got.stdout)
        self.assertNotIn("unrecognized arguments", got.stderr)


class _stderr:
    """Patch sys.stderr for one cloud.cli call."""

    def __init__(self, buf):
        self.buf = buf
        self.old = None

    def __enter__(self):
        self.old = sys.stderr
        sys.stderr = self.buf
        return self.buf

    def __exit__(self, *exc):
        sys.stderr = self.old


class Columns(unittest.TestCase):
    def test_columns_grow_to_fit_and_stay_aligned(self):
        long_id = "prof-948b7471ed7575fc17236906"
        long_sid = "sess-948b7471ed7575fc17236906"
        rows = [
            {"targeted": True, "label": "Saved phone", "platform": "android",
             "id": long_id, "state": "stored", "temporary": False,
             "session": {"id": long_sid, "left": "14 min"}},
            {"targeted": False, "label": "Temporary", "platform": "android",
             "id": None, "state": "-", "temporary": True, "session": {}},
        ]
        header, saved, temporary = cloud._format_ls(rows).splitlines()
        for line in (header, saved, temporary):
            self.assertEqual(line, line.rstrip())
        id_at = header.index("ID")
        state_at = header.index("STATE")
        note_at = header.index("NOTE")
        session_at = header.index("SESSION")
        left_at = header.index("LEFT")
        self.assertEqual(saved.index(long_id), id_at)
        self.assertEqual(state_at, id_at + len(long_id) + 1)
        self.assertTrue(saved[state_at:].startswith("stored"))
        self.assertEqual(saved.index(long_sid), session_at)
        self.assertEqual(left_at, session_at + len(long_sid) + 1)
        self.assertTrue(saved[left_at:].startswith("14 min"))
        self.assertTrue(saved.startswith("*"))
        self.assertTrue(temporary.startswith(" "))
        self.assertEqual(temporary.index("android"), header.index("PLATFORM"))
        self.assertEqual(temporary.index("temporary"), note_at)
        # A short id still sits under ID, padded out to the header when nothing is longer.
        short = cloud._format_ls([{
            "label": "A", "platform": "ios", "id": "iph", "state": "up",
            "temporary": False, "session": {"id": "s", "left": "1s"},
        }]).splitlines()
        self.assertEqual(short[1].index("iph"), short[0].index("ID"))
        self.assertEqual(short[0].index("STATE"), short[0].index("ID") + max(len("ID"), len("iph")) + 1)


class Color(unittest.TestCase):
    def setUp(self):
        self._no_color = os.environ.pop("NO_COLOR", None)

    def tearDown(self):
        if self._no_color is not None:
            os.environ["NO_COLOR"] = self._no_color
        else:
            os.environ.pop("NO_COLOR", None)

    def test_color_follows_the_stream_and_no_color(self):
        tty = Tty()
        plain = io.StringIO()
        self.assertEqual(term.paint("✓", "ok", tty), "\033[32m✓\033[0m")
        self.assertEqual(term.paint("nope", "err", tty), "\033[31mnope\033[0m")
        self.assertEqual(term.paint("aside", "note", tty), "\033[2maside\033[0m")
        self.assertEqual(term.paint("✓", "ok", plain), "✓")
        self.assertFalse(plain.isatty() if hasattr(plain, "isatty") else False)

        os.environ["NO_COLOR"] = ""
        self.assertEqual(term.paint("✓", "ok", tty), "✓")
        self.assertEqual(term.err("boom", tty), "boom")
        os.environ["NO_COLOR"] = "1"
        self.assertEqual(term.note("aside", tty), "aside")

    def test_a_terminal_colors_the_check_and_the_error(self):
        os.environ.pop("NO_COLOR", None)
        out, err = Tty(), Tty()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        home = tempfile.mkdtemp()
        try:
            with mock.patch.dict(os.environ, {"PHONE_HARNESS_HOME": home}, clear=False):
                os.environ.pop("PHONE_HARNESS_API_KEY", None)
                code = cloud.cli([])
                record = {"sid": "sid001", "label": None, "profile": None,
                          "link": {"kind": "adb", "host": "h", "port": 1, "code": "c"}}
                session = {"id": "sid001", "profile": None, "platform": "android",
                           "expires_at": time.time() + 900, "watch_url": None,
                           "startup": {"startup": "exact"}}
                with mock.patch.object(cloud, "_attach", return_value=record):
                    cloud._report(session, watch=False)
                removed = cloud.cli(["start", "--device"])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self.assertEqual(code, 1)
        self.assertEqual(removed, 1)
        text, errors = out.getvalue(), err.getvalue()
        self.assertIn("\033[31mNot signed in", text)
        self.assertIn("\033[0m", text)
        self.assertIn("\033[2m", text)
        self.assertIn("\033[32m✓\033[0m ready.", text)
        self.assertIn("\033[2m Resumed exactly where you left it.\033[0m", text)
        self.assertIn("\033[31m", errors)
        self.assertIn("--device was removed", errors)

        os.environ["NO_COLOR"] = "1"
        out, err = Tty(), Tty()
        sys.stdout, sys.stderr = out, err
        try:
            code = cloud.cli(["start", "--temp"])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self.assertEqual(code, 1)
        self.assertNotIn("\x1b", err.getvalue())
        self.assertIn("--temp was removed", err.getvalue())


class SessionAndStdinGuard(unittest.TestCase):
    """The top-level script path and --session live in run.py; argparse must not take them over."""

    def test_usage_text_stays_the_top_level_help(self):
        self.assertIn("<<'PY'", USAGE)
        self.assertNotIn("argparse", USAGE)


if __name__ == "__main__":
    unittest.main()
