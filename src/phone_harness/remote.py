"""A cloud phone driven by ops over HTTPS, not by ADB.

Some cloud phones have no shell to connect to: a real iPhone behind a Mac is
one. Its session carries a `control` grant instead of an `adb` block: a URL
and a token. Every op in the vocabulary becomes one POST to `<url>/op`, and
the answer is the op's value. Screenshots arrive as PNG bytes and are written
where a local backend would have written them, so helpers.py never learns
where the phone is.

What the far side cannot do comes back as `unsupported`, which is raised as
Unsupported here, exactly as a local backend would.
"""
import base64
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import telemetry
from .transport import Backend, Unsupported

USER_AGENT = "phone-harness-cli"

# The host refuses, with 409, to touch an iPhone that shows its lock screen or
# StandBy. Its own sentence says to unlock it on the phone, which nobody
# driving a cloud phone can do. Where the host can wake the phone, the caller
# is told that instead. The host marks the refusal with a code, but the
# gateway in front of the control URL passes on only the sentence, so the
# phone is asked what state it is in (see Remote._locked).
LOCKED_CODE = "locked"
WAKEABLE = ("locked", "standby")        # the states `session.state` gives such a phone
LOCKED = ("the cloud iPhone is locked or asleep. Call wake() and run this step again. It has no passcode, "
          "so there is nothing to type and nothing to ask the user for")


def _cloud_phone_error():
    from .cloud import CloudPhoneError
    return CloudPhoneError


def _runtime_prefixes():
    """Type prefixes a host may put on a RuntimeError-family message.

    CloudPhoneError is a RuntimeError. The traceback printer qualifies it
    (`phone_harness.cloud.CloudPhoneError:`), and a host may also send the
    bare class name or `RuntimeError:`.
    """
    cloud = _cloud_phone_error()
    qualified = f"{cloud.__module__}.{cloud.__qualname__}: "
    return (qualified, "CloudPhoneError: ", "RuntimeError: ")


def _plain_error(message, exc_type):
    """Drop a leading `TypeName:` that repeats the exception about to be raised.

    Raising RuntimeError while a CloudPhoneError prefix is still on the text
    (or the reverse) prints both types.
    """
    if not isinstance(message, str):
        message = str(message)
    prefixes = [exc_type.__name__ + ": "]
    if exc_type.__name__ in ("RuntimeError", "CloudPhoneError"):
        for prefix in _runtime_prefixes():
            if prefix not in prefixes:
                prefixes.append(prefix)
    while True:
        for prefix in prefixes:
            if message.startswith(prefix):
                message = message[len(prefix):]
                break
        else:
            break
    return message or exc_type.__name__


def _host_error(message):
    """The exception the host named. A CloudPhoneError stays a CloudPhoneError."""
    text = message if isinstance(message, str) else str(message)
    cloud = _cloud_phone_error()
    qualified = f"{cloud.__module__}.{cloud.__qualname__}: "
    named = RuntimeError
    while True:
        if text.startswith(qualified):
            named = cloud
            text = text[len(qualified):]
            continue
        if text.startswith("CloudPhoneError: "):
            named = cloud
            text = text[len("CloudPhoneError: "):]
            continue
        if text.startswith("RuntimeError: "):
            text = text[len("RuntimeError: "):]
            continue
        break
    return named(_plain_error(message, named))


class Remote(Backend):
    name = "remote"

    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token
        self._ops = None

    # --- the wire -----------------------------------------------------------

    def _call(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT,
                   **telemetry.cloud_headers()}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except ValueError:
                err = {}
            message = err.get("error") or f"the cloud phone answered {e.code}"
            if err.get("unsupported"):
                raise Unsupported(_plain_error(message, Unsupported)) from None
            if e.code == 404:
                raise RuntimeError("the cloud phone session is gone or its grant expired; "
                                   "`phone-harness cloud` shows whether it is still running") from None
            # The host's `error` already includes its exception type
            # ("RuntimeError: no installed app matches 'Notes'", or
            # "CloudPhoneError: session … expired"). Raising that string
            # as-is would print the type twice. A CloudPhoneError stays one,
            # so a dead cloud phone is not reported as a plain RuntimeError.
            error = _host_error(message)
            error.status, error.code = e.code, err.get("code")     # for _locked
            raise error from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"could not reach the cloud phone: {e.reason}") from None

    def _op(self, op, timeout=60, **kw):
        try:
            return self._call("POST", "/op", {"op": op, "kw": kw}, timeout=timeout)["result"]
        except RuntimeError as error:
            if self._locked(op, error):
                raise RuntimeError(LOCKED) from None
            raise

    def _locked(self, op, error):
        """Was `op` refused because the phone shows its lock screen or StandBy,
        on a host that can wake it?

        The host says so with `code: locked`. That code reaches a client of
        the host itself; the gateway in front of the control URL passes on the
        sentence alone. So a 409 with no code is settled by asking the phone
        its state, once: one more op, and only after a refusal. Only the list
        of ops already read is consulted, and a host that cannot wake a phone
        is asked nothing, so its own sentence stands.
        """
        ops = self._ops or ()
        if getattr(error, "status", None) != 409 or "device.wake" not in ops:
            return False
        if getattr(error, "code", None) == LOCKED_CODE:
            return True
        if op in ("session.state", "device.wake") or "session.state" not in ops:
            return False
        try:
            state = self._call("POST", "/op", {"op": "session.state", "kw": {}}, timeout=30)["result"]
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
            return False                    # the refusal already in hand is the one to report
        return state in WAKEABLE

    # --- capabilities -------------------------------------------------------

    def _known(self):
        if self._ops is None:
            self._ops = set(self._call("GET", "/ops")["ops"])
        return self._ops

    # Ops answered here without asking the host: bookkeeping for a phone
    # nobody's screen shows. Every other op, including the ones with a local
    # method that only shapes the result, exists only if the host lists it.
    LOCAL_OPS = frozenset({"session.refocus", "session.detail", "focus.probe", "focus.diff"})

    def supports(self, op):
        return op in self.LOCAL_OPS or op in self._known()

    def send(self, op, **kw):
        if not self.supports(op):
            raise Unsupported(f"this cloud phone cannot {op!r}")
        fn = getattr(self, self._slot(op), None)
        if fn is not None:
            return fn(**kw)
        return self._op(op, **kw)

    # --- the ops whose shape is local -----------------------------------------

    def _screen_capture(self, path=None):
        r = self._op("screen.capture", timeout=60)
        if path is None:
            fd, path = tempfile.mkstemp(prefix="phone-remote-", suffix=".png")
            os.close(fd)
        path = Path(path)
        path.write_bytes(base64.b64decode(r["png_b64"]))
        return str(path), r["bounds"]

    def _input_text(self, s, delay=0.03, keystrokes=False):
        return self._op("input.text", timeout=max(60, 0.2 * len(s) + 30), s=s, delay=delay, keystrokes=keystrokes)

    def _apps_launch(self, name, fresh=False):
        return self._op("apps.launch", timeout=90, name=name, fresh=fresh)

    def _apps_list(self, include_system=False):
        return self._op("apps.list", timeout=90, include_system=include_system)

    def _url_open(self, url):
        return self._op("url.open", timeout=60, url=url)

    def _session_refocus(self):
        return None

    def _session_detail(self):
        # The control URL's path is the grant, so only its host is named.
        return f"cloud phone at {urllib.parse.urlsplit(self.url).netloc}"

    def _focus_probe(self):
        return (True,)

    def _focus_diff(self, before, after):
        return {"raised": False, "stole_focus": False}
