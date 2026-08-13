import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_army.codex_executor import CodexCliExecutor, CodexExecutionRequest


class CodexCliExecutorTests(unittest.TestCase):
    def test_runs_codex_with_workspace_write_and_parses_json(self) -> None:
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["prompt"] = kwargs["input"]
            return subprocess.CompletedProcess(command, 0, stdout='{"summary":"ok"}', stderr=None)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".git").mkdir()
            role = workspace / "ROLE.md"
            schema = workspace / "schema.json"
            role.write_text("# Test role", encoding="utf-8")
            schema.write_text("{}", encoding="utf-8")
            result = CodexCliExecutor(runner).execute(
                CodexExecutionRequest(role, workspace, {"issue": "data"}, schema)
            )

        self.assertEqual(result, {"summary": "ok"})
        self.assertIn("workspace-write", captured["command"])
        self.assertNotIn("CODEX_API_KEY", captured["prompt"])
        self.assertIn("untrusted data", captured["prompt"])
