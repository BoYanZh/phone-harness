# phone-harness for Hermes Agent

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets
Hermes drive a real phone with one tool, `phone_exec`. The model writes a short
Python script with the phone-harness helpers pre-imported, and the plugin runs
it on the phone and returns what it printed.

```text
phone_exec(code='''
ensure_device()
open_app("Weather")
wait_stable()
print([o["text"] for o in ocr()][:12])
''')
```

When the installed `phone_harness` package contains `SKILL.md`, the plugin
registers that file as the skill `phone-harness:phone-harness`, and passes
Hermes the description and frontmatter from the file (Hermes does not re-read
them for the skill list). If the package does not ship `SKILL.md`, no skill is
registered. The agent reads the helpers from that skill.

## Install

```bash
hermes plugins install ShawnPana/phone-harness/integrations/hermes
```

Hermes installs the `phone-harness` package into its own environment and asks
whether to enable the plugin. Tools load per session, so start a new chat.
Restart the Hermes desktop app after installing or updating the plugin.
Each script runs as `python -m phone_harness.run` on Hermes's interpreter: the
copy Hermes installed, kept apart from a `phone-harness` command on `PATH`.

phone-harness chooses the phone (an attached cloud phone, otherwise its
default). To force the phone on this machine, export `PHONE_HARNESS_PLATFORM`
as `ios` or `android` in the environment Hermes runs in. iPhone control needs
Accessibility and Screen Recording for the app that runs Hermes (the Hermes
desktop app, or your terminal for `hermes` in a shell). Android needs adb; a
cloud phone needs `phone-harness cloud login`. [install.md](../../install.md)
covers each.

## Settings

This plugin has no settings of its own.

- Telemetry follows phone-harness's own config.
- `PHONE_HARNESS_PLATFORM`, when you have exported it, is passed through.
- The agent workspace is one folder, `agent-workspace`, in Hermes's plugin
  data directory. If `PH_AGENT_WORKSPACE` is already set, that path is used
  instead. Each script receives it as `PH_AGENT_WORKSPACE`. If
  `agent_helpers.py` is missing, the packaged default is copied in; a file
  that is already there is left as it is.

## How it works

1. The model calls `phone_exec(code, timeout_s=300)`.
2. A `pre_tool_call` hook reads the script. Phone helpers and plain data
   handling go straight through. A script that reaches past the phone goes to
   Hermes's approval prompt first (see below).
3. Subagents in this process take turns on the phone, via an in-process lock.
   After 120 s the tool reports the phone as busy.
4. The plugin pipes the script on stdin to the phone-harness Hermes installed,
   with `PH_AGENT_WORKSPACE` and `PH_CLIENT=hermes`. It does not set a platform
   and does not turn telemetry off.
5. The result is what the script printed, plus the exit code, the tail of
   stderr, and any screenshot paths it printed (the agent opens those with
   `vision_analyze`). A script past its timeout is killed with everything it
   started.

## Approvals

phone-harness scripts are Python, so they can do anything Python can on this
computer. The hook sends a script to Hermes's approval prompt when it:

- imports anything beyond `phone_harness` and a small set of data modules
  (`json`, `re`, `time`, `math`, `datetime`, `collections`, ...);
- calls `open`, `exec`, `eval`, `compile`, `__import__`, `breakpoint` or `input`;
- touches dunder names (`__builtins__`, `__globals__`, `"__self__"`, ...);
- runs an adb `shell()` command on the phone.

Denial, timeout or no one to ask (cron, the messaging gateway) fails closed.
This is a heuristic gate, not a sandbox. Taps, typing and app launches never
ask: what to do on the phone is the skill's consent rule, which tells the agent
to ask before sending, posting, buying, deleting or changing settings.

## What it touches

- Runs model-written Python locally through the phone-harness Hermes installed.
- Taps and types into your real phone, with your apps and accounts.
- Screenshots are PNG files in the system temp folder.
- A cloud phone streams its screen to Phone Harness Cloud, bills by the minute,
  and uses phone-harness's own cloud sign-in.
- Hermes adds the plugin's toolset to every platform that has a saved tool
  selection, messaging platforms included. Turn it off there with
  `hermes tools` unless you want people who can message your bot to drive your
  phone.

## Limits

- Screenshots are not attached to the model's context automatically; the
  agent views them with `vision_analyze`.
- No password-vault integration: the agent never types passwords, PINs or 2FA
  codes. Hand the phone back to the user for those.
- `back()` is Android-only; on an iPhone the agent taps the app's back arrow.
