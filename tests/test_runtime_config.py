import tempfile
import unittest
from pathlib import Path

from agent_army.config import (
    ClaudeBackendConfig,
    CodexBackendConfig,
    RuntimeConfig,
    load_agent_runtime_config,
    load_runtime_config,
)


class RuntimeConfigTests(unittest.TestCase):
    @staticmethod
    def _config_file(directory: str, contents: str) -> Path:
        path = Path(directory) / "config.yaml"
        path.write_text(contents, encoding="utf-8")
        return path

    def test_repository_config_selects_codex_by_default(self) -> None:
        config = load_runtime_config(Path("config.yaml"))
        self.assertEqual(config.backend, "codex")
        self.assertEqual(config.codex.sandbox, "workspace-write")

    def test_missing_file_preserves_previous_behavior(self) -> None:
        config = load_runtime_config(Path("does-not-exist.yaml"))
        self.assertEqual(config, RuntimeConfig())
        self.assertEqual(config.backend, "codex")

    def test_reads_claude_backend_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._config_file(
                directory,
                "backend: claude\n"
                "backends:\n"
                "  claude:\n"
                "    permission_mode: acceptEdits\n"
                "    model: claude-opus-5\n",
            )
            config = load_runtime_config(path)

        self.assertEqual(config.backend, "claude")
        self.assertEqual(
            config.claude,
            ClaudeBackendConfig(permission_mode="acceptEdits", model="claude-opus-5"),
        )
        # An unconfigured backend still carries usable defaults.
        self.assertEqual(config.codex, CodexBackendConfig())

    def test_rejects_unknown_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._config_file(directory, "backend: gemini\n")
            with self.assertRaises(ValueError) as raised:
                load_runtime_config(path)

        self.assertIn("gemini", str(raised.exception))
        self.assertIn("codex", str(raised.exception))

    def test_rejects_permission_mode_that_cannot_work_headless(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._config_file(
                directory,
                "backend: claude\nbackends:\n  claude:\n    permission_mode: plan\n",
            )
            with self.assertRaises(ValueError) as raised:
                load_runtime_config(path)

        self.assertIn("permission_mode", str(raised.exception))

    def test_command_line_override_wins(self) -> None:
        self.assertEqual(RuntimeConfig().with_backend("claude").backend, "claude")
        self.assertEqual(RuntimeConfig().with_backend(None).backend, "codex")
        with self.assertRaises(ValueError):
            RuntimeConfig().with_backend("gemini")


class AgentRuntimeOverrideTests(unittest.TestCase):
    @staticmethod
    def _agent_config(directory: str, contents: str) -> Path:
        path = Path(directory) / "agent-config.yaml"
        path.write_text(contents, encoding="utf-8")
        return path

    def test_agent_overrides_the_repository_default(self) -> None:
        base = RuntimeConfig(backend="codex")
        with tempfile.TemporaryDirectory() as directory:
            path = self._agent_config(
                directory,
                "agent:\n  name: developer\n"
                "runtime:\n"
                "  backend: claude\n"
                "  claude:\n"
                "    model: claude-opus-5\n",
            )
            config = load_agent_runtime_config(path, base)

        self.assertEqual(config.backend, "claude")
        self.assertEqual(config.claude.model, "claude-opus-5")
        # Unspecified keys still inherit rather than resetting to defaults.
        self.assertEqual(config.claude.permission_mode, base.claude.permission_mode)

    def test_agent_without_a_runtime_section_inherits_everything(self) -> None:
        base = RuntimeConfig(backend="claude", claude=ClaudeBackendConfig(model="claude-opus-5"))
        with tempfile.TemporaryDirectory() as directory:
            path = self._agent_config(directory, "agent:\n  name: documentation\n")
            self.assertEqual(load_agent_runtime_config(path, base), base)

    def test_command_line_backend_beats_the_agent_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._agent_config(directory, "runtime:\n  backend: claude\n")
            config = load_agent_runtime_config(path, RuntimeConfig())
        self.assertEqual(config.backend, "claude")
        self.assertEqual(config.with_backend("codex").backend, "codex")

    def test_shipped_agent_configs_are_valid(self) -> None:
        base = load_runtime_config(Path("config.yaml"))
        for path in sorted(Path("agents").glob("*/agent-config.yaml")):
            with self.subTest(agent=path.parent.name):
                config = load_agent_runtime_config(path, base)
                self.assertIn(config.backend, ("codex", "claude"))


if __name__ == "__main__":
    unittest.main()
