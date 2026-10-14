"""Opt-in Mirroring experiment, loaded only by this directory's run script.

This patches process-local helpers. It is intended for one synchronous script,
not concurrent threads or a production backend.
"""
import json as _json
import subprocess as _subprocess
import os as _os
import time as _time
import uuid as _uuid
from contextlib import contextmanager as _contextmanager
from pathlib import Path as _Path
from Foundation import NSRunLoop as _NSRunLoop, NSDate as _NSDate
from phone_harness import helpers as _h
from phone_harness import background as _background
from phone_harness.transport import Unsupported as _Unsupported

if _h.phone.name != "iphone-mirroring":
    raise RuntimeError("This experiment requires the local iPhone Mirroring backend")

_root = _Path(__file__).resolve().parent
_binary = _root / "native/mirror_events"
_output = _Path(_os.environ.get("PH_MIRRORING_OUTPUT", str(_root / "output"))).resolve()
_focus_log = _output / "focus/actions.jsonl"
_focus_log.parent.mkdir(parents=True, exist_ok=True)
_os.environ['PH_FOCUS_ACTION_LOG'] = str(_focus_log)


@_contextmanager
def _focus_span(label):
    span_id = _uuid.uuid4().hex[:12]
    parent = _os.environ.get('PH_FOCUS_PARENT')
    def emit(phase, **extra):
        row = {'time_ns': _time.time_ns(), 'monotonic_ns': _time.monotonic_ns(),
               'kind': 'action', 'phase': phase, 'label': label, 'id': span_id,
               'parent': parent, 'pid': _os.getpid(), **extra}
        with _focus_log.open('a') as log:
            log.write(_json.dumps(row, separators=(',', ':'))+'\n')
    emit('begin')
    _os.environ['PH_FOCUS_PARENT'] = span_id
    try:
        yield
    except BaseException as exc:
        emit('end', error_type=type(exc).__name__)
        raise
    else:
        emit('end')
    finally:
        if parent is None:
            _os.environ.pop('PH_FOCUS_PARENT', None)
        else:
            _os.environ['PH_FOCUS_PARENT'] = parent


def _refresh_after_child():
    # AppKit briefly returned no Mirroring application after the native child
    # exited, while its window and process still existed. Deliver workspace
    # updates before the normal connection checks; never skip those checks.
    _NSRunLoop.currentRunLoop().runUntilDate_(
        _NSDate.dateWithTimeIntervalSinceNow_(.1))


def _native(*args):
    _h.phone._raise_if_not_live()
    route = ["--minimal-record"] if args[0] == "tap" else ["--pid-route"]
    if _NO_FRONT and "--no-front" not in args:   # no-front experiment (opt-in)
        route.append("--no-front")
    with _focus_span('native.'+str(args[0])):
        result = _subprocess.run(
            [str(_binary), *map(str, args), *route, "--send"],
            capture_output=True, text=True, timeout=15,
        )
    record = {"args": args, "exit_code": result.returncode,
              "stderr": result.stderr.strip()}
    if result.stdout.strip():
        record["result"] = _json.loads(result.stdout)
    with (_output / "submissions.jsonl").open("a") as log:
        log.write(_json.dumps(record) + "\n")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Native helper failed")
    _refresh_after_child()
    _h.phone._raise_if_not_live()
    return record["result"]  # submitted is not a phone acknowledgment


class _ScratchInput:
    def __getattr__(self, name):
        return getattr(_background, name)

    def show(self):
        with _focus_span('mirroring.show'):
            return _background.show()

    def tap(self, x, y):
        win = _background.ensure_window()
        nx, ny = (x - win["x"]) / win["w"], (y - win["y"]) / win["h"]
        if not (0 <= nx <= 1 and 0 <= ny <= 1):
            raise ValueError("Tap is outside the current Mirroring window")
        return _native("tap", nx, ny)

    def press(self, combo):
        if combo.lower() == "cmd+1" and not _NO_FRONT:   # in no_front(): in-process key path
            return _native("home")
        return _background.press(combo)

    def scroll_wheel(self, dy, x, y, steps=6, dx=0):
        raise _Unsupported(
            "Cursor-free scrolling is unverified. Use native_probe('scroll', "
            "delta, ...) for an explicit experiment; no cursor-warp fallback."
        )


_h.phone.mirror = _ScratchInput()
_h.phone.background = True
_original_send = _h.phone.send
def _measured_send(op, **kw):
    with _focus_span('api.'+op):
        return _original_send(op, **kw)
