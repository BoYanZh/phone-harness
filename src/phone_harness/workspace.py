"""Default helpers an agent can edit, and how an install gets them.

The repo's ``agent-workspace/agent_helpers.py`` is the only copy of the
default helper. The wheel force-includes that file at
``phone_harness/agent_workspace/agent_helpers.py``.

Resolution:

  PH_AGENT_WORKSPACE if set, else
  <repo>/agent-workspace when this package sits in a git checkout
  (that directory contains ``.git`` and ``agent-workspace/``), else
  ~/.phone-harness/agent-workspace, unless ~/.phone-harness is itself a
  git checkout, in which case
  $XDG_DATA_HOME/phone-harness/agent-workspace, or
  ~/.local/share/phone-harness/agent-workspace when XDG_DATA_HOME is unset.

The last two are outside any install prefix, so a pip or uv upgrade does
not wipe helpers an agent added. The same path is used on every OS.

A missing directory is created from that default. An existing directory
is never modified, even if it is only partly filled in. To get a fresh
default, delete the workspace folder; it is recreated on the next run.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_SKIPPED_NAMES = {".env", ".DS_Store", "__pycache__"}


def repo_root():
    """Two parents above this package.

    In a checkout that is the repo root. In a pip or uv install it lands
    inside the prefix (``lib/pythonX.Y``), which is not a place to store
    helpers: that tree is replaced on upgrade.
    """
    return Path(__file__).resolve().parent.parent.parent


def _is_git_checkout(path):
    """True when ``path`` is a git work tree. ``.git`` may be a file."""
    return (path / ".git").exists()


def _user_agent_workspace():
    """Per-user workspace, never inside an install prefix.

    ``~/.phone-harness`` is the canonical clone (see install.md). When that
    path is a checkout, its ``agent-workspace/`` belongs to the checkout;
    an install uses the data directory instead of writing into the clone.
    """
    canonical = Path.home() / ".phone-harness"
    if not _is_git_checkout(canonical):
        return canonical / "agent-workspace"
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "phone-harness" / "agent-workspace"


def resolved_agent_workspace():
    """Where agent helpers live.

    ``PH_AGENT_WORKSPACE`` if set (an empty value is honoured; the default
    applies only when the variable is unset). Otherwise the checkout's
    ``agent-workspace/`` when this package is running from one, otherwise
    the per-user directory from ``_user_agent_workspace``.
    """
    if "PH_AGENT_WORKSPACE" in os.environ:
        return Path(os.environ["PH_AGENT_WORKSPACE"])
    root = repo_root()
    if _is_git_checkout(root) and (root / "agent-workspace").is_dir():
        return root / "agent-workspace"
    return _user_agent_workspace()


def packaged_defaults():
    """Default workspace files.

    An installed wheel has them inside the package. A checkout does not;
    the repo directory is that same source of truth, not a second copy.
    """
    from importlib import resources
    try:
        packaged = resources.files("phone_harness") / "agent_workspace"
        if packaged.is_dir():
            return packaged
    except (ImportError, OSError, TypeError):
        pass
    fallback = repo_root() / "agent-workspace"
    if fallback.is_dir():
        return fallback
    return None


def _skipped(name):
    """Secrets and bytecode are not defaults, even if they sit beside them."""
    return name in _SKIPPED_NAMES or name.endswith((".pyc", ".pyo"))


def _children(source):
    items = [child for child in source.iterdir() if not _skipped(child.name)]
    items.sort(key=lambda child: child.name)
    return items


def _same_dir(source, dest):
    resolve = getattr(source, "resolve", None)
    if resolve is None:
        return False
    try:
        return Path(resolve()).resolve() == Path(dest).resolve()
    except (OSError, TypeError, ValueError):
        return False


def _copy_into(source, dest):
    """Copy ``source``'s children into the existing directory ``dest``."""
    for child in _children(source):
        target = dest / child.name
        if child.is_dir():
            target.mkdir()
            _copy_into(child, target)
        elif child.is_file():
            target.write_bytes(child.read_bytes())


def ensure_agent_workspace(dest):
    """Create ``dest`` from the defaults when it is absent.

    If ``dest`` already exists — a checkout, a partial directory, a file —
    it is returned untouched.
    """
    dest = Path(dest)
    if dest.exists():
        return dest
    source = packaged_defaults()
    if source is None or _same_dir(source, dest):
        return dest
    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=parent))
    try:
        _copy_into(source, tmp)
        os.rename(tmp, dest)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        # Another process created it first. Keep that directory as it is.
        if dest.is_dir():
            return dest
        raise
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


_warned = False


def _warn_once(dest, exc):
    global _warned
    if _warned:
        return
    _warned = True
    print(f"phone-harness: could not create agent workspace {dest} ({exc})",
          file=sys.stderr)


def agent_workspace():
    """The resolved workspace, seeded from the defaults when it is missing."""
    dest = resolved_agent_workspace()
    try:
        return ensure_agent_workspace(dest)
    except OSError as exc:
        _warn_once(dest, exc)
        return dest
