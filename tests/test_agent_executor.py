import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_army.agent_executor import (
    AgentExecutionRequest,
    ClaudeCliExecutor,
    CodexCliExecutor,
    build_executor,
)
from agent_army.config import ClaudeBackendConfig, RuntimeConfig


class CodexCliExecutorTests(unittest.TestCase):
    def test_runs_codex_with_workspace_write_and_parses_json(self) -> None:
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["prompt"] = kwargs["input"]
            captured["stderr"] = kwargs["stderr"]
            return subprocess.CompletedProcess(command, 0, stdout='{"summary":"ok"}', stderr=None)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".git").mkdir()
            role = workspace / "ROLE.md"
            schema = workspace / "schema.json"
            role.write_text("# Test role", encoding="utf-8")
            schema.write_text("{}", encoding="utf-8")
            reference = workspace / "domain-modeling.md"
            reference.write_text("# Domain Modeling reference", encoding="utf-8")
            unrelated = workspace / "unrelated.md"
            unrelated.write_text("# Unrelated reference", encoding="utf-8")
            result = CodexCliExecutor(runner).execute(
                AgentExecutionRequest(
                    role, workspace, {"issue": "data"}, schema, (reference,)
                )
            )

        self.assertEqual(result.output, {"summary": "ok"})
        self.assertEqual(result.backend, "codex")
        # Codex cannot report dollars on this output path; see AgentExecutionResult.
        self.assertEqual(result.cost_usd, 0.0)
        self.assertIn("workspace-write", captured["command"])
        self.assertEqual(captured["stderr"], subprocess.PIPE)
        self.assertNotIn("CODEX_API_KEY", captured["prompt"])
        self.assertIn("untrusted data", captured["prompt"])
        self.assertIn("# Domain Modeling reference", captured["prompt"])
        self.assertNotIn("# Unrelated reference", captured["prompt"])


class ClaudeCliExecutorTests(unittest.TestCase):
    @staticmethod
    def _envelope(**overrides) -> str:
        envelope = {
            "type": "result",
            "is_error": False,
            "result": '{"summary":"ok"}',
            "structured_output": {"summary": "ok"},
            "permission_denials": [],
            "total_cost_usd": 0.0425,
        }
        envelope.update(overrides)
        return json.dumps(envelope)

    def _run(self, runner, *, config=None):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".git").mkdir()
            role = workspace / "ROLE.md"
            schema = workspace / "schema.json"
            role.write_text("# Test role", encoding="utf-8")
            schema.write_text('{"type":"object"}', encoding="utf-8")
            return ClaudeCliExecutor(runner, config).execute(
                AgentExecutionRequest(role, workspace, {"issue": "data"}, schema)
            )

    def test_unwraps_structured_output_and_passes_schema_inline(self) -> None:
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["prompt"] = kwargs["input"]
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(command, 0, stdout=self._envelope(), stderr=None)

        result = self._run(runner)

        self.assertEqual(result.output, {"summary": "ok"})
        self.assertEqual(result.backend, "claude")
        self.assertEqual(result.cost_usd, 0.0425)
        # Claude takes the schema inline, not as a file path.
        self.assertIn('{"type":"object"}', captured["command"])
        self.assertIn("--json-schema", captured["command"])
        self.assertIn("--print", captured["command"])
        self.assertIn("bypassPermissions", captured["command"])
        self.assertIn("untrusted data", captured["prompt"])
        self.assertNotIn("ANTHROPIC_API_KEY", captured["env"])
        self.assertNotIn("GITHUB_TOKEN", captured["env"])

    def test_applies_configured_permission_mode_and_model(self) -> None:
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, stdout=self._envelope(), stderr=None)

        self._run(
            runner,
            config=ClaudeBackendConfig(
                permission_mode="acceptEdits", model="claude-opus-5", effort="medium"
            ),
        )

        self.assertIn("acceptEdits", captured["command"])
        self.assertIn("claude-opus-5", captured["command"])
        self.assertIn("--effort", captured["command"])
        self.assertIn("medium", captured["command"])

    def test_reports_permission_denials_when_structured_output_is_missing(self) -> None:
        def runner(command, **kwargs):
            stdout = self._envelope(
                structured_output=None,
                permission_denials=[{"tool_name": "Bash"}],
            )
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=None)

        with self.assertRaises(RuntimeError) as raised:
            self._run(runner)

        self.assertIn("Bash", str(raised.exception))

    def test_raises_on_reported_error(self) -> None:
        def runner(command, **kwargs):
            stdout = self._envelope(is_error=True, result="context limit reached")
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=None)

        with self.assertRaises(RuntimeError) as raised:
            self._run(runner)

        self.assertIn("context limit reached", str(raised.exception))


class BuildExecutorTests(unittest.TestCase):
    def test_defaults_to_codex(self) -> None:
        self.assertIsInstance(build_executor(RuntimeConfig()), CodexCliExecutor)

    def test_selects_claude(self) -> None:
        config = RuntimeConfig(backend="claude")
        self.assertIsInstance(build_executor(config), ClaudeCliExecutor)


if __name__ == "__main__":
    unittest.main()
