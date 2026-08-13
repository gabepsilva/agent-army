"""Small, explicit Git worktree operations owned by the Python orchestrator."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class WorktreeResult:
    path: Path
    branch: str
    base_sha: str


class IsolatedGitWorktree:
    """Create and remove one temporary worktree for a Developer task."""

    def __init__(
        self,
        repository: Path,
        *,
        base_ref: str,
        branch: str,
        create_branch: bool = True,
        command_runner: CommandRunner = subprocess.run,
    ) -> None:
        self.repository = repository
        self.base_ref = base_ref
        self.branch = branch
        self.create_branch = create_branch
        self._command_runner = command_runner
        self._path: Path | None = None
        self._root: Path | None = None

    def __enter__(self) -> WorktreeResult:
        if not self.repository.is_dir() or not (self.repository / ".git").exists():
            raise ValueError(f"Workspace is not a Git repository: {self.repository}")
        self._root = Path(tempfile.mkdtemp(prefix="agent-army-worktree-"))
        self._path = self._root / "workspace"
        self._run(["git", "worktree", "add", "--detach", str(self._path), self.base_ref], self.repository)
        try:
            if self.create_branch:
                self._run(["git", "switch", "-c", self.branch], self._path)
            base_sha = self._run(["git", "rev-parse", "HEAD"], self._path).stdout.strip()
            return WorktreeResult(self._path, self.branch, base_sha)
        except Exception:
            self._remove()
            raise

    def __exit__(self, *_: object) -> None:
        self._remove()

    def _remove(self) -> None:
        if self._path is None:
            return
        self._run(["git", "worktree", "remove", "--force", str(self._path)], self.repository, check=False)
        if self._root is not None and self._root.exists():
            shutil.rmtree(self._root)
        self._path = None
        self._root = None

    def _run(self, command: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = self._command_runner(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"Git command failed: {' '.join(command)}")
        return result


def run_git(
    command: list[str], cwd: Path, *, command_runner: CommandRunner = subprocess.run
) -> subprocess.CompletedProcess[str]:
    """Run one quiet Git command and raise without exposing command output."""
    result = command_runner(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Git command failed: {' '.join(command)}")
    return result


def git_ref_exists(
    repository: Path,
    ref: str,
    *,
    command_runner: CommandRunner = subprocess.run,
) -> bool:
    result = command_runner(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{ref}"],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.returncode == 0


def branch_name(issue_number: int, title: str) -> str:
    # The issue number is the stable identity. Titles can change while an issue
    # is in flight, so they must not create a second PR branch on retry.
    return f"agent-army/issue-{issue_number}"


def commit_and_push(worktree: WorktreeResult, *, title: str, command_runner: CommandRunner = subprocess.run) -> str:
    """Commit all Developer changes and push the isolated branch."""
    status = run_git(["git", "status", "--porcelain"], worktree.path, command_runner=command_runner)
    if not status.stdout.strip():
        raise ValueError("Developer produced no workspace changes.")
    run_git(["git", "add", "--all"], worktree.path, command_runner=command_runner)
    run_git(["git", "commit", "-m", title], worktree.path, command_runner=command_runner)
    run_git(
        ["git", "push", "--set-upstream", "origin", f"HEAD:{worktree.branch}"],
        worktree.path,
        command_runner=command_runner,
    )
    return run_git(["git", "rev-parse", "HEAD"], worktree.path, command_runner=command_runner).stdout.strip()
