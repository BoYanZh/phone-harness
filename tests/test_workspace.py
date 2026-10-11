"""Agent workspace seeding: first run copies defaults, an existing one is left alone.

    python -m unittest discover tests
"""
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEFAULT_HELPERS = ROOT / "agent-workspace" / "agent_helpers.py"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from phone_harness.workspace import (  # noqa: E402
    ensure_agent_workspace,
    update_workspace,
)


def _run(code, workspace, home, *args):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["PH_AGENT_WORKSPACE"] = str(workspace)
    env["PHONE_HARNESS_HOME"] = str(home)
    env["PHONE_HARNESS_PLATFORM"] = "android"
    env["PHONE_HARNESS_TELEMETRY"] = "0"
    env.pop("PHONE_HARNESS_SESSION", None)
    cmd = [sys.executable, "-c", code] if code is not None else [
        sys.executable, "-m", "phone_harness.run", *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


class AgentWorkspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.ws = Path(self.tmp.name) / "workspace"

    def test_update_on_a_missing_workspace_still_seeds_it(self):
        r = _run(None, self.ws, self.home, "workspace", "update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.ws / "agent_helpers.py").read_bytes(),
                         DEFAULT_HELPERS.read_bytes())
        self.assertNotIn("differs", r.stdout)

    def test_a_missing_workspace_is_seeded_from_the_defaults(self):
        r = _run("import phone_harness.config", self.ws, self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        seeded = self.ws / "agent_helpers.py"
        self.assertTrue(seeded.is_file(), r.stderr)
        self.assertEqual(seeded.read_bytes(), DEFAULT_HELPERS.read_bytes())

        r = _run(
            "import phone_harness.helpers as h\n"
            "assert callable(h.tap_icon)\n",
            self.ws, self.home)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_an_existing_workspace_is_left_untouched(self):
        self.ws.mkdir()
        custom = self.ws / "agent_helpers.py"
        custom.write_text("# agent edited this\nVALUE = 'custom'\n")
        extra = self.ws / "my_helpers.py"
        extra.write_text("mine = True\n")
        before = {p.name: p.read_bytes() for p in self.ws.iterdir()}

        r = _run("import phone_harness.config", self.ws, self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        after = {p.name: p.read_bytes() for p in self.ws.iterdir()}
        self.assertEqual(after, before)
        self.assertNotIn(b"def tap_icon", custom.read_bytes())

    def test_a_partial_workspace_is_not_filled_in(self):
        self.ws.mkdir()
        (self.ws / "my_helpers.py").write_text("mine = True\n")
        self.assertEqual(ensure_agent_workspace(self.ws), self.ws)
        self.assertFalse((self.ws / "agent_helpers.py").exists())
        self.assertEqual((self.ws / "my_helpers.py").read_text(), "mine = True\n")

    def test_workspace_update_adds_only_missing_files(self):
        source = Path(self.tmp.name) / "defaults"
        nested = source / "notes"
        nested.mkdir(parents=True)
        (source / "agent_helpers.py").write_text("default helpers\n")
        (source / "extra.py").write_text("extra\n")
        (nested / "nested.py").write_text("nested\n")
        (source / ".env").write_text("SECRET=1\n")

        self.ws.mkdir()
        (self.ws / "agent_helpers.py").write_text("# agent edited this\n")
        (self.ws / "my_helpers.py").write_text("mine = True\n")

        added, differ = update_workspace(self.ws, source)
        self.assertEqual(added, ["extra.py", "notes/nested.py"])
        self.assertEqual(differ, ["agent_helpers.py"])
        self.assertEqual((self.ws / "agent_helpers.py").read_text(), "# agent edited this\n")
        self.assertEqual((self.ws / "my_helpers.py").read_text(), "mine = True\n")
        self.assertEqual((self.ws / "extra.py").read_text(), "extra\n")
        self.assertEqual((self.ws / "notes" / "nested.py").read_text(), "nested\n")
        self.assertFalse((self.ws / ".env").exists())

        added, differ = update_workspace(self.ws, source)
        self.assertEqual(added, [])
        self.assertEqual(differ, ["agent_helpers.py"])
        self.assertEqual((self.ws / "agent_helpers.py").read_text(), "# agent edited this\n")

    def test_update_command_adds_a_missing_default_and_reports_edits(self):
        self.ws.mkdir()
        (self.ws / "my_helpers.py").write_text("mine = True\n")
        r = _run(None, self.ws, self.home, "workspace", "update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("added agent_helpers.py", r.stdout)
        self.assertNotIn("differs", r.stdout)
        self.assertNotIn("my_helpers.py", r.stdout)
        self.assertEqual((self.ws / "agent_helpers.py").read_bytes(),
                         DEFAULT_HELPERS.read_bytes())
        self.assertEqual((self.ws / "my_helpers.py").read_text(), "mine = True\n")

        (self.ws / "agent_helpers.py").write_text("# agent edited this\n")
        r = _run(None, self.ws, self.home, "workspace", "update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("differs agent_helpers.py", r.stdout)
        self.assertNotIn("added", r.stdout)
        self.assertEqual((self.ws / "agent_helpers.py").read_text(), "# agent edited this\n")

    def test_a_checkout_workspace_is_not_rewritten(self):
        before = DEFAULT_HELPERS.read_bytes()
        names = sorted(p.name for p in DEFAULT_HELPERS.parent.iterdir())
        env = os.environ.copy()
        env.pop("PH_AGENT_WORKSPACE", None)
        env["PYTHONPATH"] = str(SRC)
        env["PHONE_HARNESS_HOME"] = str(self.home)
        env["PHONE_HARNESS_PLATFORM"] = "android"
        env["PHONE_HARNESS_TELEMETRY"] = "0"
        r = subprocess.run(
            [sys.executable, "-m", "phone_harness.run", "workspace", "update"],
            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no missing default files", r.stdout)
        self.assertNotIn("differs", r.stdout)
        self.assertNotIn("added", r.stdout)
        self.assertEqual(DEFAULT_HELPERS.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in DEFAULT_HELPERS.parent.iterdir()), names)

    def test_seed_skips_a_dotenv_beside_the_defaults(self):
        source = Path(self.tmp.name) / "defaults"
        source.mkdir()
        (source / "agent_helpers.py").write_text("default helpers\n")
        (source / ".env").write_text("SECRET=1\n")
        with mock.patch("phone_harness.workspace.packaged_defaults", return_value=source):
            ensure_agent_workspace(self.ws)
        self.assertEqual((self.ws / "agent_helpers.py").read_text(), "default helpers\n")
        self.assertFalse((self.ws / ".env").exists())


def _build_wheel(directory):
    """Build a wheel of this tree. Prefer the installed hatchling; CI may not
    have it, and then pip's isolated build downloads the backend."""
    cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps",
           "--wheel-dir", str(directory), str(ROOT)]
    r = subprocess.run([*cmd, "--no-build-isolation"], capture_output=True, text=True)
    if r.returncode != 0:
        r = subprocess.run(cmd, capture_output=True, text=True)
    return r


class PackagedWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name)
        r = _build_wheel(cls.out)
        if r.returncode != 0:
            cls._tmp.cleanup()
            raise AssertionError(r.stderr or r.stdout)
        wheels = list(cls.out.glob("*.whl"))
        if len(wheels) != 1:
            cls._tmp.cleanup()
            raise AssertionError(f"expected one wheel, got {wheels}")
        cls.wheel = wheels[0]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_wheel_ships_agent_workspace(self):
        with zipfile.ZipFile(self.wheel) as zf:
            names = zf.namelist()
            packaged = "phone_harness/agent_workspace/agent_helpers.py"
            self.assertIn(packaged, names)
            self.assertEqual(zf.read(packaged), DEFAULT_HELPERS.read_bytes())
            self.assertTrue(any(name.endswith("phone_harness/SKILL.md") or
                                name == "phone_harness/SKILL.md" for name in names))
            self.assertFalse(any(name.endswith("/.env") or name == ".env"
                                 for name in names))

    def test_an_installed_wheel_seeds_without_clobbering_edits(self):
        """Install the wheel with pip --target (no venv; CI images vary) and
        import it with only that tree on PYTHONPATH, so the checkout cannot
        supply agent-workspace/."""
        target = self.out / "install"
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--no-deps",
             "--target", str(target), str(self.wheel)],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        self.assertTrue((target / "phone_harness" / "agent_workspace" /
                         "agent_helpers.py").is_file())

        def installed(workspace, code=None, *args):
            env = os.environ.copy()
            env["PYTHONPATH"] = str(target)
            env["PH_AGENT_WORKSPACE"] = str(workspace)
            env["PHONE_HARNESS_HOME"] = str(self.out / "home")
            env["PHONE_HARNESS_PLATFORM"] = "android"
            env["PHONE_HARNESS_TELEMETRY"] = "0"
            env.pop("PHONE_HARNESS_SESSION", None)
            cmd = [sys.executable, "-c", code] if code is not None else [
                sys.executable, "-m", "phone_harness.run", *args]
            return subprocess.run(cmd, capture_output=True, text=True,
                                  env=env, cwd=self.out)

        ws = self.out / "seeded"
        r = installed(ws,
                      "import phone_harness.helpers as h\n"
                      "assert callable(h.tap_icon)\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((ws / "agent_helpers.py").read_bytes(),
                         DEFAULT_HELPERS.read_bytes())

        (ws / "agent_helpers.py").write_text("VALUE = 'custom'\n")
        (ws / "my_helpers.py").write_text("mine = True\n")
        r = installed(ws,
                      "import phone_harness.helpers as h\n"
                      "assert h.VALUE == 'custom'\n"
                      "assert not hasattr(h, 'tap_icon')\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((ws / "agent_helpers.py").read_text(), "VALUE = 'custom'\n")
        self.assertEqual((ws / "my_helpers.py").read_text(), "mine = True\n")

        partial = self.out / "partial"
        partial.mkdir()
        (partial / "agent_helpers.py").write_text("VALUE = 'kept'\n")
        r = installed(partial, None, "workspace", "update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("differs agent_helpers.py", r.stdout)
        self.assertNotIn("added", r.stdout)
        self.assertEqual((partial / "agent_helpers.py").read_text(), "VALUE = 'kept'\n")

        empty = self.out / "empty"
        empty.mkdir()
        (empty / "my_helpers.py").write_text("mine = True\n")
        r = installed(empty, None, "workspace", "update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("added agent_helpers.py", r.stdout)
        self.assertNotIn("my_helpers.py", r.stdout)
        self.assertEqual((empty / "agent_helpers.py").read_bytes(),
                         DEFAULT_HELPERS.read_bytes())
        self.assertEqual((empty / "my_helpers.py").read_text(), "mine = True\n")
