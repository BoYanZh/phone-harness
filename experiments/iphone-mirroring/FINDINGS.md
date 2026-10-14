# Recorded gesture and focus findings

Recorded October 14, 2026 on one Apple Silicon Mac (macOS 26.6.2, build 25G83)
and one paired iPhone through consumer wireless iPhone Mirroring. The adapter
used phone-harness main `8ad9a8b` (0.3.2). The native source was built with SDK
26.5. This is a summarized lab record; local screenshots, phone text and
application-activity logs have intentionally not been published.

## Phone effects

The initial matrix covered 43 cases, including variants and historical cases
re-evaluated from saved evidence. A separate no-front pass recorded 38 actions.
"Verified" means the phone visibly performed the expected action, checked
with screenshots/OCR. A successful API return was never sufficient.

| Input | Prepared path | Without front-process preparation |
| --- | --- | --- |
| Single tap | Verified with minimal 248-byte records | Verified twice across the passes, plus three menu cancellations |
| Double tap | Verified with four minimal records | Not tested |
| Long press | Verified: context menu | Verified three times; cancelled without selecting a menu item |
| Home-page horizontal swipe | Left and right verified | Three left/right pairs verified |
| Vertical list flick/drag | No effect with fast, slow and native variants | Fast flick and slow drag still had no effect |
| Continuous scroll, both axes/signs | No effect on tested list, Maps and Home surfaces | Not retested |
| Pinch/magnify in and out | Verified on Maps using trackpad-sender gesture events | Not tested |
| Rotate | Worked once, failed five subsequent attempts | Not tested; unresolved |
| Translate/two-finger pan | No effect | Not tested |
| Home / Cmd+1 | Verified native and in-process paths | Both verified; eight in-process Home actions in follow-up |
| App switcher / Cmd+2 | Verified | Three repetitions verified |
| Spotlight / Cmd+3 | Verified | Four repetitions verified |
| Keystroke text entry | Verified | Four repetitions verified |
| Rapid repeated backspace | No matching prepared control | One key lost in each four-key sequence, three repetitions |
| Backspace with 250 ms gaps | Not retested | Five of five keys registered, test text cleared |
| Arbitrary independent touch contacts | No implementation | No implementation |

The spaced-backspace result supports a timing explanation; it does not prove
preparation is unrelated because the prepared rapid-key control was not run.
The clipboard paste path was not exercised. Magnify/rotate/translate are
semantic trackpad gestures, not independently positioned touch contacts.

## Focus measurements

The prepared baseline calls `_SLPSSetFrontProcessWithOptions(..., 0x200)`
before posting. All 18 measured steps in the initial matrix that started from
a non-Mirroring baseline activated Mirroring. Activation arrived about
0.9–9 ms after preparation began, before or within 2 ms after the first post.
AX focus samples reflected the switch 15–38 ms later. Mirroring could remain
off-Space and invisible while stealing keyboard focus.

The no-front follow-up ran with the user's Chrome window on one Space and
Mirroring on another. Across 38 marked actions there were:

- Zero actual preparation calls; 218 calls replaced with marker-only stubs.
- 494 targeted event posts.
- Zero app activation notifications throughout the observation window.
- No observed AX focus change, on-screen window-order change, or active Space
  change during the action spans or the following 1.5 seconds.

The context kept the same event encoding, key records, timing, liveness guards
and posting calls. Nothing activated or restored the previous Mac app. In
the prepared baseline, focus remained on Mirroring until a later activation
of another app; no automatic restoration was implemented. Those elapsed
intervals are not gesture durations.

## Limits and remaining work

- The no-front observation covered about six minutes on one Mac and phone.
  Active-Space and already-focused Mirroring behavior were not exercised.
- The observer requests 20 ms polling; actual intervals vary with AX calls
  and scheduling. A short AX transition can be missed. App activation is also
  observed through notifications. Check each run's maximum sample gap.
- On-screen window order cannot reveal reordering inside an inactive Space.
- Cursor samples can change because the user moves their mouse. The dispatch
  paths contain no cursor warp or global input post.
- A boolean based on OCR/pixel differences produced false movement reports.
  Saved screenshots resolved those cases; list contents had not moved.
- After Mac sleep, the existing session guard once reported `ready` while the
  window showed `iPhone in Use` / `Connection Interrupted`. The session later
  reconnected by itself. No guard was disabled or changed in this experiment.
- `ensure_device()` and explicit activation remain separate focus-taking
  paths. Skipping input preparation does not make reconnecting focus-free.
- Private APIs and the event-record ABI may change between macOS versions;
  the helper checks the generated record length before dispatch.

These findings justify further opt-in testing. The experiment does not
promote new production capabilities or change the backend's defaults.
