"""Pytest loads this on its own. Unittest does not; test_cloud installs the
same recorder at import, and its CLI subprocesses load tests/support/sitecustomize.py.
"""
import sys
from pathlib import Path

_SUPPORT = Path(__file__).resolve().parent / "support"
if str(_SUPPORT) not in sys.path:
    sys.path.insert(0, str(_SUPPORT))

import browser_recorder
import pytest

browser_recorder.install()


@pytest.fixture(autouse=True)
def block_real_browser(monkeypatch):
    """No test may launch a browser. `recorder.urls()` is what was opened."""
    browser_recorder.install(monkeypatch)
    browser_recorder.recorder.reset()
    yield browser_recorder.recorder
