"""Imported automatically in test subprocesses (PYTHONPATH lists this directory).

Installed before phone_harness so `cloud start`, `cloud watch`, and `cloud open`
record the URL instead of calling a real browser.
"""
import browser_recorder

browser_recorder.install()
