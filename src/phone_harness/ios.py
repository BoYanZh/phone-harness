"""iOS backend: the op vocabulary over iPhone Mirroring.

An adapter, not a rewrite. mirror.py and background.py keep their shape and are
imported as they are — they hold the details that were expensive to learn (why
NSWorkspace's frontmost value is a constant, why a slow drag barely registers
on iOS, why the live phone image is opaque to accessibility) and none of that
should move just to change the seam above it.

What iPhone Mirroring cannot do appears here as absence rather than emulation:

  no `nav.back`      iOS has no system Back button. Guessing at an edge swipe
                     returns something the caller cannot tell from a real Back.
  no `apps.current`  the window exposes no app inventory; the foreground app is
  no `apps.list`     only knowable by reading the screen.
  no `tree`          the phone image is a video stream, which is the whole
                     reason screen.text has to recognise glyphs.
  no `raw`           there is no single command channel the way adb is one. The
                     substrate is Quartz: `import Quartz` in your own script,
                     or reach through this backend's .mirror module.

Each raises Unsupported, so a caller can ask rather than discover it by
getting a plausible wrong answer.
"""
import importlib
import os

from . import ocr as _vision
from .transport import Backend

# Fallback only. The primary check is structural (see _session_state), and a
# phrase list cannot be complete: it missed "Connection Paused" and the Mac
# login screen in English, and misses every interstitial on a non-English
# system.
_BLOCKED_MARKERS = ("iphone in use", "lock your iphone", "mirroring ended",
                    "to connect", "connection paused", "connection interrupted",
                    "is locked", "enter the mac login", "try again")


def _load_transport():
    """background drives iPhone Mirroring without ever taking the window to the
    front (SkyLight event records) and is the default: not covering the user's
    screen is what you want unless something is broken. Its private SkyLight
    symbols are not guaranteed across macOS builds, so fall back rather than
    leaving the harness unusable."""
    want_bg = os.environ.get("PHONE_HARNESS_BACKGROUND", "1").lower() not in (
        "0", "false", "no")
    if want_bg:
        try:
            return importlib.import_module(".background", __package__), True
        except Exception:
            pass
    return importlib.import_module(".mirror", __package__), False


