"""`phone-harness skill` is printed on Windows CI, where stdout is cp1252."""
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from phone_harness.run import _skill_text, _write_utf8  # noqa: E402
from phone_harness.android import CLI_USAGE as ANDROID_USAGE  # noqa: E402
from phone_harness.cloud import CLI_USAGE as CLOUD_USAGE  # noqa: E402
from phone_harness.cloud import IPHONES_ONLY  # noqa: E402
from phone_harness.config import CLI_USAGE as CONFIG_USAGE
from phone_harness.run import USAGE  # noqa: E402


class SkillOutput(unittest.TestCase):
    def test_printed_docs_and_help_are_ascii(self):
        for text in (_skill_text(), USAGE, ANDROID_USAGE, CLOUD_USAGE, CONFIG_USAGE):
            text.encode("ascii")

    def test_skill_command_survives_cp1252_stdout(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "cp1252:strict"
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        env["PHONE_HARNESS_TELEMETRY"] = "0"
        env.pop("BROWSER", None)
        r = subprocess.run(
            [sys.executable, "-m", "phone_harness.run", "skill"],
            capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"phone-harness", r.stdout)
        r.stdout.decode("ascii")

    def test_write_utf8_uses_the_buffer_when_the_text_stream_refuses(self):
        class Stdout:
            encoding = "cp1252"

            def __init__(self):
                self.buffer = io.BytesIO()

            def reconfigure(self, **_kw):
                raise OSError("cannot reconfigure")

            def write(self, s):
                s.encode(self.encoding)

            def flush(self):
                pass

        out = Stdout()
        old = sys.stdout
        sys.stdout = out
        try:
            _write_utf8("a \u2192 b")
        finally:
            sys.stdout = old
        self.assertEqual(out.buffer.getvalue(), "a \u2192 b".encode("utf-8"))

    def test_skill_points_at_helpers_and_names_screen(self):
        text = _skill_text()
        self.assertIn("The main helpers are listed in [Helpers](#helpers).", text)
        self.assertIn("\n## Helpers\n", text)
        self.assertIn("`screen(min_confidence=0.3)`", text)
        self.assertIn("`screenshot(path=None)` - Capture the screen to a PNG, print its path, and return it.", text)
        self.assertIn("`screen_info()`", text)
        self.assertNotIn("ocr()", text)

    def test_skill_says_how_reserving_a_phone_works(self):
        """What an agent has to get right before it reserves or cancels for someone."""
        section = _skill_text().split("\n## Reserving a cloud phone\n", 1)[1].split("\n## ", 1)[0]
        text = " ".join(section.split())
        for rule in (
                "**Which phones.** " + IPHONES_ONLY,             # the sentence `cloud reserve` refuses an Android with
                "`--type` is `iphone`, or `iphone-esim` for one with an eSIM",
                "**Signed in first.**", "phone-harness cloud login", "PHONE_HARNESS_API_KEY",
                "phone-harness.com/get-a-phone",                       # with no account: the website
                "$20 per phone refundable deposit", "quote the command prints is authoritative",
                "do not quote them from memory",
                "**You never pay.**", "**Nothing is held until the deposit is paid.**",
                "**Queue order is paid time.**", "does not move one up",
                "one phone at the intro price when this customer has not had it yet, the rest at the standard price",
                "The refund is automatic, to the card that paid",
                "part handed over refunds only the phones still waiting",
                "became a credit on the first bill and is no longer refundable",
                "**The website has no list.**", "`cloud reservations` is the list",
                "Cancelling one does not free a place"):
            self.assertIn(rule, text)
        self.assertNotIn("`android`", text)                # not a --type

    def test_screenshot_prints_the_path_it_returns(self):
        import contextlib

        from phone_harness import helpers

        path = "/tmp/phone-harness-example.png"
        saved = helpers.send
        helpers.send = lambda op, **kw: (path, {"w": 1})
        self.addCleanup(setattr, helpers, "send", saved)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            returned = helpers.screenshot()
        self.assertEqual(returned, path)
        self.assertEqual(buf.getvalue().strip(), path)

    def test_screen_is_the_name_of_ocr(self):
        from phone_harness.helpers import ocr, screen
        self.assertIs(ocr, screen)
        self.assertEqual(screen.__defaults__, (0.3,))
