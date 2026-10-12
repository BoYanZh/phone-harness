"""Cloud-phone screenshots use a private temp file, not a fixed path."""
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from phone_harness.remote import Remote  # noqa: E402


class RemoteCapture(unittest.TestCase):
    def setUp(self):
        self.remote = Remote("https://example.invalid", "token")
        self.png = b"\x89PNG-test"
        self.remote._op = lambda *args, **kwargs: {
            "png_b64": base64.b64encode(self.png).decode("ascii"),
            "bounds": {"x": 0, "y": 0, "w": 2, "h": 3},
        }
        self.made = []

    def tearDown(self):
        for path in self.made:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_screenshot_uses_mkstemp_not_a_fixed_name(self):
        fixed = Path(tempfile.gettempdir()) / "phone-harness" / "remote.png"
        first, bounds = self.remote._screen_capture()
        second, _ = self.remote._screen_capture()
        self.made.extend((first, second))
        self.assertEqual(bounds, {"x": 0, "y": 0, "w": 2, "h": 3})
        for path in (first, second):
            self.assertTrue(path.endswith(".png"))
            self.assertIn("phone-remote-", Path(path).name)
            self.assertNotEqual(Path(path).resolve(), fixed.resolve())
            self.assertEqual(Path(path).read_bytes(), self.png)
        self.assertNotEqual(first, second)

    def test_an_explicit_path_is_honored(self):
        dest = Path(tempfile.mkdtemp()) / "chosen.png"
        self.addCleanup(lambda: (dest.unlink(missing_ok=True), dest.parent.rmdir()))
        path, _ = self.remote._screen_capture(path=dest)
        self.assertEqual(Path(path), dest)
        self.assertEqual(dest.read_bytes(), self.png)
