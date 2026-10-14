# Native iPhone Mirroring experiment

This preserves the October 14, 2026 gesture and focus experiments in a
self-contained directory. The existing `PH_AGENT_WORKSPACE` extension loads
the adapter only through `./run`. No normal backend defaults, package contents,
or persistent configuration change. Delete this directory to remove it.

The experiment sends events to the running consumer iPhone Mirroring app over
its existing wireless session. It uses no USB, pymobiledevice3, Developer Mode,
developer services, injected code, or extra phone app. These are private macOS
APIs and an observed event-record layout, so results are build-dependent.

## What was learned

The original `_SLPSSetFrontProcessWithOptions(..., 0x200)` preparation stole
keyboard focus even when Mirroring stayed in another Space and did not raise
its window. Removing that call preserved the working tap, Home-page swipe,
long-press and keyboard paths in the tested off-Space condition. The follow-up
observed 38 actions, 218 skipped preparation calls and 494 event posts with
no observed app activation, AX focus change, visible raise or Space change.

List scrolling remains unresolved. Pinch zoom worked with the prepared native
gesture path; rotation was inconsistent. These observations are not a claim
of arbitrary multi-touch support. See [FINDINGS.md](FINDINGS.md) for the matrix,
repetitions, timing issue and measurement limits.

## Setup

Run from a checkout with Python 3.10+, the repository's ordinary macOS Python
dependencies installed, and Xcode Command Line Tools. For example, activate a
virtual environment and run `python -m pip install -e .` at the repository root.
Use the normal macOS Accessibility and Screen Recording permissions required
by phone-harness. Establish a working iPhone Mirroring session first.

```sh
experiments/iphone-mirroring/build
```

The test machine required SDK 26.5 because its default SDK 27 did not match the
installed compiler. Select an installed compatible SDK when needed:

```sh
PH_MIRRORING_SDK=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk \
  experiments/iphone-mirroring/build
```

`run` uses `python3` from PATH. Set `PH_MIRRORING_PYTHON` to another Python
executable with the repository's dependencies installed if needed. Paths are
resolved from the checkout; no machine-specific environment is required.

## Run one experiment

First inspect the connection without activating it:

```sh
experiments/iphone-mirroring/run <<'PY'
# task: check the experimental Mirroring adapter
# step: inspect connection and visible phone content
print(native_status())
print(connection_state())
print(screen())
PY
```

Check the content too: a session after Mac sleep reported `ready` while showing
an interstitial. The existing session guards remain enabled; this experiment
does not fix that discrepancy. `ensure_device()` and explicit activation can
take focus or change Spaces. Reconnect before starting a focus measurement.

Wrap input in the explicit context to skip front-process preparation:

```sh
experiments/iphone-mirroring/run <<'PY'
# task: verify Home without taking Mac focus
# step: send Home through the in-process key path and check the phone
with no_front():
    mark("home.begin")
    press("cmd+1")
    mark("home.end")
print(screen())
PY
```

The same context covers `tap`, `double_tap`, `long_press`, `swipe`, `press`,
`type_text(..., keystrokes=True)`, `native_nav`, `native_probe` and `native_raw`.
Only the paths in the findings have been verified on the phone. Coordinates
for ordinary helpers are global Mac screen points, including OCR coordinates;
the native CLI takes normalized full-window coordinates. Do not reuse old
PID, window, sender or tracking IDs.

Outside `no_front()` the prepared baseline is preserved and can steal focus.
The context patches process-local functions and restores them even on an
exception; use it only in a single synchronous script. It does not prevent
explicit activation/reconnection. The clipboard-based default `type_text`
was not tested; use `keystrokes=True` for the verified path. Back-to-back
backspaces lost keys; 250 ms between presses cleared the test query reliably.

## Native probes

`native/mirror_events.m` implements targeted tap/navigation plus experimental
scroll, swipe, magnify, rotate and translate events. It defaults to a dry run;
only `--send` posts events. A dry run still requires a running Mirroring window
to resolve its geometry. `submitted` and zero dispatch errors are not evidence
that the phone reacted. `target_is_active` can be cached; use the observer.

```sh
experiments/iphone-mirroring/native/mirror_events info
experiments/iphone-mirroring/native/mirror_events tap 0.5 0.5 --minimal-record --no-front
experiments/iphone-mirroring/native/list_hid_services
```

The read-only HID inventory returns current local service IDs and product
names. To reproduce the zoom probe, identify a real trackpad service and use
its current decimal ID as `TRACKPAD_SERVICE_ID` on a zoomable phone surface:

```sh
experiments/iphone-mirroring/native/mirror_events magnify 0.5 \
  --pid-route --sender "$TRACKPAD_SERVICE_ID" --at 0.5 0.6 --send
```

That command reproduces the **prepared** zoom path and can steal focus.
`--no-front` is available, but zoom without preparation was not tested in the
recorded pass. `--activate` explicitly raises the app and cannot be combined
with `--no-front`. Ordinary `scroll()` raises `Unsupported` in this adapter,
and `supports('input.scroll')` is false, so there is no cursor-warp fallback.

## Measure focus

Run the observer in a separate terminal using the same Python environment:

```sh
python3 experiments/iphone-mirroring/focus_watch.py --seconds 120 \
  --output experiments/iphone-mirroring/output/focus/watch.jsonl
```

It observes AX focus, application activation notifications, on-screen window
order, active Space ID and cursor position. It does not post input or record
typed text, key codes, window titles or screen content. `--seconds` is a
monotonic-time budget (1–1800); Mac sleep can extend wall-clock lifetime.
Ctrl-C or SIGTERM stops it and appends `watch_end` with the maximum sample gap.

Correlate wall-clock `time_ns` from the observer with `output/focus/actions.jsonl`
and `mark()` rows in `output/gesture/steps.jsonl`. Action spans carry `id` and
`parent`; native calls record prepare/post boundaries. Compare focus before
the first post, during the gesture and afterward; record window order and
Space separately. A baseline already focused on Mirroring is inconclusive
for proving focus preservation. Do not label OCR jitter or dispatch success
as phone movement.

Generated output defaults to the ignored `output/` directory. Set
`PH_MIRRORING_OUTPUT` to relocate adapter logs. `mark()` logs whatever the
caller supplies, so keep screen contents out of shared reports. Local raw
screenshots, transcripts and observer logs from the original tests are not
part of this repository; [FINDINGS.md](FINDINGS.md) preserves the conclusions.

## Check the saved code

```sh
python3 -m unittest discover -s experiments/iphone-mirroring/tests
python3 -m unittest discover tests
```

The focused tests use fake Apple APIs and never operate a phone. Build and
dry-run checks validate packaging and event construction, not live gesture
delivery. Testing the active-Space and already-focused cases, reliable
scrolling, and wider device/OS coverage remains future work.
