"""phone-harness for Hermes Agent: one ``phone_exec`` tool.

The model writes a Python script. The plugin pipes it on stdin to the phone-harness Hermes
installed from this plugin's python_dependencies, which runs it with the phone helpers
pre-imported against the user's phone and hands stdout back as the tool result. Each call is
a fresh interpreter; the phone and the agent workspace persist.

Safety, in order of what it protects:
- A ``pre_tool_call`` hook sends any script that reaches past the phone (imports beyond a small set
  of data modules, file and eval builtins, dunder introspection, adb ``shell()``) to Hermes's
  approval prompt. It is a heuristic gate, not a sandbox.
- One script drives the phone at a time, across subagents in this process.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("phone_harness.hermes")

PLUGIN = "phone-harness"
DEFAULT_TIMEOUT_S, MIN_TIMEOUT_S, MAX_TIMEOUT_S = 300, 5, 1800
LOCK_WAIT_S = 120
OUTPUT_CAP, STDERR_CAP = 50_000, 4_000

_THREAD_LOCK = threading.Lock()   # subagents in this process take turns on the phone

DESCRIPTION = (
    "Drive the user's real phone with phone-harness: their iPhone through the Mac's iPhone Mirroring "
    "window, an Android over adb, or a Phone Harness Cloud phone. `code` runs as Python with the phone "
    "helpers pre-imported; print() whatever you need back. Each call is a fresh interpreter (variables "
    "do not persist); the phone keeps its state. Start with ensure_device(); it covers iPhone "
    "Mirroring, Android, and a cloud phone. Read what is on screen with screen(). Ask the user before "
    "anything outward-facing or hard to undo (sending, posting, buying, deleting, changing settings). "
    "Never type passwords, PINs or 2FA codes. Read the skill phone-harness:phone-harness for the helpers."
)

SCHEMA = {
    "name": "phone_exec",
    "description": DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python using the pre-imported phone-harness helpers. print() what you need back.",
            },
            "timeout_s": {
                "type": "integer",
                "default": DEFAULT_TIMEOUT_S,
                "description": f"Max seconds for the script (default {DEFAULT_TIMEOUT_S}, max {MAX_TIMEOUT_S}).",
            },
        },
        "required": ["code"],
    },
}

# ---------------------------------------------------------------------------------------------
# Approval gate: what a phone script may do without asking.

# Modules a phone script can import without touching the computer beyond the phone.
SAFE_MODULES = frozenset({
    "phone_harness", "json", "re", "time", "math", "random", "string", "datetime", "itertools",
    "functools", "collections", "statistics", "textwrap", "difflib", "typing", "dataclasses", "enum",
    "decimal", "fractions", "pprint", "unicodedata", "operator", "heapq", "bisect", "copy",
})
# Builtins that read or write files, run arbitrary code, or wait on a terminal; shell() is
# phone-harness's adb shell.
RISKY_CALLS = frozenset({"open", "exec", "eval", "compile", "__import__", "breakpoint", "input", "shell"})
_DUNDER = re.compile(r"^__\w+__$")
_FM_END = re.compile(r"\n---\s*(?:\n|$)")


def approval_reason(code: str) -> str | None:
    """Why this script needs the user's OK before it runs, or None when it only drives the phone.

    Phone helpers and plain data handling pass. Host access (imports outside SAFE_MODULES, file and
    eval builtins, dunder introspection) and raw adb shell commands do not. A heuristic, not a sandbox.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None  # it cannot run; the SyntaxError comes back as the result
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in SAFE_MODULES:
                    return f"imports {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in SAFE_MODULES:
                return f"imports from {node.module or '.'}"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in RISKY_CALLS:
            return f"calls {node.func.id}()"
        elif isinstance(node, ast.Attribute) and _DUNDER.match(node.attr):
            return f"uses {node.attr}"
        elif isinstance(node, ast.Name) and _DUNDER.match(node.id):
            return f"uses {node.id}"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _DUNDER.match(node.value):
            return f"references {node.value}"
    return None


def _on_pre_tool_call(tool_name: str = "", args: dict | None = None, task_id: str = "", **kwargs):
    if tool_name != "phone_exec":
        return None
    reason = approval_reason(str((args or {}).get("code") or ""))
    if reason is None:
        return None
    return {"action": "approve",
            "message": f"phone_exec wants to run a script that {reason}, which reaches past the phone "
                       "onto this computer."}

# ---------------------------------------------------------------------------------------------
# The copy Hermes installed, and where its scripts keep notes.


def _data_dir() -> Path:
    """Hermes's data folder for this plugin: follows the active profile, survives plugin updates."""
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir(PLUGIN))
    except Exception:  # outside Hermes (unit tests)
        return Path.home() / ".hermes" / "plugin-data" / PLUGIN


def _package_dir() -> Path | None:
    """Directory of the phone_harness Hermes installed, or None when it cannot be imported."""
    spec = importlib.util.find_spec("phone_harness")
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).resolve().parent


def _command() -> list[str] | None:
    """argv for the phone-harness Hermes installed, on Hermes's own interpreter."""
    if _package_dir() is None:
        return None
    return [sys.executable, "-m", "phone_harness.run"]


def _available() -> bool:
    return _command() is not None


def _workspace() -> Path:
    """Where notes live. An inherited PH_AGENT_WORKSPACE, else one folder in Hermes's plugin data."""
    inherited = os.environ.get("PH_AGENT_WORKSPACE", "").strip()
    if inherited:
        return Path(inherited).expanduser()
    return _data_dir() / "agent-workspace"


