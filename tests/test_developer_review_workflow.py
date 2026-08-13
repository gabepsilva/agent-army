import subprocess
import unittest
from pathlib import Path

from agent_army.git_worktrees import WorktreeResult
from agent_army.orchestrator import IssueOrchestrator
from agent_army.work_items import WorkItemReader


def developer_result() -> dict:
    return {
        "status": "completed",
        "summary": "Implemented the accepted change.",
        "evidence": ["Focused tests pass."],
        "questions": [],
        "recommended_actions": ["Run the normal review workflow."],
        "files_changed": ["src/example.py"],
        "commands_run": ["uv run python -m unittest"],
    }


def blocked_developer_result() -> dict:
    return {
        "status": "blocked",
        "summary": "Implementation needs a product decision.",
        "evidence": ["The acceptance criteria leave the target audience ambiguous."],
        "questions": ["Which audience should this behavior support?"],
        "recommended_actions": ["Project Owner should decide the audience."],
        "files_changed": [],
        "commands_run": [],
    }


def review_result() -> dict:
    return {
        "outcome": "approved",
        "reviewed_commit": "developer-sha",
        "summary": "No blocking optimization findings.",
        "evidence": ["Tests and changed files were inspected."],
        "questions": [],
        "recommended_actions": [],
        "files_changed": [],
        "commands_run": ["uv run python -m unittest"],
    }


class FakeWorkflowGitHub:
    def __init__(self, labels: list[str], comments: list[dict] | None = None) -> None:
        self.labels = labels
        self.comments = comments or []
        self.pr_comments: list[dict] = []
        self.checks: list[dict] = []
        self.label_updates: list[list[str]] = []
        self.pull_request = {
            "number": 9,
            "html_url": "https://github.com/acme/widgets/pull/9",
            "title": "Implement issue #7",
            "body": "Implements #7.",
            "state": "open",
            "user": {"login": "developer-app"},
            "head": {"ref": "agent-army/issue-7-test-issue", "sha": "developer-sha"},
            "base": {"ref": "main", "sha": "base-sha"},
        }

    def list_open_issues(self, owner: str, repository: str) -> list[dict]:
        return [{"number": 7}]

    def get_repository(self, owner: str, repository: str) -> dict:
        return {"default_branch": "main"}

    def get_issue(self, target) -> dict:
        if target.kind == "pull_request":
            return {
                "title": self.pull_request["title"],
                "body": self.pull_request["body"],
                "state": "open",
                "html_url": self.pull_request["html_url"],
                "labels": [],
            }
        return {
            "title": "Test issue",
            "body": "Issue body",
            "state": "open",
            "html_url": "https://github.com/acme/widgets/issues/7",
            "labels": [{"name": label} for label in self.labels],
        }

    def get_issue_comments(self, target) -> list[dict]:
        return self.pr_comments if target.kind == "pull_request" else self.comments

    def create_issue_comment(self, target, body: str) -> dict:
        self.comments.append({"body": body, "user": {"login": "agent-army"}})
        return {"html_url": "https://github.com/acme/widgets/issues/7#comment"}

    def update_issue_labels(self, target, labels: list[str]) -> dict:
        self.labels = labels
        self.label_updates.append(labels)
        return {}

    def list_open_pull_requests(self, owner: str, repository: str, *, head: str | None = None) -> list[dict]:
        return []

    def create_pull_request(self, owner: str, repository: str, **kwargs) -> dict:
        return self.pull_request

    def get_pull_request(self, target) -> dict:
        return self.pull_request

    def get_pull_request_files(self, target) -> list[dict]:
        return []

    def get_pull_request_reviews(self, target) -> list[dict]:
        return []

    def get_check_runs(self, owner: str, repository: str, head_sha: str) -> list[dict]:
        return [check for check in self.checks if check["head_sha"] == head_sha]

    def create_check_run(self, owner: str, repository: str, **kwargs) -> dict:
        check = {"name": kwargs["name"], "head_sha": kwargs["head_sha"], "output": {"summary": kwargs["summary"]}}
        self.checks.append(check)
        return check


class FakeExecutor:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.requests = []

    def execute(self, request) -> dict:
        self.requests.append(request)
        return self.result


class FakeWorktree:
    def __init__(self, branch: str) -> None:
        self.result = WorktreeResult(Path("/tmp/agent-army-test-worktree"), branch, "base-sha")

    def __enter__(self) -> WorktreeResult:
        return self.result

    def __exit__(self, *_: object) -> None:
        return None


