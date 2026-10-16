# Android on the desk

Drives an Android phone over adb, from macOS, Linux or Windows.

## Needs

- Python 3.10+ and adb (Android platform-tools):
  `brew install android-platform-tools` (macOS),
  `apt install adb` (Debian/Ubuntu), `winget install Google.PlatformTools`
  (Windows). Optional: `scrcpy` for a live mirror window.
- Developer options on the phone: Settings > About phone > tap
  **Build number** 7 times.

## Steps only the user can do

- **USB:** Developer options > **USB debugging** on, plug in, tap **Allow**
  ("Always allow from this computer").
- **Wi-Fi** (Android 11+, same network as the computer): Developer options >
  **Wireless debugging** > **Pair device with pairing code**, then
  `phone-harness android pair CODE` with the 6 digits. The phone is remembered.

## Check

`phone-harness config set platform android` to make it the default, then
`phone-harness doctor android`. `locked` means unlock the phone; the harness
never types a PIN.

No phone? Use a [cloud phone](cloud.md).
