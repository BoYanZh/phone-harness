"""Waking a cloud iPhone that is asleep or locked (PHO-335).

A cloud iPhone left alone goes to sleep, and its host then refuses to touch
it. The host's own sentence says to unlock it on the phone, which nobody
driving a cloud phone can do. Where the host can wake the phone, the helpers
say to call wake() instead. The control host here is a stand-in; no cloud.

The stand-in answers as the control URL does in production: phone-cloud's
gateway forwards the op to the host and passes back the host's sentence
without its `code` (iphone_gate._interpret). `Control.code` set true is the
host answering directly, code and all.
"""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from phone_harness import transport  # noqa: E402
from phone_harness.remote import LOCKED, Remote  # noqa: E402
from phone_harness.transport import Unsupported  # noqa: E402

# What phone-host answers while an iPhone shows its lock screen, and StandBy.
HOST_LOCKED = "The iPhone is locked. Unlock it on the phone, then retry; the host never enters a passcode."
HOST_STANDBY = "The iPhone is in StandBy, which takes no input. Call device.wake (Wake in the preview), then retry."
GATED = ("input.tap", "input.text", "apps.launch", "url.open", "session.require")


class Control(BaseHTTPRequestHandler):
    """A phone's control URL: /ops and /op, as phone-host serves them."""
    ops = ()            # what GET /ops lists
    locked = False      # the phone shows its lock screen
    standby = False     # the phone shows StandBy
    passcode = False    # a passcode was set, so a wake leaves it locked
    code = False        # True: the host's `code` reaches the client; False: the gateway dropped it
    calls = []          # every body POSTed to /op
    gets = 0            # how many times /ops was read

    def log_message(self, *a):
        pass

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else {}
        if self.headers.get("Authorization") != "Bearer ct_1":
            return self._send(404, {"error": "not found"})
        if self.path == "/ops":
            Control.gets += 1
            return self._send(200, {"ops": sorted(Control.ops)})
        if self.path == "/op":
            Control.calls.append(body)
            op = body["op"]
            if op not in Control.ops:
                return self._send(400, {"error": f"this phone cannot {op!r}", "unsupported": True})
            state = "locked" if Control.locked else "standby" if Control.standby else "ready"
            if op == "session.state":
                return self._send(200, {"result": state})
            if op == "device.wake":
                # phone-host's answer: where it left the phone, where it found it, and whether it pressed anything.
                Control.standby = False
                if not Control.passcode:
                    Control.locked = False
                woke = state != "ready"
                return self._send(200, {"result": {"state": "locked" if Control.locked else "ready", "was": state,
                                                   "woke": woke, "through": "home" if woke else None}})
            if state != "ready" and op in GATED:
                refusal = {"error": HOST_LOCKED if Control.locked else HOST_STANDBY}
                if Control.code:
                    refusal["code"] = "locked"
                return self._send(409, refusal)
            if op == "input.busy":
                return self._send(409, {"error": "the phone is busy with another operation"})
            return self._send(200, {"result": True})
        return self._send(404, {"error": "not found"})

    do_GET = do_POST = _handle


