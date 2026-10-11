"""Tell the user when a newer phone-harness is out, and update when they ask.

The check runs at most once a day. It stores the time and the latest version
it saw under the cache dir (see config.py) and prints one stderr line. It
never upgrades by itself, and a network failure never fails the command.

`phone-harness update` is the only thing that changes an install: `uv tool
upgrade` for a uv tool, `git pull --ff-only` for a checkout, otherwise the
pip command printed for the user to run.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import config

CHECK_INTERVAL = 24 * 60 * 60
FETCH_TIMEOUT = 2
PYPI_URL = "https://pypi.org/pypi/phone-harness/json"
_TRUTHY = {"1", "true", "yes", "on"}


def installed_version():
    try:
        from importlib.metadata import version
        return version("phone-harness")
    except Exception:
        return ""


def _parse(value):
    nums = []
    for part in str(value).split("."):
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        if digits:
            nums.append(int(digits))
    return tuple(nums)


def is_newer(latest, current):
    """True when `latest` is a higher version than `current`."""
    a, b = _parse(latest), _parse(current)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return a > b


def _load():
    data = config._read(config.paths()["update"], {})
    latest = data.get("latest") if isinstance(data.get("latest"), str) else None
    try:
        checked = float(data.get("checked_at") or 0)
    except (TypeError, ValueError):
        checked = 0.0
    return {"checked_at": checked, "latest": latest}


def _store(checked_at, latest):
    config._write(config.paths()["update"], {"checked_at": checked_at, "latest": latest})


def fetch_latest(opener=None, timeout=FETCH_TIMEOUT):
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(
        PYPI_URL,
        headers={"Accept": "application/json", "User-Agent": "phone-harness-cli"},
    )
    with opener(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    version = (payload.get("info") or {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("PyPI returned no version")
    return version.strip()


def _try_fetch(opener=None):
    try:
        return fetch_latest(opener=opener)
    except Exception:
        return None


def announce(*, opener=None, version=None, now=None, stderr=None):
    """Compare the installed version to PyPI and print the one-line notice.

    Network errors are swallowed. A fresh cache (younger than a day) is used
    as-is, so a command does not wait on the network again.
    """
    current = installed_version() if version is None else version
    if not current:
        return
    now = time.time() if now is None else now
    stderr = stderr or sys.stderr
    cached = _load()
    latest = cached["latest"]
    if now - cached["checked_at"] >= CHECK_INTERVAL:
        fetched = _try_fetch(opener)
        latest = fetched or latest
        _store(now, latest)
    if latest and is_newer(latest, current):
        print(
            f"phone-harness {latest} is available (you have {current}): "
            "run phone-harness update",
            file=stderr,
        )


def under_test():
    """True when this process is a test run, so the suite never calls PyPI.

    Covers `python -m unittest`, pytest, and a `test_*.py` file. Some runners
    put the whole command in argv[0] (`python3 -m unittest`) instead of the
    unittest module path."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    argv0 = (sys.argv[0] if sys.argv else "").replace("\\", "/")
    if argv0.endswith(("/unittest/__main__.py", "/pytest/__main__.py", "/pytest.py")):
        return True
    base = argv0.rsplit("/", 1)[-1]
    if base in {"pytest", "py.test", "unittest"}:
        return True
    # `python3 -m unittest` as a single argv[0], not a path that merely
    # contains those letters.
    if " -m unittest" in argv0 or " -m pytest" in argv0:
        return True
    return base.startswith("test_") and base.endswith(".py")


def suppressed(environ=None, isatty=None, testing=None):
    environ = os.environ if environ is None else environ
    flag = str(environ.get("PHONE_HARNESS_NO_UPDATE_CHECK", "")).strip().lower()
    if flag in _TRUTHY:
        return True
    if isatty is None:
        try:
            isatty = sys.stderr.isatty()
        except Exception:
            isatty = False
    if not isatty:
        return True
    if testing is None:
        testing = under_test()
    return bool(testing)


def maybe_notify():
    """The CLI's check. Returns without doing work when it should stay quiet."""
    try:
        if suppressed():
            return
        announce()
    except Exception:
        return


def is_uv_tool_prefix(prefix):
    """A uv tool env lives in `.../uv/tools/<name>`."""
    parts = Path(prefix).parts
    return any(part == "uv" and parts[i + 1] == "tools" for i, part in enumerate(parts[:-1]))


def git_checkout_root(module_file):
    """The repo root when `module_file` is the checkout, not an installed copy."""
    path = Path(module_file).resolve()
    if "site-packages" in path.parts or "dist-packages" in path.parts:
        return None
    for parent in path.parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").is_file():
            return parent
    return None


def install_mode():
    """'uv', 'git', or 'pip' — which install this process is running from."""
    if is_uv_tool_prefix(sys.prefix):
        return "uv"
    if git_checkout_root(Path(__file__).resolve()):
        return "git"
    return "pip"


def _exec(argv):
    try:
        return subprocess.call(argv)
    except FileNotFoundError:
        print(f"Could not run {argv[0]}. Run this yourself: {' '.join(argv)}", file=sys.stderr)
        return 1


def cmd_update(args=None):
    args = list(args or [])
    if args:
        print("Usage: phone-harness update", file=sys.stderr)
        return 2
    mode = install_mode()
    if mode == "uv":
        print("Updating phone-harness with uv...")
        return _exec(["uv", "tool", "upgrade", "phone-harness"])
    if mode == "git":
        root = git_checkout_root(Path(__file__).resolve())
        print(f"Updating {root} with git...")
        return _exec(["git", "-C", str(root), "pull", "--ff-only"])
    print("pip install -U phone-harness")
    return 0