_h.phone.send = _measured_send
_original_supports = _h.phone.supports
_h.phone.supports = lambda op: op != "input.scroll" and _original_supports(op)


def native_probe(action, delta, *options):
    """Submit one unverified gesture; inspect the screen separately.

    Options are the native CLI's arguments, e.g. --sender ID, --at X Y,
    --prime-position, --tracking-region ID, --activate. Never reuse stale IDs.
    This explicit probe does not advertise these gestures as supported.
    """
    if action not in {"scroll", "magnify", "rotate", "translate", "swipe"}:
        raise ValueError("Expected scroll, magnify, rotate, translate, or swipe")
    return _native(action, delta, *options)


def native_status():
    return {"adapter": str(_Path(__file__).resolve()), "binary": str(_binary),
            "native_actions": ["tap", "home", "tap --double (scratch)", "tap --no-front (scratch)", "native_nav app-switcher/spotlight (scratch)"],
            "tap_route": "minimal-record matching background._post",
            "connection_checks": "existing guards after AppKit update delivery",
            "cursor_warp_scroll_disabled": True, "output": str(_output),
            "no_front_active": no_front_active(),
            "other_input": "existing background keyboard/press/drag",
            "gestures": "unverified explicit native_probe only"}


def tap_icon(label, index=0):
    hits = _h.find_text(label)
    if not hits:
        raise RuntimeError(f"no Home-Screen label matching {label!r}")
    hit = hits[index]
    _h.tap(hit["x"], hit["y"] - 35)
    return hit


def native_raw(action, *options, send=True):
    """Explicit helper call with caller-chosen options (route included).

    Unlike _native, this does NOT force --pid-route; pass it yourself if
    wanted. Every call is logged to submissions.jsonl with route='raw'.
    Submission is not a phone acknowledgment: check the screen separately.
    """
    _h.phone._raise_if_not_live()
    args = [str(_binary), str(action), *map(str, options)]
    if _NO_FRONT and action != "info" and "--no-front" not in args:
        args.append("--no-front")
    if send and "--send" not in args:
        args.append("--send")
    with _focus_span('native_raw.'+str(action)):
        result = _subprocess.run(args, capture_output=True, text=True, timeout=20)
    record = {"args": args[1:], "exit_code": result.returncode,
              "stderr": result.stderr.strip(), "via": "native_raw"}
    if result.stdout.strip():
        try:
            record["result"] = _json.loads(result.stdout)
        except ValueError:
            record["stdout"] = result.stdout.strip()
    with (_output / "submissions.jsonl").open("a") as log:
        log.write(_json.dumps(record) + "\n")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Native helper failed")
    _refresh_after_child()
    _h.phone._raise_if_not_live()
    return record.get("result")


def norm_point(x, y):
    """Global screen point -> normalized full-window coordinates."""
    win = _background.ensure_window()
    return (x - win["x"]) / win["w"], (y - win["y"]) / win["h"]


# ---- gesture pass (2026-10-14 13:30 PDT): scratch-only markers and variants ----
import Quartz as _Quartz
_steps_log = _output / "gesture/steps.jsonl"
_steps_log.parent.mkdir(exist_ok=True)


def _emit_native_call(label, **extra):
    """Marker row in focus/actions.jsonl for the in-process background paths
    (same shape as the native helper's rows). Never records key codes."""
    row = {'time_ns': _time.time_ns(), 'kind': 'native_call', 'label': label,
           'action': 'background', 'pid': _os.getpid(),
           'parent': _os.environ.get('PH_FOCUS_PARENT', ''), **extra}
    with _focus_log.open('a') as log:
        log.write(_json.dumps(row, separators=(',', ':')) + '\n')


_orig_front = _background._sky._SLPSSetFrontProcessWithOptions
def _traced_front(psn, wid, opts):
    _emit_native_call('bg.front.prepare.begin', window=int(wid), options=int(opts))
    status = _orig_front(psn, wid, opts)
    _emit_native_call('bg.front.prepare.end', window=int(wid), result=int(status or 0))
    return status
_background._sky._SLPSSetFrontProcessWithOptions = _traced_front

_orig_post_record = _background._sky.SLPSPostEventRecordTo
def _traced_post_record(psn, buf):
    _emit_native_call('bg.post.record.begin')
    status = _orig_post_record(psn, buf)
    _emit_native_call('bg.post.record.end', result=int(status or 0))
    return status
_background._sky.SLPSPostEventRecordTo = _traced_post_record

