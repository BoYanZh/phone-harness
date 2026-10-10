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
from phone_harness.config import CLI_USAGE as CONFIG_USAGE  # noqa: E402
from phone_harness.run import USAGE  # noqa: E402


class SkillOutput(unittest.TestCase):
    def test_printed_docs_and_help_are_ascii(self):
        for text in (_skill_text(), USAGE, ANDROID_USAGE, CLOUD_USAGE, CONFIG_USAGE):
            text.encode("ascii")

    def test_skill_command_survives_cp1252_stdout(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "cp1252:strict"
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
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