class IPhone(Backend):
    name = "iphone-mirroring"

    def __init__(self):
        self.mirror, self.background = _load_transport()

    # --- screen ---------------------------------------------------------

    def _screen_bounds(self):
        return self.mirror.find_window()

    def _screen_require(self):
        """Bounds for gesture maths. Distinct from session.require, which is
        the full connected gate — reading the window rect should not put the
        session through an interstitial check on every swipe."""
        return self.mirror.ensure_window()

    def _screen_capture(self, path=None):
        return self.mirror.capture(path)

    def _screen_text(self, min_confidence=0.3):
        path, win = self.mirror.capture()
        return [dict(o, source="pixels")
                for o in _vision.recognize(path, win)
                if o["confidence"] >= min_confidence]

    # Pixels are the only source iOS has ever had.
    _screen_text_pixels = _screen_text

    # --- input ----------------------------------------------------------

    def _input_tap(self, x, y):
        self.mirror.tap(x, y)

    def _input_press(self, x, y, duration=0.8):
        self.mirror.long_press(x, y, duration)

    def _input_drag(self, x1, y1, x2, y2, duration=0.35, steps=14):
        self.mirror.drag(x1, y1, x2, y2, duration=duration, steps=steps)

    def _input_scroll(self, x, y, dy, dx=0, steps=6):
        self.mirror.scroll_wheel(dy, x, y, steps=steps, dx=dx)

    def _input_keys(self, combo):
        self.mirror.press(combo)

    def _input_text(self, s, delay=0.03, keystrokes=False):
        self.mirror.type_text(s, delay=delay, keystrokes=keystrokes)

    # --- navigation -----------------------------------------------------

    def _nav_home(self):
        self.mirror.press("cmd+1")
        _sleep(0.8)

    def _nav_recents(self):
        self.mirror.press("cmd+2")
        _sleep(0.8)

    # No _nav_back: iOS has no system Back button.

    def _apps_launch(self, name):
        """Spotlight (Cmd+3): type the name, let results populate, commit."""
        self.mirror.press("cmd+3")
        _sleep(0.9)
        # Keystrokes on purpose: Spotlight is the load-bearing path behind
        # open_app and demonstrably works this way. Move it to paste only once
        # that path has miles on it.
        self.mirror.type_text(name, keystrokes=True)
        _sleep(1.2)
        self.mirror.press("return")
        return name

    # --- session --------------------------------------------------------

    def _session_state(self):
        """'ready' | 'blocked' | 'no-window' | 'not-running'.

        'blocked' means an interstitial is up — iPhone in Use, paused, ended,
        connect, or the Mac login screen — and nothing should be tapped or
        typed until the user clears it.

        Detected structurally: the live phone image is a video stream that
        accessibility cannot see into, so a working session exposes no UI
        inside the window, while every interstitial is an ordinary Mac view
        with labels and a button. That holds in any language and for screens
        Apple has not shipped yet, where matching known phrases does neither —
        the old list missed both "Connection Paused" and the Mac login prompt,
        and reported 'ready' for a password field.
        """
        if self.mirror.running_app() is None:
            return "not-running"
        if self.mirror.find_window() is None:
            return "no-window"
        if self.mirror.window_ax_content():
            return "blocked"
        path, win = self.mirror.capture()   # window exists, launches nothing
        texts = " ".join(o["text"] for o in _vision.recognize(path, win)).lower()
        return "blocked" if any(m in texts for m in _BLOCKED_MARKERS) else "ready"

    def _session_detail(self):
        """What the interstitial says, quoted rather than guessed at.

        Button titles are not used: the same screen reported 'Connect' once and
        an empty title a minute later.
        """
        return " ".join(t for role, t in self.mirror.window_ax_content()
                        if role == "AXStaticText" and t)

    def _session_require(self):
        """Bounds once the phone is connected and ready.

        First does what the user would do by hand: opens iPhone Mirroring
        when it is not running, and presses the interstitial's own button
        (Connect, Try Again, Continue) once. What is left after that is
        physical — an unlocked phone ("iPhone in Use", "Timed Out"), the Mac
        login prompt, a phone that is not paired — and only the user can
        clear it, so this raises quoting the window rather than pressing
        again. Once the phone is live the window is brought to the front so
        the user can watch the task. One press per call: a caller that retries after the user says
        the phone is locked gets a fresh press.
        """
        state = self._session_state()
        if state == "not-running":
            self.mirror.launch()
            state = self._wait_state(("blocked", "ready"), _LAUNCH_WAIT)
        # Shown before anything else so the user sees what the harness sees,
        # the interstitial included, not only a session that worked.
        self._show()
        if state == "no-window":
            state = self._wait_state(("blocked", "ready"), _WINDOW_WAIT)
        pressed = None
        if state == "blocked":
            pressed = self._press_connect()
            if pressed:
                state = self._wait_connected(_CONNECT_WAIT)
        if state == "ready":
            return self.mirror.find_window()
        if state == "not-running":
            raise RuntimeError(
                "iPhone Mirroring would not launch. Please open the iPhone "
                "Mirroring app and connect your phone, then retry.")
        if state == "no-window":
            raise RuntimeError(
                "iPhone Mirroring is open but shows no phone window. Pair "
                "the phone in the app (it has to be nearby, locked, and "
                "signed in to the same Apple Account), then retry.")
        said = self._session_detail() or "an interstitial with no text"
        did = (f"I pressed {pressed} and it came back with"
               if pressed else "It says")
        raise RuntimeError(
            f"iPhone Mirroring is not connected. {did}: {said}. This needs "
            "you: if it mentions the iPhone being in use or timing out, LOCK "
            "your iPhone, then tell me and I will try once more. I never "
            "type a passcode.")

    def _show(self):
        """Bring the mirroring window to the front so the user can watch.
        Best effort: a window that will not come forward is not a reason to
        fail the session, since the background build drives it regardless."""
        try:
            self.mirror.show()
        except Exception:
            pass

    def _press_connect(self):
        """Press the interstitial's button through accessibility, once.

        Returns the title pressed, or None when there is nothing safe to
        press: no button, several buttons (the choice is the user's), or a
        login prompt — pressing Unlock without a password does nothing, and
        the password is never ours to type.
        """
        content = self.mirror.window_ax_content()
        if any(role in ("AXTextField", "AXSecureTextField")
               for role, _ in content):
            return None
        buttons = [t for role, t in content if role == "AXButton"]
        if len(buttons) == 1 and buttons[0].lower() not in _NOT_CONNECT_BUTTONS:
            return self.mirror.press_window_button()
        return self.mirror.press_window_button(_CONNECT_BUTTONS)

    def _wait_state(self, wanted, timeout):
        """Poll until the session is in one of `wanted` or time runs out;
        returns the last state seen."""
        deadline = _now() + timeout
        state = self._session_state()
        while state not in wanted and _now() < deadline:
            _sleep(0.5)
            state = self._session_state()
        return state

    def _wait_connected(self, timeout):
        """After a press: wait for the live stream, and fail fast otherwise.

        The attempt is watched through an accessibility observer on iPhone
        Mirroring, so the Connecting screen is noticed when it changes rather
        than on a timer. A hang emits nothing, so the wait still gives up at
        `timeout`. If the observer cannot be installed, the same decision is
        made by polling.

        'ready' has to hold for three evaluations in a row. Measured: the
        press empties the window for about half a second before "Connecting
        to" draws, and one empty read in that gap looks live.

        The Connecting screen is in progress when its text contains
        "Connecting to" or it offers a Background button. A live attempt
        showed "Connecting to iPhone 17 Pro Max" with no button at all, and
        treating "no Background button" as a finished failure gave up while
        the phone was still connecting. A blocked interstitial that is not
        that screen — "iPhone in Use", locked, timed out — is the answer once
        the attempt has had a couple of seconds to draw, so it fails then
        instead of sitting out the cap. The user locking the phone is what
        fixes it, and the app reconnects by itself, so the next
        session.require finds it live."""
        try:
            return self._wait_connected_events(timeout)
        except _ObserverUnavailable:
            return self._wait_connected_polling(timeout)

    def _wait_connected_events(self, timeout):
        """Event-driven wait. One evaluation on entry, then again on every
        accessibility notification. If a notification is missed, the same
        check runs about once a second so the attempt cannot stall."""
        start = _now()
        streak = 0
        state = "blocked"
        decided = None
        last_event = start

        def evaluate():
            nonlocal streak, state, decided
            state = self._session_state()
            decided, streak = self._connection_choice(
                state, streak, _now() - start)

        def on_notify():
            nonlocal last_event
            last_event = _now()
            evaluate()
            if decided is not None:
                _stop_run_loop()

        watch = _ax_watch_open(self.mirror, on_notify)
        try:
            evaluate()
            if decided is not None:
                return decided
            while _now() - start < timeout:
                remaining = timeout - (_now() - start)
                before = _now()
                _run_loop_slice(min(_EVENT_SLICE, remaining))
                if decided is not None:
                    return decided
                # A hang, or a notification the observer did not deliver.
                if _now() - last_event >= _EVENT_QUIET:
                    evaluate()
                    last_event = _now()
                    if decided is not None:
                        return decided
                if _now() <= before:
                    _sleep(min(_EVENT_SLICE, timeout - (_now() - start)))
            return "blocked" if state == "ready" and streak < 3 else state
        finally:
            watch.close()

    def _wait_connected_polling(self, timeout):
        """The pre-observer wait, used when AXObserver cannot be installed.

        Same decisions as the event wait, including treating a "Connecting
        to" screen with no buttons as still in progress."""
        start = _now()
        _sleep(1.0)
        streak = 0
        state = self._session_state()
        while _now() - start < timeout:
            decided, streak = self._connection_choice(
                state, streak, _now() - start)
            if decided is not None:
                return decided
            _sleep(0.5)
            state = self._session_state()
        return "blocked" if state == "ready" and streak < 3 else state

    def _connection_choice(self, state, streak, elapsed):
        """('ready' | 'blocked' | None, streak) for one look at the window.

        None means the attempt is still in progress. 'ready' only after the
        live stream has held for three looks. 'blocked' is a finished
        interstitial once the Connecting screen has had time to appear."""
        streak = streak + 1 if state == "ready" else 0
        if streak >= 3:
            return "ready", streak
        if (state == "blocked" and elapsed > _ATTEMPT_SETTLE
                and not self._still_connecting()):
            return "blocked", streak
        return None, streak

    def _still_connecting(self):
        """The Connecting screen, including one that has no buttons.

        The words are the signal. A Background button is the other form of
        the same screen; either one means the press has not finished."""
        content = self.mirror.window_ax_content()
        text = " ".join(t for _role, t in content if t).lower()
        if "connecting to" in text:
            return True
        return any(t.lower() == "background"
                   for t, _el in self.mirror.window_ax_buttons())

    def _session_refocus(self):
        self.mirror.activate()

    # --- interruption ---------------------------------------------------

    def _focus_probe(self):
        return self.mirror.focus_probe()

    def _focus_diff(self, before, after):
        return self.mirror.interruption(before, after)


