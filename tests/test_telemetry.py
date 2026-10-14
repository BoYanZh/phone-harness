"""CI runs send no telemetry. An ordinary run still does.

    python -m unittest tests.test_telemetry
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class CiTelemetry(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"PHONE_HARNESS_HOME": home.name}, clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop("PHONE_HARNESS_TELEMETRY", None)
        # The suite itself runs with CI=true. Clear it here so a case that
        # expects a send is not turned into a no-op by the runner.
        os.environ.pop("CI", None)

    def test_ci_true_does_not_send_or_write_an_install_id(self):
        from phone_harness.config import paths
        from phone_harness.telemetry import capture_cli_event, is_enabled

        os.environ["CI"] = "true"
        os.environ["PHONE_HARNESS_TELEMETRY"] = "1"
        self.assertFalse(is_enabled())
        with mock.patch("phone_harness.telemetry._send_detached") as send:
            capture_cli_event(action="completed", command="version")
        send.assert_not_called()
        self.assertFalse(paths()["telemetry"].exists())

    def test_ci_unset_still_sends(self):
        from phone_harness.config import paths
        from phone_harness.telemetry import capture_cli_event, is_enabled

        self.assertNotIn("CI", os.environ)
        self.assertTrue(is_enabled())
        with mock.patch("phone_harness.telemetry._send_detached") as send:
            capture_cli_event(action="completed", command="version")
        send.assert_called_once()
        payload = send.call_args.args[0]
        self.assertEqual(payload["event"], "cli_event")
        self.assertTrue(payload["distinct_id"])
        self.assertTrue(paths()["telemetry"].is_file())

    def test_empty_zero_and_false_are_not_ci(self):
        from phone_harness.telemetry import capture_cli_event, is_enabled

        for value in ("", "0", "false", "False"):
            os.environ["CI"] = value
            self.assertTrue(is_enabled(), value)
            with mock.patch("phone_harness.telemetry._send_detached") as send:
                capture_cli_event(action="completed", command="version")
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0]["event"], "cli_event")
