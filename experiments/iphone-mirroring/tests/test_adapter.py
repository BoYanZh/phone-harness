"""Check opt-in routing/restoration with fake Apple APIs; never drive a phone."""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ADAPTER = Path(__file__).resolve().parents[1] / "agent_helpers.py"


class AdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {"PH_MIRRORING_OUTPUT": temp.name})
        env.start()
        self.addCleanup(env.stop)

        self.front = Mock(return_value=0)
        self.sky = types.SimpleNamespace(
            _SLPSSetFrontProcessWithOptions=self.front,
            SLPSPostEventRecordTo=Mock(return_value=0),
        )
        self.background = types.ModuleType("phone_harness.background")
        self.background._sky = self.sky
        self.background.press = Mock()
        self.background.scroll_wheel = Mock()
        self.background.ensure_window = Mock(
            return_value={"x": 100, "y": 200, "w": 300, "h": 600})
        self.phone = types.SimpleNamespace(
            name="iphone-mirroring", send=Mock(), supports=Mock(return_value=True),
            _raise_if_not_live=Mock(),
        )
        helpers = types.ModuleType("phone_harness.helpers")
        helpers.phone = self.phone
        package = types.ModuleType("phone_harness")
        package.helpers = helpers
        package.background = self.background
        transport = types.ModuleType("phone_harness.transport")
        transport.Unsupported = type("Unsupported", (RuntimeError,), {})
        self.unsupported = transport.Unsupported
        foundation = types.ModuleType("Foundation")
        foundation.NSRunLoop = Mock()
        foundation.NSDate = Mock()
        quartz = types.ModuleType("Quartz")
        quartz.CGEventPostToPid = Mock()
        quartz.CGEventGetType = Mock(return_value=10)
        modules = patch.dict(sys.modules, {
            "phone_harness": package,
            "phone_harness.helpers": helpers,
            "phone_harness.background": self.background,
            "phone_harness.transport": transport,
            "Foundation": foundation,
            "Quartz": quartz,
        })
        modules.start()
        self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location("experiment_adapter", ADAPTER)
        self.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.adapter)
        self.adapter._refresh_after_child = Mock()
        process = patch.object(self.adapter._subprocess, "run", return_value=
                               types.SimpleNamespace(returncode=0, stderr="",
                                                     stdout='{"submitted":true}'))
        self.child = process.start()
        self.addCleanup(process.stop)

    def test_nested_context_skips_preparation_and_restores_after_exception(self):
        original = self.sky._SLPSSetFrontProcessWithOptions
        with self.assertRaisesRegex(RuntimeError, "script failed"):
            with self.adapter.no_front():
                with self.adapter.no_front():
                    self.assertEqual(self.sky._SLPSSetFrontProcessWithOptions(None, 1, 512), 0)
                self.assertTrue(self.adapter.no_front_active())
                self.sky._SLPSSetFrontProcessWithOptions(None, 1, 512)
                raise RuntimeError("script failed")
        self.front.assert_not_called()
        self.assertIs(self.sky._SLPSSetFrontProcessWithOptions, original)
        self.assertFalse(self.adapter.no_front_active())
        original(None, 1, 512)
        self.front.assert_called_once_with(None, 1, 512)

    def test_context_restores_even_when_enter_marker_cannot_be_written(self):
        original = self.sky._SLPSSetFrontProcessWithOptions
        with patch.object(self.adapter, "_emit_native_call", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                with self.adapter.no_front():
                    self.fail("enter must fail")
        self.assertIs(self.sky._SLPSSetFrontProcessWithOptions, original)
        self.assertFalse(self.adapter.no_front_active())

    def test_native_tap_keeps_encoding_and_opts_in_only_inside_context(self):
        self.phone.mirror.tap(250, 500)
        baseline = self.child.call_args.args[0]
        self.assertIn("--minimal-record", baseline)
        self.assertNotIn("--no-front", baseline)
        with self.adapter.no_front():
            self.phone.mirror.tap(250, 500)
        opted_in = self.child.call_args.args[0]
        self.assertEqual([v for v in opted_in if v != "--no-front"], baseline)
        self.assertEqual(self.phone._raise_if_not_live.call_count, 4)

    def test_raw_navigation_inherits_context_but_info_takes_no_extra_flags(self):
        with self.adapter.no_front():
            self.adapter.native_raw("home", "--pid-route")
            self.assertIn("--no-front", self.child.call_args.args[0])
            self.adapter.native_raw("home", "--pid-route", "--no-front")
            self.assertEqual(self.child.call_args.args[0].count("--no-front"), 1)
            self.adapter.native_raw("info", send=False)
            self.assertEqual(self.child.call_args.args[0][1:], ["info"])

    def test_home_uses_the_in_process_keyboard_path_only_in_context(self):
        self.phone.mirror.press("cmd+1")
        self.child.assert_called_once()
        self.background.press.assert_not_called()
        self.child.reset_mock()
        with self.adapter.no_front():
            self.phone.mirror.press("cmd+1")
        self.child.assert_not_called()
        self.background.press.assert_called_once_with("cmd+1")

    def test_blocked_session_prevents_native_dispatch(self):
        self.phone._raise_if_not_live.side_effect = RuntimeError("phone in use")
        with self.adapter.no_front():
            with self.assertRaisesRegex(RuntimeError, "phone in use"):
                self.phone.mirror.tap(250, 500)
        self.child.assert_not_called()
        self.front.assert_not_called()

    def test_scroll_never_falls_back_to_the_cursor_moving_implementation(self):
        self.assertFalse(self.phone.supports("input.scroll"))
        self.assertTrue(self.phone.supports("input.tap"))
        with self.assertRaises(self.unsupported):
            self.phone.mirror.scroll_wheel(100, 250, 500)
        with self.adapter.no_front():
            with self.assertRaises(self.unsupported):
                self.phone.mirror.scroll_wheel(100, 250, 500)
        self.background.scroll_wheel.assert_not_called()
        self.child.assert_not_called()


if __name__ == "__main__":
    unittest.main()
