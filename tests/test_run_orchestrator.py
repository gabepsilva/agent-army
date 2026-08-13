import sys
import unittest
from unittest.mock import patch

from agent_army.run_orchestrator import main


class DryRunCliTests(unittest.TestCase):
    def test_dry_run_without_once_is_a_usage_error_before_contacting_github(self) -> None:
        argv = [
            "run-orchestrator",
            "--repository",
            "acme/widgets",
            "--workspace",
            "/workspace",
            "--dry-run",
        ]
        with patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as context:
                main()
        self.assertEqual(context.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