def _seed_workspace(workspace: Path) -> None:
    """Copy the packaged agent_helpers.py in when that file is missing.

    Hermes's plugin-data folder is created up front. An empty directory
    makes phone-harness skip its first-run seed, so the workspace can end
    up with no helpers. Existing files are never overwritten.
    """
    try:
        from phone_harness.workspace import seed_missing_files
    except ImportError as exc:
        log.warning("phone-harness: could not seed the agent workspace: %s", exc)
        workspace.mkdir(parents=True, exist_ok=True)
        return
    seed_missing_files(workspace)
    if not workspace.is_dir():
        workspace.mkdir(parents=True, exist_ok=True)


def _env(workspace: Path) -> dict:
    """The script inherits Hermes's environment. Platform and telemetry stay phone-harness's own
    config: an exported PHONE_HARNESS_PLATFORM is left as it is, and this plugin does not set one."""
    env = dict(os.environ)
    env["PH_AGENT_WORKSPACE"] = str(workspace)
    env["PH_CLIENT"] = "hermes"
    return env

# ---------------------------------------------------------------------------------------------
# One script on the phone at a time (subagents in this process).


class PhoneBusy(Exception):
    pass


@contextlib.contextmanager
def _phone_lock(wait_s: float):
    """Subagents take turns. Raises PhoneBusy after wait_s."""
    if not _THREAD_LOCK.acquire(timeout=max(wait_s, 0.1)):
        raise PhoneBusy
    try:
        yield
    finally:
        _THREAD_LOCK.release()

# ---------------------------------------------------------------------------------------------
# The tool.


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the runner and everything it spawned (it runs in its own process group)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def _result(success: bool, **fields) -> str:
    return json.dumps({"success": success, **{k: v for k, v in fields.items() if v not in (None, "", [])}},
                      ensure_ascii=False)


def _timeout(value) -> int:
    try:
        return max(MIN_TIMEOUT_S, min(int(value or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


_PNG = re.compile(r"(/[^\s'\"]+\.png)\b")


def phone_exec(args: dict, **kwargs) -> str:
    code = str((args or {}).get("code") or "")
    if not code.strip():
        return _result(False, error="No code provided. Start with: ensure_device(); print(screen_info())")
    cmd = _command()
    if cmd is None:
        return _result(False, error="phone-harness is not installed in Hermes's environment. "
                                    "Reinstall this plugin so Hermes installs it.")
    timeout = _timeout((args or {}).get("timeout_s"))
    workspace = _workspace()
    group: dict[str, Any] = ({"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
                             if os.name == "nt" else {"start_new_session": True})
    try:
        _seed_workspace(workspace)
        with _phone_lock(LOCK_WAIT_S):
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                env=_env(workspace), cwd=str(workspace), **group,
            )
            try:
                out, err = proc.communicate(input=code, timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.communicate(timeout=10)
                return _result(False, workspace=str(workspace),
                               error=f"phone_exec timed out after {timeout}s. Split the work into smaller "
                                     f"scripts or raise timeout_s (max {MAX_TIMEOUT_S}).")
    except PhoneBusy:
        return _result(False, error=f"The phone is busy with another phone_exec script (waited {LOCK_WAIT_S}s). "
                                    "Try again when it finishes.")
    except OSError as e:
        return _result(False, error=f"Failed to launch phone-harness: {e}")

    out = out or ""
    shots = [p for p in dict.fromkeys(_PNG.findall(out)) if os.path.isfile(p)]
    return _result(proc.returncode == 0, exit_code=proc.returncode, output=out[-OUTPUT_CAP:],
                   stderr=(err or "").strip()[-STDERR_CAP:], screenshots=shots, workspace=str(workspace))

# ---------------------------------------------------------------------------------------------
# Registration.


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Flat YAML scalars at the top of a SKILL.md, or {} when there is no frontmatter.

    Hermes stores the description and frontmatter ``register_skill`` is given and does not
    re-read the file for the skill list, so leaving them off shows an empty skill.
    """
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}
    end = _FM_END.search(text[3:])
    if not end:
        return {}
    data: dict[str, str] = {}
    for line in text[3:end.start() + 3].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        data[key.strip()] = value
    return data


def _packaged_skill() -> tuple[Path, str, dict[str, str]] | None:
    """SKILL.md inside the installed phone_harness package, read once. None if it does not ship one."""
    root = _package_dir()
    if root is None:
        return None
    path = root / "SKILL.md"
    if not path.is_file():
        return None
    frontmatter = _parse_frontmatter(path.read_text(encoding="utf-8"))
    return path, frontmatter.get("description", ""), frontmatter


def _register_skill(ctx) -> None:
    skill = _packaged_skill()
    if skill is None:
        return
    path, description, frontmatter = skill
    try:
        ctx.register_skill(PLUGIN, path, description=description, frontmatter=frontmatter)
    except TypeError:  # Hermes builds that take a path and a description, and not frontmatter
        ctx.register_skill(PLUGIN, path, description=description)


def register(ctx) -> None:
    ctx.register_tool(name="phone_exec", toolset=PLUGIN, schema=SCHEMA, handler=phone_exec,
                      check_fn=_available, emoji="📱")
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    try:
        _register_skill(ctx)
    except Exception as exc:
        log.warning("phone-harness: could not register the skill: %s", exc)
