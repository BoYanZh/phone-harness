"""The Hermes plugin in integrations/hermes, tested without Hermes or a phone.

It loads integrations/hermes/__init__.py by path, points its data folder at a temp
dir and swaps the phone-harness command for a plain Python that runs the script it
is given, so what is checked is the plumbing around a script: the approval gate,
the environment the script runs in, one script on the phone at a time, timeouts,
and what register() hands Hermes.

    python -m unittest discover tests
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

PLUGIN_INIT = Path(__file__).resolve().parents[1] / "integrations" / "hermes" / "__init__.py"
REPO_SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def load_plugin() -> Any:
    spec = importlib.util.spec_from_file_location("phone_harness_hermes_plugin", PLUGIN_INIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    """The slice of Hermes's PluginContext the plugin uses."""

    def __init__(self):
        self.tools, self.hooks, self.skills = {}, {}, {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = dict(toolset=toolset, schema=schema, handler=handler, **kw)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills[name] = {"path": path, "description": description, "frontmatter": dict(frontmatter or {})}


class Base(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.plugin._data_dir = lambda: self.data
        self.plugin._workspace = lambda: self.data / "agent-workspace"
        # The installed module, here a Python that runs whatever script arrives on stdin.
        self.plugin._command = lambda: [sys.executable, "-c", "import sys; exec(sys.stdin.read())"]
        self.plugin._packaged_skill = lambda: None
        self.ctx = FakeCtx()
        self.plugin.register(self.ctx)

    def run_tool(self, code, **args):
        return json.loads(self.plugin.phone_exec({"code": code, **args}))


class Registration(Base):
    def test_one_tool_and_the_approval_hook(self):
        tool = self.ctx.tools["phone_exec"]
        self.assertEqual(tool["toolset"], "phone-harness")
        self.assertEqual(tool["schema"]["name"], "phone_exec")
        self.assertTrue(tool["check_fn"]())
        self.assertIn("pre_tool_call", self.ctx.hooks)

    def test_the_tool_description_points_at_the_skill(self):
        description = self.ctx.tools["phone_exec"]["schema"]["description"]
        self.assertIn("ensure_device()", description)
        self.assertIn("screen()", description)
        self.assertIn("phone-harness:phone-harness", description)
        self.assertIn("passwords", description)
        self.assertNotIn("scroll_until", description)


class ApprovalGate(Base):
    PHONE_ONLY = [
        'ensure_device()\nprint([o["text"] for o in screen()][:10])',
        'import json, re, time\nprint(json.dumps(screen_info()))',
        'from phone_harness import helpers\nhelpers.tap_text("Weather")',
        'from collections import Counter\nprint(Counter(o["text"] for o in ocr()))',
        'def (',  # a SyntaxError cannot run, so there is nothing to approve
    ]
    PAST_THE_PHONE = [
        "import os", "import subprocess", "import os.path", "from os import path", "import Quartz",
        "from . import agent_helpers", 'open("notes.txt", "w").write("x")', '__import__("os")',
        'eval("1")', "print.__self__", 'getattr(print, "__self__")', 'shell("pm list packages")',
    ]

    def hook(self, code, tool="phone_exec"):
        return self.ctx.hooks["pre_tool_call"](tool_name=tool, args={"code": code}, task_id="t")

    def test_phone_scripts_run_without_asking(self):
        for code in self.PHONE_ONLY:
            with self.subTest(code=code):
                self.assertIsNone(self.plugin.approval_reason(code))
                self.assertIsNone(self.hook(code))

    def test_scripts_that_reach_past_the_phone_ask_first(self):
        for code in self.PAST_THE_PHONE:
            with self.subTest(code=code):
                self.assertIsNotNone(self.plugin.approval_reason(code))
                directive = self.hook(code)
                self.assertEqual(directive["action"], "approve")
                self.assertIn("phone_exec", directive["message"])

    def test_other_tools_are_left_alone(self):
        self.assertIsNone(self.hook("import os", tool="terminal"))


class Environment(Base):
    def test_inherited_env_passes_through_with_workspace_and_client(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["PYTHONHOME"] = "/somewhere"
        os.environ["PYTHONPATH"] = "/hermes"
        os.environ["PHONE_HARNESS_PLATFORM"] = "ios"
        os.environ["PHONE_HARNESS_TELEMETRY"] = "1"
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PH_AGENT_WORKSPACE"], str(self.data / "ws"))
        self.assertEqual(env["PH_CLIENT"], "hermes")
        self.assertEqual(env["PYTHONHOME"], "/somewhere")
        self.assertEqual(env["PYTHONPATH"], "/hermes")
        self.assertEqual(env["PHONE_HARNESS_PLATFORM"], "ios")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "1")

    def test_the_plugin_does_not_invent_a_platform(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.pop("PHONE_HARNESS_PLATFORM", None)
        os.environ.pop("PHONE_HARNESS_TELEMETRY", None)
        os.environ["PHONE_HARNESS_HOME"] = str(self.data)
        env = self.plugin._env(self.data / "ws")
        self.assertNotIn("PHONE_HARNESS_PLATFORM", env)
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "0")


class Workspace(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.plugin._data_dir = lambda: self.data
        self.saved = os.environ.get("PH_AGENT_WORKSPACE")
        os.environ.pop("PH_AGENT_WORKSPACE", None)
        self.addCleanup(self.restore)

    def restore(self):
        if self.saved is None:
            os.environ.pop("PH_AGENT_WORKSPACE", None)
        else:
            os.environ["PH_AGENT_WORKSPACE"] = self.saved

    def test_default_is_one_folder_in_the_plugin_data_dir(self):
        self.assertEqual(self.plugin._workspace(), self.data / "agent-workspace")

    def test_an_inherited_workspace_is_honored(self):
        os.environ["PH_AGENT_WORKSPACE"] = str(self.data / "custom")
        self.assertEqual(self.plugin._workspace(), self.data / "custom")

    def test_a_blank_inherited_workspace_is_ignored(self):
        os.environ["PH_AGENT_WORKSPACE"] = "   "
        self.assertEqual(self.plugin._workspace(), self.data / "agent-workspace")


class Command(unittest.TestCase):
    def test_runs_the_installed_module_on_hermes_interpreter(self):
        plugin = load_plugin()
        plugin._package_dir = lambda: Path("/opt/hermes/phone_harness")
        self.assertEqual(plugin._command(), [sys.executable, "-m", "phone_harness.run"])

    def test_a_missing_package_is_unavailable(self):
        plugin = load_plugin()
        plugin._package_dir = lambda: None
        self.assertIsNone(plugin._command())
        self.assertFalse(plugin._available())


class Skill(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_the_real_skill_frontmatter_has_a_description(self):
        frontmatter = self.plugin._parse_frontmatter(REPO_SKILL.read_text(encoding="utf-8"))
        self.assertEqual(frontmatter["name"], "phone-harness")
        self.assertTrue(frontmatter["description"].startswith("Control the user's phone"))

    def test_a_package_without_skill_md_registers_nothing(self):
        self.plugin._package_dir = lambda: self.root
        ctx = FakeCtx()
        self.plugin.register(ctx)
        self.assertEqual(ctx.skills, {})

    def test_the_packaged_file_is_registered_with_its_frontmatter(self):
        path = self.root / "SKILL.md"
        path.write_text(
            '---\nname: phone-harness\ndescription: "Drive the phone."\n---\n\n# phone-harness\n',
            encoding="utf-8",
        )
        self.plugin._package_dir = lambda: self.root
        self.plugin._command = lambda: [sys.executable, "-c", ""]
        ctx = FakeCtx()
        self.plugin.register(ctx)
        self.assertEqual(ctx.skills["phone-harness"]["path"], path)
        self.assertEqual(ctx.skills["phone-harness"]["description"], "Drive the phone.")
        self.assertEqual(ctx.skills["phone-harness"]["frontmatter"], {
            "name": "phone-harness",
            "description": "Drive the phone.",
        })

    def test_a_failing_skill_registration_is_logged(self):
        path = self.root / "SKILL.md"
        path.write_text(
            '---\nname: phone-harness\ndescription: "Drive the phone."\n---\n\n# phone-harness\n',
            encoding="utf-8",
        )
        self.plugin._package_dir = lambda: self.root
        self.plugin._command = lambda: [sys.executable, "-c", ""]

        class Boom(FakeCtx):
            def register_skill(self, name, path, description="", frontmatter=None):
                raise RuntimeError("skill store down")

        ctx = Boom()
        with self.assertLogs("phone_harness.hermes", level="WARNING") as caught:
            self.plugin.register(ctx)
        self.assertIn("phone_exec", ctx.tools)
        self.assertTrue(any("skill store down" in line for line in caught.output))


class WorkspaceSeed(unittest.TestCase):
    def test_an_empty_workspace_is_seeded_and_an_edit_is_kept(self):
        src = Path(__file__).resolve().parents[1] / "src"
        sys.path.insert(0, str(src))
        self.addCleanup(sys.path.remove, str(src))
        plugin = load_plugin()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        plugin._data_dir = lambda: data
        plugin._workspace = lambda: data / "agent-workspace"
        plugin._command = lambda: [sys.executable, "-c", "import sys; exec(sys.stdin.read())"]
        plugin._packaged_skill = lambda: None

        first = json.loads(plugin.phone_exec({"code": "print('seeded')"}))
        self.assertTrue(first["success"], first)
        helper = data / "agent-workspace" / "agent_helpers.py"
        self.assertTrue(helper.is_file())
        helper.write_text("# keep\n")
        (data / "agent-workspace" / "notes.txt").write_text("mine\n")

        again = json.loads(plugin.phone_exec({"code": "print('again')"}))
        self.assertTrue(again["success"], again)
        self.assertEqual(helper.read_text(), "# keep\n")
        self.assertEqual((data / "agent-workspace" / "notes.txt").read_text(), "mine\n")


class TelemetryOptIn(Base):
    """Hermes runs are opt-in. The CLI default stays on when the setting was never stored."""

    def setUp(self):
        super().setUp()
        self.saved = dict(os.environ)
        os.environ["PHONE_HARNESS_HOME"] = str(self.data)
        os.environ.pop("PHONE_HARNESS_TELEMETRY", None)
        self.addCleanup(self.restore_env)

    def restore_env(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def child_enabled(self) -> bool:
        env = self.plugin._env(self.data / "ws")
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-c",
             "from phone_harness.telemetry import is_enabled\n"
             "print('1' if is_enabled() else '0')"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip() == "1"

    def test_unset_is_off_under_hermes_and_the_cli_default_stays_on(self):
        from phone_harness.config import get, lookup
        from phone_harness.telemetry import is_enabled

        self.assertEqual(lookup("telemetry"), (True, "default"))
        self.assertTrue(get("telemetry"))
        self.assertTrue(is_enabled())
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "0")
        self.assertNotIn("PHONE_HARNESS_TELEMETRY", os.environ)
        self.assertTrue(is_enabled())
        self.assertFalse(self.child_enabled())

    def test_an_explicit_stored_true_is_on(self):
        from phone_harness.config import set as set_config

        set_config("telemetry", "true")
        env = self.plugin._env(self.data / "ws")
        self.assertNotIn("PHONE_HARNESS_TELEMETRY", env)
        self.assertTrue(self.child_enabled())

    def test_an_explicit_stored_false_is_off(self):
        from phone_harness.config import set as set_config
        from phone_harness.telemetry import is_enabled

        set_config("telemetry", "false")
        self.assertFalse(is_enabled())
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "0")
        self.assertFalse(self.child_enabled())

    def test_an_enabling_env_var_is_on(self):
        os.environ["PHONE_HARNESS_TELEMETRY"] = "true"
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "true")
        self.assertTrue(self.child_enabled())

    def test_a_disabling_env_var_is_off(self):
        os.environ["PHONE_HARNESS_TELEMETRY"] = "0"
        env = self.plugin._env(self.data / "ws")
        self.assertEqual(env["PHONE_HARNESS_TELEMETRY"], "0")
        self.assertFalse(self.child_enabled())

    def test_the_plugin_reads_the_same_config_file_as_the_cli(self):
        from phone_harness.config import paths

        self.assertEqual(self.plugin._config_json(), paths()["config"])
        os.environ.pop("PHONE_HARNESS_HOME", None)
        os.environ["XDG_CONFIG_HOME"] = str(self.data / "xdg")
        self.assertEqual(self.plugin._config_json(), paths()["config"])


class Running(Base):
    def test_prints_come_back_as_output(self):
        result = self.run_tool("print(6 * 7)")
        self.assertTrue(result["success"])
        self.assertEqual(result["output"], "42\n")
        self.assertEqual(result["workspace"], str(self.data / "agent-workspace"))

    def test_the_script_runs_in_the_workspace_with_the_plugin_environment(self):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ["PHONE_HARNESS_PLATFORM"] = "ios"
        result = self.run_tool(
            "import os\nprint(os.getcwd())\nprint(os.environ['PH_CLIENT'])\n"
            "print(os.environ['PHONE_HARNESS_PLATFORM'])"
        )
        cwd, client, platform = result["output"].split()
        self.assertEqual(Path(cwd).resolve(), (self.data / "agent-workspace").resolve())
        self.assertEqual(client, "hermes")
        self.assertEqual(platform, "ios")

    def test_a_failing_script_reports_exit_code_and_stderr(self):
        result = self.run_tool('raise SystemExit("iPhone in Use")')
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("iPhone in Use", result["stderr"])

    def test_screenshot_paths_in_the_output_are_listed(self):
        shot = self.data / "shot.png"
        shot.write_bytes(b"\x89PNG")
        result = self.run_tool(f"print('saved {shot}')")
        self.assertEqual(result["screenshots"], [str(shot)])

    def test_empty_code_and_a_missing_module_are_errors_not_crashes(self):
        self.assertFalse(self.run_tool("   ")["success"])
        self.plugin._command = lambda: None
        self.assertIn("not installed", self.run_tool("print(1)")["error"])

    def test_a_script_past_its_timeout_is_killed(self):
        self.plugin.MIN_TIMEOUT_S = 1
        started = time.monotonic()
        result = self.run_tool("import time\ntime.sleep(30)", timeout_s=1)
        self.assertFalse(result["success"])
        self.assertIn("timed out after 1s", result["error"])
        self.assertLess(time.monotonic() - started, 15)

    def test_one_script_on_the_phone_at_a_time(self):
        self.plugin.LOCK_WAIT_S = 0.5
        self.assertTrue(self.plugin._THREAD_LOCK.acquire(timeout=1))
        try:
            busy = self.run_tool("print('should not run')")
        finally:
            self.plugin._THREAD_LOCK.release()
        self.assertFalse(busy["success"])
        self.assertIn("busy", busy["error"])
        self.assertTrue(self.run_tool("print('after')")["success"])   # the lock was released