_orig_post_pid = _Quartz.CGEventPostToPid
def _traced_post_pid(pid, ev):
    etype = int(_Quartz.CGEventGetType(ev))   # 10 keyDown / 11 keyUp; no key code
    _emit_native_call('bg.post.pid.begin', target_pid=int(pid), event_type=etype)
    result = _orig_post_pid(pid, ev)
    _emit_native_call('bg.post.pid.end', target_pid=int(pid), event_type=etype)
    return result
_Quartz.CGEventPostToPid = _traced_post_pid


def mark(step, **info):
    """Test-step marker (gesture/steps.jsonl) to correlate with the observer."""
    row = {'time_ns': _time.time_ns(), 'step': step, **info}
    with _steps_log.open('a') as log:
        log.write(_json.dumps(row, separators=(',', ':'), default=str) + '\n')
    return row['time_ns']


def texts(n=40, min_confidence=0.3):
    return [o['text'] for o in _h.screen(min_confidence)][:n]


def double_tap(x, y):
    """Scratch-only: down/up/down/up at 50 ms spacing in one helper call."""
    win = _background.ensure_window()
    nx, ny = (x - win["x"]) / win["w"], (y - win["y"]) / win["h"]
    if not (0 <= nx <= 1 and 0 <= ny <= 1):
        raise ValueError("Tap is outside the current Mirroring window")
    with _focus_span('api.double_tap'):
        return _native("tap", nx, ny, "--double")


def tap_no_front(x, y):
    """Scratch-only variant: minimal-record tap WITHOUT any
    _SLPSSetFrontProcessWithOptions preparation (experiment only)."""
    win = _background.ensure_window()
    nx, ny = (x - win["x"]) / win["w"], (y - win["y"]) / win["h"]
    if not (0 <= nx <= 1 and 0 <= ny <= 1):
        raise ValueError("Tap is outside the current Mirroring window")
    with _focus_span('api.tap_no_front'):
        return _native("tap", nx, ny, "--no-front")


def native_nav(action):
    """Native helper app-switcher / spotlight / home over the PID key route."""
    if action not in {"home", "app-switcher", "spotlight"}:
        raise ValueError("home, app-switcher or spotlight")
    with _focus_span('api.native_nav.' + action):
        return _native(action)


# ---- no-front experiment (2026-10-14 15:05 PDT): explicit opt-in, scratch only ----
_NO_FRONT = False


def _skip_front(psn, wid, opts):
    """Marker-only stand-in for _SLPSSetFrontProcessWithOptions inside no_front()."""
    _emit_native_call('bg.front.skipped', window=int(wid), options=int(opts))
    return 0


@_contextmanager
def no_front():
    """Skip front-process preparation for input dispatched inside the body.

    - in-process background paths (_post, _make_key): the SkyLight front-process
      call is replaced by a marker-only stub; record encoding, timing, session
      guards (phone.send) and post markers are unchanged
    - native helper calls get --no-front (tap edges included)
    - press('cmd+1') uses the in-process key path instead of the native helper
    Does not suppress explicit session activation/reconnection (ensure_device,
    activate, show). Establish the connection before testing. This context is
    process-global and must not overlap other threads' input. Default behaviour
    outside the context is unchanged (baseline preserved).
    """
    global _NO_FRONT
    prev_flag, prev_front = _NO_FRONT, _background._sky._SLPSSetFrontProcessWithOptions
    _NO_FRONT = True
    _background._sky._SLPSSetFrontProcessWithOptions = _skip_front
    try:
        _emit_native_call('no_front.enter')
        yield
    finally:
        _background._sky._SLPSSetFrontProcessWithOptions = prev_front
        _NO_FRONT = prev_flag
        _emit_native_call('no_front.exit')


def no_front_active():
    return _NO_FRONT and _background._sky._SLPSSetFrontProcessWithOptions is _skip_front


def set_steps_log(path):
    """Redirect mark() rows to another append-only file (e.g. no-front/steps.jsonl)."""
    global _steps_log
    _steps_log = _output / path
    _steps_log.parent.mkdir(parents=True, exist_ok=True)
    return str(_steps_log)


def no_front_status():
    """Public view for scripts (private names are hidden from script globals)."""
    return {'active': no_front_active(), 'flag': _NO_FRONT,
            'front_fn': getattr(_background._sky._SLPSSetFrontProcessWithOptions, '__name__', 'ctypes _SLPSSetFrontProcessWithOptions')}