# Buttons an interstitial offers that mean "try to connect"; pressed once
# per session.require. Lower-case; matched case-insensitively.
_CONNECT_BUTTONS = ("connect", "try again", "retry", "continue", "reconnect")
# A lone button that is not an attempt to connect: "Background" on the
# Connecting screen sends the app behind, "Cancel" gives up.
_NOT_CONNECT_BUTTONS = ("background", "cancel", "quit", "unlock", "ok",
                        "done", "settings")
_LAUNCH_WAIT = 12.0     # app launch to phone window
_WINDOW_WAIT = 6.0      # app running, window still coming up
_CONNECT_WAIT = 12.0    # press to live stream, the cap; a hang emits no events
_ATTEMPT_SETTLE = 2.5   # a press has drawn Connecting and then its outcome by now
_EVENT_SLICE = 0.2      # how long one CFRunLoopRunInMode turn waits
_EVENT_QUIET = 1.0      # re-check when no notification arrives for this long

# Posted on the app element and the phone window. A Connecting screen that
# never exposes a button still changes one of these as its label appears and
# as the live stream replaces it.
_AX_NOTIFICATIONS = (
    "kAXValueChangedNotification",
    "kAXTitleChangedNotification",
    "kAXUIElementDestroyedNotification",
    "kAXCreatedNotification",
    "kAXWindowCreatedNotification",
    "kAXLayoutChangedNotification",
    "kAXFocusedUIElementChangedNotification",
)
_AX_NOTIFICATION_NAMES = (
    "AXValueChanged",
    "AXTitleChanged",
    "AXUIElementDestroyed",
    "AXCreated",
    "AXWindowCreated",
    "AXLayoutChanged",
    "AXFocusedUIElementChanged",
)