class DeveloperReviewWorkflowTests(unittest.TestCase):
    def make_orchestrator(
        self,
        github: FakeWorkflowGitHub,
        executor: FakeExecutor,
        *,
        review_clean: bool = False,
    ) -> IssueOrchestrator:
        git_calls = {"rev_parse": 0}

        def git_runner(command, **kwargs):
            if command[-2:] == ["rev-parse", "main"]:
                return subprocess.CompletedProcess(command, 0, stdout="base-sha\n", stderr="")
            if command[-2:] == ["rev-parse", "HEAD"]:
                git_calls["rev_parse"] += 1
                sha = "base-sha" if git_calls["rev_parse"] == 1 else "developer-sha"
                return subprocess.CompletedProcess(command, 0, stdout=f"{sha}\n", stderr="")
            if command[-2:] == ["status", "--porcelain"]:
                status = "" if review_clean else " M src/example.py\n"
                return subprocess.CompletedProcess(command, 0, stdout=status, stderr="")
            if command[1:4] == ["show-ref", "--verify", "--quiet"]:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        return IssueOrchestrator(
            repository="acme/widgets",
            project_owner_github=github,
            documentation_github=github,
            developer_github=github,
            reviewer_github=github,
            workspace=Path("/workspace"),
            project_owner_role=Path("agents/project-owner/ROLE.md"),
            documentation_role=Path("agents/documentation/ROLE.md"),
            developer_role=Path("agents/developer/ROLE.md"),
            reviewer_role=Path("agents/optimization-reviewer/ROLE.md"),
            output_schema_path=Path("schemas/orchestrator-result.schema.json"),
            developer_output_schema_path=Path("schemas/developer-result.schema.json"),
            reviewer_output_schema_path=Path("schemas/optimization-review-result.schema.json"),
            executor=executor,
            work_item_reader=WorkItemReader(github),
            worktree_factory=lambda repository, **kwargs: FakeWorktree(kwargs["branch"]),
            git_command_runner=git_runner,
        )

    def test_developer_commits_pushes_creates_pr_and_routes_to_review(self) -> None:
        github = FakeWorkflowGitHub(["ready-for-development"])
        executor = FakeExecutor(developer_result())

        outcome = self.make_orchestrator(github, executor).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertEqual(github.labels, ["needs-optimization-review"])
        self.assertEqual(len(github.comments), 1)
        self.assertIn("pull/9", github.comments[0]["body"])
        self.assertEqual(executor.requests[0].workspace, Path("/tmp/agent-army-test-worktree"))
        self.assertEqual(
            executor.requests[0].reference_paths,
            (Path("agents/developer/references/mattpocock-skills/domain-modeling/SKILL.md"),),
        )

    def test_blocked_developer_question_is_durable_and_routes_to_project_owner(self) -> None:
        github = FakeWorkflowGitHub(["ready-for-development"])
        executor = FakeExecutor(blocked_developer_result())

        outcome = self.make_orchestrator(github, executor).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertEqual(github.labels, ["needs-decision"])
        self.assertEqual(len(github.comments), 1)
        self.assertIn("Focused question", github.comments[0]["body"])
        self.assertIn("Which audience", github.comments[0]["body"])

    def test_reviewer_publishes_check_and_routes_approved_head_to_merge_pause(self) -> None:
        developer_marker = {
            "body": "<!-- agent-army:result role=developer from=ready-for-development "
            "next=needs-optimization-review pr=9 branch=agent-army/issue-7-test-issue head=developer-sha -->"
        }
        github = FakeWorkflowGitHub(["needs-optimization-review"], [developer_marker])
        executor = FakeExecutor(review_result())

        outcome = self.make_orchestrator(github, executor, review_clean=True).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertEqual(github.labels, ["ready-for-merge"])
        self.assertEqual(len(github.checks), 1)
        self.assertIn("Outcome: **approved**", github.checks[0]["output"]["summary"])
        self.assertEqual(len(github.comments), 2)
        self.assertEqual(
            executor.requests[0].reference_paths,
            (
                Path(
                    "agents/optimization-reviewer/references/"
                    "mattpocock-skills/domain-modeling/SKILL.md"
                ),
            ),
        )

    def test_new_commit_on_merge_paused_pr_requires_fresh_review(self) -> None:
        comments = [
            {
                "body": "<!-- agent-army:result role=developer from=ready-for-development "
                "next=needs-optimization-review pr=9 branch=agent-army/issue-7 head=developer-sha -->"
            },
            {
                "body": "<!-- agent-army:result role=optimization-reviewer "
                "from=needs-optimization-review next=ready-for-merge outcome=approved "
                "pr=9 head=old-sha -->"
            },
        ]
        github = FakeWorkflowGitHub(["ready-for-merge"], comments)
        executor = FakeExecutor(review_result())

        task = self.make_orchestrator(github, executor)._select_task(
            {"issue": {"labels": ["ready-for-merge"], "comments": comments}}
        )

        self.assertIsNotNone(task)
        self.assertEqual(task.agent.name, "optimization-reviewer")
        self.assertEqual(task.source_state, "needs-optimization-review")


if __name__ == "__main__":
    unittest.main()
