import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_army.orchestrator import (
    OPTIMIZATION_REVIEW_CHECK_NAME,
    IssueOrchestrator,
    format_dry_run_outcome,
)
from agent_army.github_app import DESIGN_SIGNOFF_REACTION
from agent_army.publishers import MAX_CONVERGENCE_ROUNDS, encode_payload
from agent_army.run_orchestrator import main as run_orchestrator_main
from agent_army.work_items import WorkItemReader


class FakeDryRunGitHub:
    """A read/write GitHub double covering every call dry_run() can make.

    Write calls raise, so any test that reaches one fails loudly instead of
    silently recording a side effect dry-run must never produce.
    """

    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self.pull_requests: dict[int, dict] = {}
        self.pr_comments: dict[int, list[dict]] = {}
        self.checks: dict[str, list[dict]] = {}
        self.reactions: dict[int, list[dict]] = {}

    def add_issue(self, number: int, labels: list[str], comments: list[dict] | None = None) -> None:
        self.issues[number] = {"labels": labels, "comments": comments or []}

    def add_pull_request(
        self, number: int, *, head_sha: str, comments: list[dict] | None = None
    ) -> None:
        self.pull_requests[number] = {
            "number": number,
            "html_url": f"https://github.com/acme/widgets/pull/{number}",
            "head": {"ref": f"agent-army/issue-{number}", "sha": head_sha},
        }
        self.pr_comments[number] = comments or []

    def list_open_issues(self, owner: str, repository: str) -> list[dict]:
        return [{"number": number} for number in sorted(self.issues)]

    def get_issue(self, target) -> dict:
        issue = self.issues[target.number]
        return {
            "title": f"Issue {target.number}",
            "body": "Body",
            "state": "open",
            "html_url": f"https://github.com/acme/widgets/issues/{target.number}",
            "labels": [{"name": label} for label in issue["labels"]],
        }

    def get_issue_comments(self, target) -> list[dict]:
        if target.number in self.pr_comments:
            return self.pr_comments[target.number]
        return self.issues[target.number]["comments"]

    def get_pull_request(self, target) -> dict:
        return self.pull_requests[target.number]

    def get_check_runs(self, owner: str, repository: str, head_sha: str) -> list[dict]:
        return self.checks.get(head_sha, [])

    def list_reactions(self, owner: str, repository: str, comment_id: int) -> list[dict]:
        return self.reactions.get(comment_id, [])

    def create_issue_comment(self, target, body: str) -> dict:
        raise AssertionError("dry-run must never create a comment")

    def update_issue_labels(self, target, labels: list[str]) -> dict:
        raise AssertionError("dry-run must never update labels")

    def update_issue_comment(self, owner: str, repository: str, comment_id: int, body: str) -> dict:
        raise AssertionError("dry-run must never update a comment")

    def create_reaction(self, owner: str, repository: str, comment_id: int, content: str) -> dict:
        raise AssertionError("dry-run must never create a reaction")

    def create_pull_request(self, owner: str, repository: str, **kwargs) -> dict:
        raise AssertionError("dry-run must never create a pull request")

    def list_open_pull_requests(self, owner: str, repository: str, *, head: str | None = None):
        raise AssertionError("dry-run must never look for a branch to push")

    def get_repository(self, owner: str, repository: str) -> dict:
        raise AssertionError("dry-run must never need the default branch")


class UnreachableExecutor:
    def execute(self, request):
        raise AssertionError("dry-run must never invoke the agent executor")


