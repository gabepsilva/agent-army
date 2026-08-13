import unittest
from pathlib import Path

from agent_army.orchestrator import IssueOrchestrator
from agent_army.work_items import WorkItemReader


def result(*, next_state: str, questions: list[str] | None = None) -> dict:
    return {
        "summary": "A validated result.",
        "evidence": ["The issue and repository were reviewed."],
        "questions": questions or [],
        "recommended_actions": ["Take the selected next action."],
        "files_changed": [],
        "commands_run": ["uv run python -m unittest"],
        "next_state": next_state,
    }


class FakeGitHub:
    def __init__(self, labels: list[str], comments: list[dict] | None = None) -> None:
        self.labels = labels
        self.comments = comments or []
        self.label_updates: list[list[str]] = []
        self.fail_label_update_once = False

    def list_open_issues(self, owner: str, repository: str) -> list[dict]:
        return [{"number": 7}]

    def get_issue(self, target) -> dict:
        return {
            "title": "Test issue",
            "body": "Issue body",
            "state": "open",
            "html_url": "https://github.com/acme/widgets/issues/7",
            "labels": [{"name": label} for label in self.labels],
        }

    def get_issue_comments(self, target) -> list[dict]:
        return self.comments

    def create_issue_comment(self, target, body: str) -> dict:
        self.comments.append({"body": body, "user": {"login": "agent-army"}})
        return {"html_url": "https://github.com/acme/widgets/issues/7#comment"}

    def update_issue_labels(self, target, labels: list[str]) -> dict:
        if self.fail_label_update_once:
            self.fail_label_update_once = False
            raise RuntimeError("temporary label API failure")
        self.labels = labels
        self.label_updates.append(labels)
        return {}


class FakeExecutor:
    def __init__(self, *results: dict) -> None:
        self.results = list(results)
        self.calls = 0
        self.requests = []

    def execute(self, request) -> dict:
        self.calls += 1
        self.requests.append(request)
        return self.results.pop(0)


class IssueOrchestratorTests(unittest.TestCase):
    def make_orchestrator(self, github: FakeGitHub, executor: FakeExecutor) -> IssueOrchestrator:
        return IssueOrchestrator(
            repository="acme/widgets",
            project_owner_github=github,
            documentation_github=github,
            workspace=Path("/workspace"),
            project_owner_role=Path("agents/project-owner/ROLE.md"),
            documentation_role=Path("agents/documentation/ROLE.md"),
            output_schema_path=Path("schemas/orchestrator-result.schema.json"),
            executor=executor,
            work_item_reader=WorkItemReader(github),
            id_factory=lambda: "test-invocation",
        )

    def test_processes_one_unlabeled_intake_and_preserves_non_workflow_labels(self) -> None:
        github = FakeGitHub(["priority:high"])
        executor = FakeExecutor(result(next_state="ready-for-development"))

        outcome = self.make_orchestrator(github, executor).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertEqual(github.labels, ["priority:high", "ready-for-development"])
        self.assertEqual(executor.calls, 1)
        self.assertEqual(len(github.comments), 1)
        self.assertIn("agent-army:result", github.comments[0]["body"])
        self.assertNotIn("agent-army:handoff", github.comments[0]["body"])
        self.assertEqual(
            executor.requests[0].reference_paths,
            (Path("agents/project-owner/references/mattpocock-skills/domain-modeling/SKILL.md"),),
        )

    def test_documentation_always_returns_to_project_owner(self) -> None:
        github = FakeGitHub(["needs-documentation"])
        executor = FakeExecutor(result(next_state="needs-decision", questions=["Who is the audience?"]))

        outcome = self.make_orchestrator(github, executor).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertEqual(github.labels, ["needs-decision"])
        self.assertIn("Focused question", github.comments[-1]["body"])
        self.assertEqual(
            executor.requests[0].reference_paths,
            (Path("agents/documentation/references/mattpocock-skills/domain-modeling/SKILL.md"),),
        )

    def test_project_owner_can_pause_for_explicit_user_guidance(self) -> None:
        github = FakeGitHub(["needs-decision"])
        executor = FakeExecutor(
            result(next_state="needs-user-guidance", questions=["Which audience is in scope?"])
        )
        orchestrator = self.make_orchestrator(github, executor)

        self.assertEqual(orchestrator.run_once().status, "processed")
        self.assertEqual(github.labels, ["needs-user-guidance"])
        self.assertEqual(orchestrator.run_once().status, "idle")
        self.assertEqual(executor.calls, 1)

    def test_orchestration_paused_prevents_unlabeled_intake_and_preserves_labels(self) -> None:
        github = FakeGitHub(["priority:high", "orchestration-paused"])
        executor = FakeExecutor(result(next_state="ready-for-development"))

        outcome = self.make_orchestrator(github, executor).run_once()

        self.assertEqual(outcome.status, "idle")
        self.assertEqual(github.labels, ["priority:high", "orchestration-paused"])
        self.assertEqual(github.label_updates, [])
        self.assertEqual(github.comments, [])
        self.assertEqual(executor.calls, 0)

    def test_orchestration_paused_wins_over_an_existing_workflow_state(self) -> None:
        github = FakeGitHub(["needs-documentation", "orchestration-paused"])
        executor = FakeExecutor(result(next_state="needs-decision"))

        outcome = self.make_orchestrator(github, executor).run_once()

        self.assertEqual(outcome.status, "idle")
        self.assertEqual(github.labels, ["needs-documentation", "orchestration-paused"])
        self.assertEqual(executor.calls, 0)

    def test_invalid_result_does_not_advance_and_is_retried(self) -> None:
        github = FakeGitHub(["needs-grooming"])
        executor = FakeExecutor(
            result(next_state="ready-for-development", questions=["one", "two"]),
            result(next_state="ready-for-development"),
        )
        orchestrator = self.make_orchestrator(github, executor)

        self.assertEqual(orchestrator.run_once().status, "failed")
        self.assertEqual(github.labels, ["needs-grooming"])
        self.assertEqual(github.comments, [])
        self.assertEqual(orchestrator.run_once().status, "processed")
        self.assertEqual(github.labels, ["ready-for-development"])
        self.assertEqual(executor.calls, 2)

    def test_durable_result_recovers_a_failed_label_update_without_rerunning_agent(self) -> None:
        github = FakeGitHub(["needs-documentation"])
        github.fail_label_update_once = True
        executor = FakeExecutor(result(next_state="needs-decision"))
        orchestrator = self.make_orchestrator(github, executor)

        self.assertEqual(orchestrator.run_once().status, "retrying-label-update")
        self.assertEqual(len(github.comments), 1)
        self.assertEqual(orchestrator.run_once().status, "recovered")
        self.assertEqual(github.labels, ["needs-decision"])
        self.assertEqual(len(github.comments), 1)
        self.assertEqual(executor.calls, 1)


if __name__ == "__main__":
    unittest.main()