class _ObserverUnavailable(Exception):
    """AXObserver could not be installed. The polling wait is the fallback."""


class _AxWatch:
    """An observer and the run-loop source it was scheduled on.

    close() removes both. It is safe to call twice, and a failure while
    removing one does not skip the other."""

    def __init__(self, remove_note, remove_source, notes, callback):
        self._remove_note = remove_note
        self._remove_source = remove_source
        self._notes = list(notes)
        self._callback = callback          # the observer holds this too
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._remove_source()
        except Exception:
            pass
        for el, name in self._notes:
            try:
                self._remove_note(el, name)
            except Exception:
                pass


def _ax_watch_open(mirror, on_notify):
    """Observe iPhone Mirroring and call `on_notify` for each notification.

    `on_notify` takes no arguments. The caller runs the observer's run-loop
    source in short slices via `_run_loop_slice` and must `close()` the
    returned watch.

    Raises _ObserverUnavailable, without probing the app, when this transport
    has no `ax_observer_target`. A test double therefore falls straight
    through to polling and does not consume a scripted frame.
    """
    target = getattr(mirror, "ax_observer_target", None)
    if target is None:
        raise _ObserverUnavailable(
            "this transport has no accessibility observer")
    try:
        opened = target()
    except Exception as exc:
        raise _ObserverUnavailable(
            "could not read the mirroring app for an observer") from exc
    if not opened or len(opened) != 2:
        raise _ObserverUnavailable("the mirroring app is not observable")
    pid, elements = opened
    elements = [el for el in (elements or []) if el is not None]
    if pid is None or not elements:
        raise _ObserverUnavailable("the mirroring app is not observable")

    try:
        import ApplicationServices as ax
        import CoreFoundation as cf
    except Exception as exc:
        raise _ObserverUnavailable(
            "accessibility observer support is unavailable") from exc

    holder = {"fn": on_notify}

    def callback(observer, element, notification, refcon):
        # One bad notification must not kill the run loop. The caller's
        # quiet re-check evaluates again outside the callback.
        try:
            holder["fn"]()
        except Exception:
            pass

    try:
        try:
            created = ax.AXObserverCreate(int(pid), callback, None)
        except TypeError:
            created = ax.AXObserverCreate(int(pid), callback)
    except Exception as exc:
        raise _ObserverUnavailable("AXObserverCreate failed") from exc
    if isinstance(created, tuple):
        err = created[0]
        observer = created[1] if len(created) > 1 else None
    else:
        err, observer = 0, created
    if err or observer is None:
        raise _ObserverUnavailable("AXObserverCreate failed")

    try:
        source = ax.AXObserverGetRunLoopSource(observer)
    except Exception as exc:
        raise _ObserverUnavailable(
            "the observer has no run-loop source") from exc
    if source is None:
        raise _ObserverUnavailable("the observer has no run-loop source")
    try:
        loop = cf.CFRunLoopGetCurrent()
        mode = cf.kCFRunLoopDefaultMode
        cf.CFRunLoopAddSource(loop, source, mode)
    except Exception as exc:
        raise _ObserverUnavailable(
            "could not schedule the observer") from exc

    names = [getattr(ax, const, None) or fallback
             for const, fallback in zip(_AX_NOTIFICATIONS,
                                        _AX_NOTIFICATION_NAMES)]
    added = []

    def remove_source():
        cf.CFRunLoopRemoveSource(loop, source, mode)

    try:
        for el in elements:
            for name in names:
                try:
                    note_err = ax.AXObserverAddNotification(
                        observer, el, name, None)
                except Exception:
                    continue
                if not note_err:
                    added.append((el, name))
        if not added:
            raise _ObserverUnavailable(
                "no accessibility notifications were registered")
    except _ObserverUnavailable:
        try:
            remove_source()
        except Exception:
            pass
        raise

    def remove_note(el, name):
        ax.AXObserverRemoveNotification(observer, el, name)

    return _AxWatch(remove_note, remove_source, added, callback)


def _run_loop_slice(seconds):
    """Run the current thread's run loop long enough for one burst of
    accessibility notifications. returnAfterSourceHandled is false so a
    single event does not end the slice early; the callback stops the loop
    itself once the attempt is decided."""
    import CoreFoundation as cf
    cf.CFRunLoopRunInMode(cf.kCFRunLoopDefaultMode, float(seconds), False)


def _stop_run_loop():
    try:
        import CoreFoundation as cf
        cf.CFRunLoopStop(cf.CFRunLoopGetCurrent())
    except Exception:
        pass


def _now():
    import time
    return time.time()


def _sleep(s):
    import time
    time.sleep(s)