class WakingACloudIphone(unittest.TestCase):
    NEW = ("input.tap", "input.text", "apps.launch", "url.open", "session.require", "session.state", "nav.home",
           "device.wake", "input.busy")
    OLD = tuple(op for op in NEW if op != "device.wake")

    def setUp(self):
        Control.ops, Control.locked, Control.standby, Control.passcode = self.NEW, False, False, False
        Control.code, Control.calls, Control.gets = False, [], 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), Control)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.remote = Remote(f"http://127.0.0.1:{server.server_port}", "ct_1")

    def test_the_vocabulary_has_the_op_and_a_host_that_lists_it_supports_it(self):
        self.assertIn("device.wake", transport.OP_NAMES)
        self.assertTrue(self.remote.supports("device.wake"))
        self.assertIn("device.wake", self.remote.ops())

    def test_wake_is_one_op_with_no_arguments_and_returns_what_the_host_says(self):
        Control.locked = True
        self.assertEqual(self.remote.send("device.wake"),
                         {"state": "ready", "was": "locked", "woke": True, "through": "home"})
        self.assertEqual(Control.calls, [{"op": "device.wake", "kw": {}}])
        self.assertFalse(Control.locked)

    def test_waking_a_phone_that_is_awake_says_nothing_was_pressed(self):
        self.assertEqual(self.remote.send("device.wake"),
                         {"state": "ready", "was": "ready", "woke": False, "through": None})

    def test_a_locked_phone_says_to_call_wake_not_to_unlock_it_on_the_phone(self):
        Control.locked = True
        for op, kw in (("input.tap", {"x": 1, "y": 2}), ("input.text", {"s": "hi"}),
                       ("apps.launch", {"name": "Notes"}), ("url.open", {"url": "https://example.com"}),
                       ("session.require", {})):
            with self.subTest(op=op), self.assertRaises(RuntimeError) as raised:
                self.remote.send(op, **kw)
            self.assertEqual(str(raised.exception), LOCKED)
            self.assertNotIsInstance(raised.exception, Unsupported)
        self.assertIn("Call wake()", LOCKED)
        self.assertIn("no passcode", LOCKED)
        self.assertNotIn("Unlock it on the phone", LOCKED)

    def test_standby_is_refused_the_same_way(self):
        Control.standby = True
        with self.assertRaises(RuntimeError) as raised:
            self.remote.send("apps.launch", name="Notes")
        self.assertEqual(str(raised.exception), LOCKED)

    def test_the_refusal_is_settled_by_asking_the_phone_its_state_once(self):
        """The gateway passes on the host's sentence without its code, so the phone is asked."""
        Control.locked = True
        with self.assertRaises(RuntimeError):
            self.remote.send("input.tap", x=1, y=2)
        self.assertEqual([c["op"] for c in Control.calls], ["input.tap", "session.state"])

    def test_the_hosts_own_code_settles_it_without_asking(self):
        Control.locked = Control.code = True
        with self.assertRaises(RuntimeError) as raised:
            self.remote.send("input.tap", x=1, y=2)
        self.assertEqual(str(raised.exception), LOCKED)
        self.assertEqual([c["op"] for c in Control.calls], ["input.tap"])

    def test_locked_then_woken_then_the_tap_lands(self):
        Control.locked = True
        with self.assertRaises(RuntimeError):
            self.remote.send("input.tap", x=1, y=2)
        self.assertEqual(self.remote.send("device.wake")["state"], "ready")
        self.assertIs(self.remote.send("input.tap", x=1, y=2), True)

    def test_a_passcode_someone_set_leaves_it_locked_and_nothing_is_typed(self):
        Control.locked = Control.passcode = True
        self.assertEqual(self.remote.send("device.wake")["state"], "locked")
        self.assertEqual([c["op"] for c in Control.calls], ["device.wake"])
        with self.assertRaises(RuntimeError) as raised:        # and the next tap says what it always says
            self.remote.send("input.tap", x=1, y=2)
        self.assertEqual(str(raised.exception), LOCKED)

    def test_a_host_that_cannot_wake_keeps_its_own_sentence(self):
        """Telling someone to call a helper their phone's host does not have would send them in a circle."""
        Control.ops, Control.locked = self.OLD, True
        self.assertFalse(self.remote.supports("device.wake"))
        with self.assertRaises(RuntimeError) as raised:
            self.remote.send("input.tap", x=1, y=2)
        self.assertEqual(str(raised.exception), HOST_LOCKED)
        with self.assertRaises(Unsupported) as raised:
            self.remote.send("device.wake")
        self.assertEqual(str(raised.exception), "this cloud phone cannot 'device.wake'")
        self.assertEqual([c["op"] for c in Control.calls], ["input.tap"])       # the wake was never sent

    def test_another_409_is_not_taken_for_a_locked_phone(self):
        with self.assertRaises(RuntimeError) as raised:
            self.remote.send("input.busy")
        self.assertEqual(str(raised.exception), "the phone is busy with another operation")
        self.assertEqual([c["op"] for c in Control.calls], ["input.busy", "session.state"])   # asked, and it was ready

    def test_a_host_that_cannot_wake_is_asked_nothing_more(self):
        Control.ops, Control.locked = self.OLD, True
        with self.assertRaises(RuntimeError):
            self.remote.send("input.tap", x=1, y=2)
        self.assertEqual([c["op"] for c in Control.calls], ["input.tap"])
        self.assertEqual(Control.gets, 1)           # the one read of /ops that every first op makes

    def test_a_state_that_cannot_be_read_leaves_the_hosts_sentence(self):
        Control.ops = tuple(op for op in self.NEW if op != "session.state")
        Control.locked = True
        with self.assertRaises(RuntimeError) as raised:
            self.remote.send("input.tap", x=1, y=2)
        self.assertEqual(str(raised.exception), HOST_LOCKED)


class TheWakeHelper(unittest.TestCase):
    class Phone:
        def __init__(self, ops):
            self.ops, self.sent = set(ops), []

        def supports(self, op):
            return op in self.ops

        def send(self, op, **kw):
            self.sent.append((op, kw))
            return {"state": "ready", "was": "standby", "woke": True, "through": "app"}

    def with_phone(self, ops):
        from phone_harness import helpers
        phone = self.Phone(ops)
        saved = helpers.phone
        helpers.phone = phone
        self.addCleanup(setattr, helpers, "phone", saved)
        return helpers, phone

    def test_wake_sends_the_op_and_returns_what_the_host_says(self):
        helpers, phone = self.with_phone({"device.wake"})
        self.assertEqual(helpers.wake(), {"state": "ready", "was": "standby", "woke": True, "through": "app"})
        self.assertEqual(phone.sent, [("device.wake", {})])

    def test_a_phone_with_no_such_control_says_cloud_iphones_only_and_sends_nothing(self):
        helpers, phone = self.with_phone({"nav.home", "input.tap"})
        with self.assertRaises(Unsupported) as raised:
            helpers.wake()
        self.assertIn("cloud iPhones only", str(raised.exception))
        self.assertEqual(phone.sent, [])

    def test_no_local_backend_claims_to_wake_a_phone(self):
        """A phone on the desk is unlocked by its owner; the harness never types a PIN."""
        from phone_harness.android import Android
        self.assertIsNone(getattr(Android, "_device_wake", None))
        if sys.platform == "darwin":
            try:
                from phone_harness.ios import IPhone
            except ImportError:
                return
            self.assertIsNone(getattr(IPhone, "_device_wake", None))


if __name__ == "__main__":
    unittest.main()
