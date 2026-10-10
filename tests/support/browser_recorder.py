"""A stand-in for webbrowser that records URLs and never launches anything.

The CLI under test is a fresh Python process, so the patch has to be installed
there too (see sitecustomize.py). Both processes share the log file.
"""
import sys
import tempfile
import webbrowser
from pathlib import Path

LOG = Path(tempfile.gettempdir()) / "phone-harness-tests-browser.log"


class Recorder:
    def urls(self):
        if not LOG.exists():
            return []
        return [line for line in LOG.read_text().splitlines() if line]

    def reset(self):
        LOG.write_text("")

    def open(self, url, new=0, autoraise=True):
        with LOG.open("a") as fh:
            fh.write(f"{url}\n")
        return True

    def open_new(self, url):
        return self.open(url, new=1)

    def open_new_tab(self, url):
        return self.open(url, new=2)


recorder = Recorder()


def _patched_open_browser(url):
    """Same gate as phone_harness.cloud._open_browser, pointed at the recorder."""
    from phone_harness import config
    if not config.get("cloud.watch"):
        return False
    try:
        return bool(recorder.open(url))
    except Exception:
        return False


def install(monkeypatch=None):
    """Replace webbrowser.open/open_new/open_new_tab and the cloud wrapper."""
    src = str(Path(__file__).resolve().parents[2] / "src")
    if src not in sys.path:
        sys.path.insert(0, src)

    def set_(obj, name, value):
        if monkeypatch is not None:
            monkeypatch.setattr(obj, name, value)
        else:
            setattr(obj, name, value)

    set_(webbrowser, "open", recorder.open)
    set_(webbrowser, "open_new", recorder.open_new)
    set_(webbrowser, "open_new_tab", recorder.open_new_tab)
    from phone_harness import cloud
    set_(cloud.webbrowser, "open", recorder.open)
    set_(cloud.webbrowser, "open_new", recorder.open_new)
    set_(cloud.webbrowser, "open_new_tab", recorder.open_new_tab)
    set_(cloud, "_open_browser", _patched_open_browser)
