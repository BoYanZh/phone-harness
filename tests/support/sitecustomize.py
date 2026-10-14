"""Imported automatically in test subprocesses (PYTHONPATH lists this directory).

Installed before phone_harness so `cloud start`, `cloud watch`, and `cloud open`
record the URL instead of calling a real browser. The update check stays off:
a test must not call PyPI, and a failure there must not fail the suite.
Telemetry stays off so a test subprocess does not post a usage event.
"""
import os

import browser_recorder

os.environ.setdefault("PHONE_HARNESS_NO_UPDATE_CHECK", "1")
os.environ.setdefault("PHONE_HARNESS_TELEMETRY", "0")
browser_recorder.install()
