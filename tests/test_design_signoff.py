import unittest
from pathlib import Path

from agent_army.agent_executor import AgentExecutionResult
from agent_army.orchestrator import IssueOrchestrator
from agent_army.publishers import MAX_SIGNOFF_ROUNDS, render_final_design
from agent_army.work_items import WorkItemReader


def signoff_result(outcome: str = "accepted", findings=None) -> dict:
    return {
        "outcome": outcome,
        "summary": "The write-up records the argument faithfully.",
        "findings": findings or [],
        "evidence": ["Compared the write-up against rounds 1-2 of the challenge."],
        "questions": [],
        "recommended_actions": [],
        "files_changed": [],
        "commands_run": [],
    }


def correction_finding() -> dict:
    return {
        "id": "S1",
        "severity": "blocking",
        "claim": "The write-up omits the conceded finding about empty input handling.",
        "evidence": ["Round 2 conceded `C1`, but the write-up still asserts it."],
    }


class FakeSignoffGitHub:
    def __init__(self, labels, comments=None, stamped=False) -> None:
        self.labels = labels
        self.comments = comments or []
        self.stamped = stamped
        self.reactions_created: list[tuple[int, str]] = []
        self.comment_updates: list[tuple[int, str]] = []

    def list_open_issues(self, owner, repository):
        return [{"number": 7}]

    def get_repository(self, owner, repository):
        return {"default_branch": "main"}

    def get_issue(self, target):
        return {
            "title": "Test issue",
            "body": "Body",
            "state": "open",
            "html_url": "https://github.com/acme/widgets/issues/7",
            "labels": [{"name": label} for label in self.labels],
        }

    def get_issue_comments(self, target):
        return self.comments

    def create_issue_comment(self, target, body):
        self.comments.append({"id": 900 + len(self.comments), "body": body})
        return {"html_url": "https://x/7#c"}

    def update_issue_comment(self, owner, repository, comment_id, body):
        self.comment_updates.append((comment_id, body))
        for comment in self.comments:
            if comment.get("id") == comment_id:
                comment["body"] = body
        return {}

    def update_issue_labels(self, target, labels):
        self.labels = labels
        return {}

    def create_reaction(self, owner, repository, comment_id, content):
        self.reactions_created.append((comment_id, content))
        self.stamped = True
        return {}

    def list_reactions(self, owner, repository, comment_id):
        return [{"content": "+1"}] if self.stamped else []


class FakeExecutor:
    def __init__(self, result) -> None:
        self.result = result
        self.requests = []

    def execute(self, request) -> AgentExecutionResult:
        self.requests.append(request)
        return AgentExecutionResult(output=self.result, backend="claude", cost_usd=0.1)


def design_comment(revision: int = 1) -> dict:
    return {
        "id": 500,
        "body": render_final_design(
            "The converged scope: add --dry-run, gated on --once.",
            source_state="needs-decision",
            next_state="needs-design-signoff",
            invocation_id="abc",
            revision=revision,
        ),
    }


class DesignSignoffGateTests(unittest.TestCase):
    def make(self, github, executor) -> IssueOrchestrator:
        return IssueOrchestrator(
            repository="acme/widgets",
            project_owner_github=github,
            documentation_github=github,
            reviewer_github=github,
            workspace=Path("/workspace"),
            project_owner_role=Path("agents/project-owner/ROLE.md"),
            documentation_role=Path("agents/documentation/ROLE.md"),
            reviewer_role=Path("agents/optimization-reviewer/ROLE.md"),
            output_schema_path=Path("schemas/orchestrator-result.schema.json"),
            reviewer_output_schema_path=Path("schemas/optimization-review-result.schema.json"),
            design_signoff_output_schema_path=Path("schemas/design-signoff-result.schema.json"),
            executor=executor,
            work_item_reader=WorkItemReader(github),
        )

    def test_stamping_the_design_releases_it_to_development(self) -> None:
        github = FakeSignoffGitHub(["needs-design-signoff"], [design_comment()])
        executor = FakeExecutor(signoff_result("accepted"))

        outcome = self.make(github, executor).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertEqual(github.labels, ["ready-for-development"])
        self.assertEqual(github.reactions_created, [(500, "+1")])

    def test_a_correction_does_not_release_it(self) -> None:
        github = FakeSignoffGitHub(["needs-design-signoff"], [design_comment()])
        executor = FakeExecutor(signoff_result("correction", [correction_finding()]))

        outcome = self.make(github, executor).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertNotIn("ready-for-development", github.labels)
        self.assertEqual(github.reactions_created, [])
        self.assertIn("Final Design correction", github.comments[-1]["body"])

    def test_an_already_stamped_design_recovers_without_rerunning_the_agent(self) -> None:
        github = FakeSignoffGitHub(["needs-design-signoff"], [design_comment()], stamped=True)
        executor = FakeExecutor(signoff_result("accepted"))

        outcome = self.make(github, executor).run_once()

        self.assertEqual(outcome.status, "recovered")
        self.assertEqual(github.labels, ["ready-for-development"])
        self.assertEqual(len(executor.requests), 0)

    def test_endless_corrections_escalate_to_a_human(self) -> None:
        corrections = [
            {
                "id": 600 + index,
                "body": "<!-- agent-army:signoff role=optimization-reviewer "
                f"revision={index + 1} outcome=correction -->",
            }
            for index in range(MAX_SIGNOFF_ROUNDS)
        ]
        github = FakeSignoffGitHub(
            ["needs-design-signoff"], [design_comment(), *corrections]
        )
        executor = FakeExecutor(signoff_result("accepted"))

        outcome = self.make(github, executor).run_once()

        self.assertEqual(outcome.status, "escalated")
        self.assertEqual(github.labels, ["needs-user-guidance"])
        self.assertEqual(len(executor.requests), 0)


if __name__ == "__main__":
    unittest.main()