class DryRunTests(unittest.TestCase):
    def make_orchestrator(self, github: FakeDryRunGitHub) -> IssueOrchestrator:
        executor = UnreachableExecutor()
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
            requirements_challenge_output_schema_path=Path(
                "schemas/requirements-challenge-result.schema.json"
            ),
            design_signoff_output_schema_path=Path("schemas/design-signoff-result.schema.json"),
            executor=executor,
            work_item_reader=WorkItemReader(github),
            id_factory=lambda: "test-invocation",
        )

    def test_no_open_issues(self) -> None:
        github = FakeDryRunGitHub()
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "no-open-issues")
        self.assertEqual(format_dry_run_outcome(outcome), "no open issues")

    def test_unlabeled_issue_would_invoke_project_owner(self) -> None:
        github = FakeDryRunGitHub()
        github.add_issue(1, ["priority:high"])
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "would-invoke")
        self.assertEqual(outcome.issue_number, 1)
        self.assertEqual(outcome.current_label, "unlabeled")
        self.assertEqual(outcome.role, "project-owner")

    def test_unlabeled_intake_already_completed_is_skipped(self) -> None:
        github = FakeDryRunGitHub()
        github.add_issue(
            1,
            [],
            comments=[
                {
                    "body": "<!-- agent-army:result role=project-owner from=unlabeled "
                    "next=needs-grooming -->"
                }
            ],
        )
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "no-eligible-issue")
        self.assertEqual(len(outcome.skipped), 1)
        self.assertEqual(outcome.skipped[0].issue_number, 1)
        self.assertEqual(outcome.skipped[0].reason, "unlabeled intake already completed")

    def test_orchestration_paused_is_skipped(self) -> None:
        github = FakeDryRunGitHub()
        github.add_issue(1, ["orchestration-paused"])
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "no-eligible-issue")
        self.assertEqual(outcome.skipped[0].reason, "orchestration-paused")

    def test_orchestration_paused_wins_over_a_workflow_label(self) -> None:
        github = FakeDryRunGitHub()
        github.add_issue(1, ["needs-decision", "orchestration-paused"])
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.skipped[0].reason, "orchestration-paused")

    def test_ambiguous_workflow_labels_are_skipped(self) -> None:
        github = FakeDryRunGitHub()
        github.add_issue(1, ["needs-decision", "ready-for-development"])
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "no-eligible-issue")
        self.assertIn("ambiguous workflow labels", outcome.skipped[0].reason)

    def test_needs_user_guidance_is_skipped_as_blocked_on_human(self) -> None:
        github = FakeDryRunGitHub()
        github.add_issue(1, ["needs-user-guidance"])
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "no-eligible-issue")
        self.assertEqual(outcome.skipped[0].reason, "needs-user-guidance (blocked on human)")

    def test_requirements_challenge_round_two_without_pending_result_is_skipped(self) -> None:
        github = FakeDryRunGitHub()
        comments = [
            {
                "body": "<!-- agent-army:result role=optimization-reviewer "
                "mode=requirements_challenge from=needs-requirements-challenge "
                "next=needs-decision round=2 outcome=concerns-found -->"
            },
            {
                "body": "<!-- agent-army:result role=project-owner from=needs-decision "
                "next=needs-requirements-challenge challenge_round=3 -->"
            },
        ]
        github.add_issue(1, ["needs-requirements-challenge"], comments=comments)
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "no-eligible-issue")
        self.assertEqual(
            outcome.skipped[0].reason, "needs-requirements-challenge round >= 2 (recovery only)"
        )

    def test_multiple_eligible_issues_reports_only_the_lowest_numbered(self) -> None:
        github = FakeDryRunGitHub()
        github.add_issue(3, ["needs-decision"])
        github.add_issue(9, ["needs-grooming"])
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "would-invoke")
        self.assertEqual(outcome.issue_number, 3)
        self.assertEqual(outcome.skipped, ())

    def test_needs_decision_with_a_posted_result_would_recover(self) -> None:
        github = FakeDryRunGitHub()
        comments = [
            {
                "body": "<!-- agent-army:result role=project-owner from=needs-decision "
                "next=needs-user-guidance -->"
            }
        ]
        github.add_issue(1, ["needs-decision"], comments=comments)
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "would-recover")
        self.assertEqual(outcome.next_state, "needs-user-guidance")
        self.assertEqual(outcome.role, "project-owner")

    def test_needs_decision_without_a_posted_result_would_invoke(self) -> None:
        github = FakeDryRunGitHub()
        github.add_issue(1, ["needs-decision"])
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "would-invoke")
        self.assertEqual(outcome.role, "project-owner")

    def test_stamped_final_design_would_recover_to_ready_for_development(self) -> None:
        github = FakeDryRunGitHub()
        comments = [
            {
                "id": 42,
                "body": "<!-- agent-army:final_design role=project-owner from=needs-decision "
                "next=needs-design-signoff revision=1 -->\nFinal Design",
            }
        ]
        github.add_issue(1, ["needs-design-signoff"], comments=comments)
        github.reactions[42] = [{"content": DESIGN_SIGNOFF_REACTION}]
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "would-recover")
        self.assertEqual(outcome.next_state, "ready-for-development")

    def test_unstamped_final_design_would_invoke_reviewer(self) -> None:
        github = FakeDryRunGitHub()
        comments = [
            {
                "id": 42,
                "body": "<!-- agent-army:final_design role=project-owner from=needs-decision "
                "next=needs-design-signoff revision=1 -->\nFinal Design",
            }
        ]
        github.add_issue(1, ["needs-design-signoff"], comments=comments)
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "would-invoke")
        self.assertEqual(outcome.role, "optimization-reviewer")
        self.assertEqual(outcome.mode, "design_signoff")

    def test_ready_for_development_always_reports_would_invoke_developer(self) -> None:
        github = FakeDryRunGitHub()
        github.add_issue(1, ["ready-for-development"])
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "would-invoke")
        self.assertEqual(outcome.role, "developer")

    def test_needs_optimization_review_with_existing_check_would_recover(self) -> None:
        github = FakeDryRunGitHub()
        marker = (
            "<!-- agent-army:result role=optimization-reviewer from=needs-optimization-review "
            "next=ready-for-merge outcome=approved head=sha-1 -->"
        )
        comments = [
            {
                "body": "<!-- agent-army:result role=developer from=ready-for-development "
                "next=needs-optimization-review pr=9 -->"
            }
        ]
        github.add_issue(1, ["needs-optimization-review"], comments=comments)
        github.add_pull_request(9, head_sha="sha-1")
        github.checks["sha-1"] = [
            {
                "name": OPTIMIZATION_REVIEW_CHECK_NAME,
                "output": {"summary": f"{marker}\nhead=sha-1"},
            }
        ]
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "would-recover")

    def test_needs_optimization_review_without_recovery_would_invoke(self) -> None:
        github = FakeDryRunGitHub()
        comments = [
            {
                "body": "<!-- agent-army:result role=developer from=ready-for-development "
                "next=needs-optimization-review pr=9 -->"
            }
        ]
        github.add_issue(1, ["needs-optimization-review"], comments=comments)
        github.add_pull_request(9, head_sha="sha-1")
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "would-invoke")
        self.assertEqual(outcome.role, "optimization-reviewer")

    def test_needs_optimization_review_past_round_cap_would_escalate(self) -> None:
        # Regression for issue #16 finding F1: seven review rounds have
        # already run without converging, each against a different commit
        # (the Developer revised between rounds), so none of them recovers
        # against the current head. The real next pass would post an
        # escalation and relabel to needs-user-guidance without invoking the
        # agent -- dry-run must report that, not "would invoke".
        comments = [
            {
                "body": "<!-- agent-army:result role=developer from=ready-for-development "
                "next=needs-optimization-review pr=9 -->"
            }
        ]
        prior_rounds = [
            {
                "body": "<!-- agent-army:result role=optimization-reviewer "
                f"from=needs-optimization-review next=ready-for-development "
                f"outcome=changes-requested pr=9 head=round-{index}-sha round={index} -->\n"
                + encode_payload({"findings": [], "round": index})
            }
            for index in range(1, MAX_CONVERGENCE_ROUNDS + 1)
        ]
        github = FakeDryRunGitHub()
        github.add_issue(1, ["needs-optimization-review"], comments=comments)
        github.add_pull_request(9, head_sha="round-8-sha", comments=prior_rounds)
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "would-escalate")
        self.assertEqual(outcome.next_state, "needs-user-guidance")
        self.assertEqual(
            format_dry_run_outcome(outcome),
            "issue #1 [needs-optimization-review] would escalate "
            "(convergence round limit reached, no agent invoked) -> needs-user-guidance",
        )

    def test_needs_optimization_review_below_round_cap_would_invoke(self) -> None:
        comments = [
            {
                "body": "<!-- agent-army:result role=developer from=ready-for-development "
                "next=needs-optimization-review pr=9 -->"
            }
        ]
        prior_rounds = [
            {
                "body": "<!-- agent-army:result role=optimization-reviewer "
                f"from=needs-optimization-review next=ready-for-development "
                f"outcome=changes-requested pr=9 head=round-{index}-sha round={index} -->\n"
                + encode_payload({"findings": [], "round": index})
            }
            for index in range(1, MAX_CONVERGENCE_ROUNDS)
        ]
        github = FakeDryRunGitHub()
        github.add_issue(1, ["needs-optimization-review"], comments=comments)
        github.add_pull_request(
            9, head_sha=f"round-{MAX_CONVERGENCE_ROUNDS}-sha", comments=prior_rounds
        )
        outcome = self.make_orchestrator(github).dry_run()
        self.assertEqual(outcome.status, "would-invoke")

    def test_dry_run_never_calls_a_write_capable_method(self) -> None:
        github = FakeDryRunGitHub()
        github.add_issue(1, ["priority:high"])
        github.add_issue(2, ["needs-decision"])
        github.add_issue(3, ["ready-for-development"])
        # Reaching any of the AssertionError-raising write methods on the fake
        # would fail this test, which is the point: dry_run() must never call
        # create_issue_comment, update_issue_labels, create_pull_request, etc.
        self.make_orchestrator(github).dry_run()


class DryRunCliTests(unittest.TestCase):
    def test_dry_run_without_once_is_a_usage_error(self) -> None:
        argv = [
            "run-orchestrator",
            "--repository",
            "acme/widgets",
            "--workspace",
            "/workspace",
            "--dry-run",
        ]
        with patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as raised:
                run_orchestrator_main()
        self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
