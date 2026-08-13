import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_army.git_worktrees import IsolatedGitWorktree, branch_name, git_ref_exists


class GitWorktreeTests(unittest.TestCase):
    def test_branch_name_is_stable_and_safe_for_issue_work(self) -> None:
        self.assertEqual(
            branch_name(42, "Add safe GitHub/PR handling!"),
            "agent-army/issue-42",
        )

    def test_isolated_worktree_creates_and_removes_a_branch_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-army-git-test-") as directory:
            repository = Path(directory)
            self._git(repository, "init", "-q")
            self._git(repository, "config", "user.email", "agent-army-test@example.com")
            self._git(repository, "config", "user.name", "Agent Army Test")
            (repository / "README.md").write_text("initial\n", encoding="utf-8")
            self._git(repository, "add", "README.md")
            self._git(repository, "commit", "-qm", "initial")

            self.assertFalse(git_ref_exists(repository, "agent-army/issue-42"))
            with IsolatedGitWorktree(
                repository,
                base_ref="HEAD",
                branch="agent-army/issue-42",
            ) as worktree:
                self.assertTrue(worktree.path.is_dir())
                self.assertTrue((worktree.path / ".git").exists())
                (worktree.path / "change.txt").write_text("change\n", encoding="utf-8")

            self.assertFalse(worktree.path.exists())
            self.assertTrue(git_ref_exists(repository, "agent-army/issue-42"))

    @staticmethod
    def _git(repository: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
