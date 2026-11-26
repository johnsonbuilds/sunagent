"""``RuntimeConfig`` is the single env parse for run wiring.

Every knob below used to be an ``os.getenv`` read deep inside the
execution / memory / recovery layers, which meant two components could
disagree about the same variable (and a library import could change
process behavior). The contract now: resolve once at the composition
root, pass the value down; the helpers keep their no-argument shape by
falling back to ``RuntimeConfig.from_env()`` exactly once.
"""

import sys
import unittest
from os import environ
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runtime.execution.guard import guard_for_runtime
from agent_runtime.execution.local import LocalShellExecutor, LocalWorkspace
from agent_runtime.memory import memory_root, render_memory_block
from agent_runtime.runtime_config import RuntimeConfig


class RunWiringFieldTests(unittest.TestCase):
    """The five newly collapsed knobs are parsed in exactly one place."""

    def test_defaults_are_the_historical_behavior(self) -> None:
        with mock.patch.dict(environ, {}, clear=True):
            config = RuntimeConfig.from_env()

        self.assertIsNone(config.workspace_root)
        self.assertEqual(config.command_guard_mode, "blocklist")
        self.assertFalse(config.memory_enabled)
        self.assertIsNone(config.memory_dir)
        self.assertIsNone(config.harness)

    def test_env_values_are_picked_up(self) -> None:
        env = {
            "AGENT_RUNTIME_WORKSPACE": "/work",
            "AGENT_RUNTIME_COMMAND_GUARD": " AllowList ",
            "AGENT_RUNTIME_MEMORY": "on",
            "AGENT_RUNTIME_MEMORY_DIR": "/mem",
            "AGENT_RUNTIME_HARNESS": "coding-v1",
        }
        with mock.patch.dict(environ, env, clear=True):
            config = RuntimeConfig.from_env()

        self.assertEqual(config.workspace_root, "/work")
        self.assertEqual(config.command_guard_mode, "allowlist")
        self.assertTrue(config.memory_enabled)
        self.assertEqual(config.memory_dir, "/mem")
        self.assertEqual(config.harness, "coding-v1")

    def test_overrides_beat_the_environment(self) -> None:
        with mock.patch.dict(environ, {"AGENT_RUNTIME_MEMORY": "1"}, clear=True):
            config = RuntimeConfig.from_env(memory_enabled=False,
                                            command_guard_mode="off")

        self.assertFalse(config.memory_enabled)
        self.assertEqual(config.command_guard_mode, "off")

    def test_invalid_guard_mode_falls_back_to_blocklist(self) -> None:
        with mock.patch.dict(environ, {"AGENT_RUNTIME_COMMAND_GUARD": "bogus"},
                             clear=True):
            with self.assertLogs("agent_runtime.runtime_config", "WARNING"):
                config = RuntimeConfig.from_env()

        self.assertEqual(config.command_guard_mode, "blocklist")

    def test_falsy_memory_values_stay_off(self) -> None:
        for raw in ("", "0", "off", "no", "false"):
            with mock.patch.dict(environ, {"AGENT_RUNTIME_MEMORY": raw},
                                 clear=True):
                self.assertFalse(RuntimeConfig.from_env().memory_enabled, raw)


class ExplicitInjectionTests(unittest.TestCase):
    """Deep layers obey the injected value, not the live environment."""

    def test_guard_built_from_config(self) -> None:
        runtime = RuntimeConfig(model="m", base_url=None,
                                command_guard_mode="allowlist")
        with mock.patch.dict(environ,
                             {"AGENT_RUNTIME_COMMAND_GUARD": "off"}, clear=True):
            self.assertEqual(guard_for_runtime(runtime).policy, "allowlist")
            # No config injected: the executor resolves the env once.
            self.assertEqual(LocalShellExecutor().guard.policy, "off")

    def test_executor_uses_the_injected_runtime(self) -> None:
        runtime = RuntimeConfig(model="m", base_url=None,
                                command_guard_mode="allowlist")
        with mock.patch.dict(environ,
                             {"AGENT_RUNTIME_COMMAND_GUARD": "off"}, clear=True):
            self.assertEqual(LocalShellExecutor(runtime=runtime).guard.policy,
                             "allowlist")

    def test_workspace_root_from_config(self) -> None:
        runtime = RuntimeConfig(model="m", base_url=None,
                                workspace_root="/from-config")
        with mock.patch.dict(environ,
                             {"AGENT_RUNTIME_WORKSPACE": "/work"}, clear=True):
            self.assertEqual(LocalWorkspace().root, Path("/work"))
            self.assertEqual(LocalWorkspace(runtime=runtime).root,
                             Path("/from-config"))
            self.assertEqual(LocalWorkspace(root="/other").root,
                             Path("/other"))

    def test_memory_enabled_flag_overrides_the_environment(self) -> None:
        # ``enabled=False`` wins even with the env switch on, and the
        # disabled path never touches the filesystem.
        with mock.patch.dict(environ, {"AGENT_RUNTIME_MEMORY": "1"},
                             clear=True):
            view = render_memory_block(LocalWorkspace(root="."),
                                       enabled=False)
            self.assertEqual(view.block, "")
            self.assertTrue(RuntimeConfig.from_env().memory_enabled)

    def test_memory_root_prefers_the_explicit_dir(self) -> None:
        with mock.patch.dict(environ,
                             {"AGENT_RUNTIME_MEMORY_DIR": "/from-env"},
                             clear=True):
            self.assertEqual(memory_root(Path("/explicit")),
                             Path("/explicit").resolve())
            self.assertEqual(memory_root(), Path("/from-env").resolve())


if __name__ == "__main__":
    unittest.main()
