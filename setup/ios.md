# Personal iPhone (iPhone Mirroring)

Drives the user's own iPhone through the Mac's iPhone Mirroring window.

## Needs

- A Mac on macOS 15 (Sequoia) or later, with the iPhone Mirroring app.
  Apple's own requirements apply: an iOS 18+ iPhone signed in to the same
  Apple ID, nearby, locked, with Bluetooth and Wi-Fi on.
- Python 3.10+ and the Mac-only dependencies, which the normal install pulls.
- Two macOS permissions for **the app that runs Python** (the terminal, or
  the agent's own app if it runs commands itself), in System Settings >
  Privacy & Security:
  - **Accessibility**: taps and typing. Takes effect at once.
  - **Screen Recording**: seeing the phone. Takes effect after that app
    restarts.
  macOS asks about the outermost app, not Python itself; `phone-harness
  doctor ios` prints its name when a permission is missing.

```bash
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
```

## Steps only the user can do

1. Open iPhone Mirroring once and finish pairing (needs the phone in hand).
2. Grant the two permissions above, then restart the app that runs Python.

## Check

`phone-harness doctor ios`. It checks each step in order and names the first
one that is missing. If it passes but taps or capture do nothing, look for a
macOS permission prompt on the Mac.

Not on a Mac, or no iPhone nearby? Use a [cloud phone](cloud.md).
