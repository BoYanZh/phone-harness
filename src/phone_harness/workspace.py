"""Default helpers an agent can edit, and how an install gets them.

The repo's ``agent-workspace/`` is the only copy of those files. The wheel
force-includes that directory at ``phone_harness/agent_workspace/``.

Resolution is unchanged: ``PH_AGENT_WORKSPACE``, else ``<repo>/agent-workspace``.
A checkout already has that directory, so nothing is copied. When the
resolved directory does not exist, it is created from those defaults. An
existing directory is never modified, even if it is only partly filled in.
``phone-harness workspace update`` is the opt-in that copies default files
that are missing and reports the ones that differ, without touching them.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

CLI_USAGE = """Usage:
  phone-harness workspace update
      copy default files missing from the agent workspace, and list
      default files that differ, without changing them
"""

_SKIPPED_NAMES = {".env", ".DS_Store", "__pycache__"}


def repo_root():
    """Two parents above this package: the checkout root, or the install prefix."""
    return Path(__file__).resolve().parent.parent.parent


def resolved_agent_workspace():
    """``PH_AGENT_WORKSPACE`` if set, else ``<repo>/agent-workspace``.

    An empty value is honoured, matching the previous ``os.environ.get``
    fallback (the default is used only when the variable is unset).
    """
    if "PH_AGENT_WORKSPACE" in os.environ:
        return Path(os.environ["PH_AGENT_WORKSPACE"])
    return repo_root() / "agent-workspace"


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


def _merge(source, dest, rel, added, differ):
    for child in _children(source):
        rel_child = rel / child.name
        target = dest / child.name
        key = rel_child.as_posix()
        if child.is_dir():
            if target.exists() and not target.is_dir():
                differ.append(key)
                continue
            if not target.exists():
                target.mkdir()
            _merge(child, target, rel_child, added, differ)
            continue
        if not child.is_file():
            continue
        data = child.read_bytes()
        if not target.exists():
            target.write_bytes(data)
            added.append(key)
        elif target.is_file():
            if target.read_bytes() != data:
                differ.append(key)
        else:
            differ.append(key)


def update_workspace(dest, source=None):
    """Copy default files that are missing under ``dest``.

    Returns ``(added, differ)`` as forward-slash paths. Existing files are
    never written, including ones whose bytes differ from the defaults.
    """
    dest = Path(dest)
    if source is None:
        source = packaged_defaults()
    added, differ = [], []
    if source is None or _same_dir(source, dest):
        return added, differ
    if dest.exists() and not dest.is_dir():
        raise NotADirectoryError(dest)
    if not dest.exists():
        dest.mkdir(parents=True)
    _merge(source, dest, Path(), added, differ)
    return added, differ


def cli(args):
    if args != ["update"]:
        print(CLI_USAGE)
        return 2
    dest = resolved_agent_workspace()
    try:
        added, differ = update_workspace(dest)
    except OSError as exc:
        print(f"phone-harness: {exc}", file=sys.stderr)
        return 1
    print(dest)
    for name in added:
        print(f"added {name}")
    for name in differ:
        print(f"differs {name}")
    if not added and not differ:
        print("no missing default files")
    return 0
