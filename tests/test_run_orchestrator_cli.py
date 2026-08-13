import unittest
from unittest.mock import patch

from agent_army import run_orchestrator


class DryRunFlagCombinationTests(unittest.TestCase):
    def test_dry_run_without_once_exits_non_zero_without_running(self) -> None:
        argv = [
            "run-orchestrator",
            "--repository",
            "acme/widgets",
            "--workspace",
            "/workspace",
            "--dry-run",
        ]
        with patch("sys.argv", argv), patch.object(run_orchestrator, "run") as run_mock:
            with self.assertRaises(SystemExit) as raised:
                run_orchestrator.main()

        self.assertNotEqual(raised.exception.code, 0)
        run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
