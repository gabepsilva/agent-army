import unittest
from pathlib import Path

from agent_army.orchestrator import DryRunReport, IssueOrchestrator, format_dry_run_report
from agent_army.work_items import WorkItemReader


class MultiIssueFakeGitHub:
    """A read-only, multi-issue GitHub fake. Any write call fails the test."""

    def __init__(self, issues: dict[int, dict]) -> None:
        self.issues = issues
        self.pull_requests_by_branch: dict[str, list[dict]] = {}
        self.pull_requests_by_number: dict[int, dict] = {}
        self.checks_by_head: dict[str, list[dict]] = {}

    def list_open_issues(self, owner: str, repository: str) -> list[dict]:
        return [{"number": number} for number in sorted(self.issues)]

    def get_issue(self, target) -> dict:
        data = self.issues[target.number]
        return {
            "title": data.get("title", f"Issue {target.number}"),
            "body": data.get("body", ""),
            "state": "open",
            "html_url": f"https://github.com/acme/widgets/issues/{target.number}",
            "labels": [{"name": label} for label in data["labels"]],
        }

    def get_issue_comments(self, target) -> list[dict]:
        return self.issues[target.number].get("comments", [])

    def create_issue_comment(self, target, body: str) -> dict:
        raise AssertionError("Dry run must not create issue comments.")

    def update_issue_labels(self, target, labels: list[str]) -> dict:
        raise AssertionError("Dry run must not update issue labels.")

    def get_repository(self, owner: str, repository: str) -> dict:
        raise AssertionError("Dry run must not read repository metadata.")

    def list_open_pull_requests(
        self, owner: str, repository: str, *, head: str | None = None
    ) -> list[dict]:
        return self.pull_requests_by_branch.get(head, [])

    def create_pull_request(self, owner: str, repository: str, **kwargs) -> dict:
        raise AssertionError("Dry run must not create pull requests.")

    def get_pull_request(self, target) -> dict:
        return self.pull_requests_by_number[target.number]

    def get_check_runs(self, owner: str, repository: str, head_sha: str) -> list[dict]:
        return self.checks_by_head.get(head_sha, [])

    def create_check_run(self, owner: str, repository: str, **kwargs) -> dict:
        raise AssertionError("Dry run must not create check runs.")


class FakeExecutor:
    def execute(self, request) -> dict:
        raise AssertionError("Dry run must not invoke an agent.")


class DryRunTests(unittest.TestCase):
    def make_orchestrator(self, github: MultiIssueFakeGitHub) -> IssueOrchestrator:
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
            executor=FakeExecutor(),
            work_item_reader=WorkItemReader(github),
        )

    def test_no_open_issues(self) -> None:
        github = MultiIssueFakeGitHub({})

        report = self.make_orchestrator(github).run_dry_run()

        self.assertEqual(report, DryRunReport(eligible_issue=None, has_open_issues=False))
        self.assertEqual(format_dry_run_report(report), "Agent Army dry run: no open issues.")

    def test_reports_ineligibility_reason_for_every_open_issue(self) -> None:
        github = MultiIssueFakeGitHub(
            {
                1: {"labels": ["orchestration-paused"]},
                2: {"labels": ["needs-grooming", "needs-decision"]},
                3: {"labels": ["needs-user-guidance"]},
            }
        )

        report = self.make_orchestrator(github).run_dry_run()

        self.assertIsNone(report.eligible_issue)
        self.assertTrue(report.has_open_issues)
        reasons = {status.issue_number: status.reason for status in report.ineligible_issues}
        self.assertEqual(
            reasons,
            {
                1: "orchestration-paused",
                2: "ambiguous-labels",
                3: "needs-user-guidance",
            },
        )
        rendered = format_dry_run_report(report)
        self.assertIn("issue #1: orchestration-paused", rendered)
        self.assertIn("issue #2: ambiguous-labels", rendered)
        self.assertIn("issue #3: needs-user-guidance", rendered)

    def test_challenge_round_exhausted_reason(self) -> None:
        comments = [
            {
                "body": "<!-- agent-army:result role=optimization-reviewer "
                "mode=requirements_challenge from=needs-requirements-challenge "
                "next=needs-decision round=1 outcome=concerns-found -->"
            },
            {
                "body": "<!-- agent-army:result role=optimization-reviewer "
                "mode=requirements_challenge from=needs-requirements-challenge "
                "next=needs-decision round=2 outcome=concerns-found -->"
            },
            {
                "body": "<!-- agent-army:result role=project-owner "
                "next=needs-requirements-challenge challenge_round=3 -->"
            },
        ]
        github = MultiIssueFakeGitHub(
            {5: {"labels": ["needs-requirements-challenge"], "comments": comments}}
        )

        report = self.make_orchestrator(github).run_dry_run()

        self.assertIsNone(report.eligible_issue)
        self.assertEqual(
            report.ineligible_issues[0].reason, "challenge-round-exhausted"
        )

    def test_no_fresh_review_needed_reason(self) -> None:
        github = MultiIssueFakeGitHub({6: {"labels": ["ready-for-merge"], "comments": []}})

        report = self.make_orchestrator(github).run_dry_run()

        self.assertIsNone(report.eligible_issue)
        self.assertEqual(report.ineligible_issues[0].reason, "no-fresh-review-needed")

    def test_eligible_issue_reports_invoke_agent_for_fresh_development(self) -> None:
        github = MultiIssueFakeGitHub(
            {8: {"labels": ["ready-for-development"], "title": "Add feature"}}
        )

        report = self.make_orchestrator(github).run_dry_run()

        self.assertIsNotNone(report.eligible_issue)
        self.assertEqual(report.eligible_issue.issue_number, 8)
        self.assertEqual(report.eligible_issue.role, "developer")
        self.assertEqual(report.eligible_issue.source_state, "ready-for-development")
        self.assertEqual(report.eligible_issue.action, "invoke agent")
        rendered = format_dry_run_report(report)
        self.assertIn("issue #8", rendered)
        self.assertIn("developer", rendered)
        self.assertIn("invoke agent", rendered)

    def test_eligible_issue_reports_recover_result_for_existing_pr(self) -> None:
        github = MultiIssueFakeGitHub(
            {9: {"labels": ["ready-for-development"], "title": "Add feature"}}
        )
        github.pull_requests_by_branch["agent-army/issue-9"] = [
            {
                "number": 42,
                "html_url": "https://github.com/acme/widgets/pull/42",
                "head": {"ref": "agent-army/issue-9", "sha": "abc123"},
            }
        ]

        report = self.make_orchestrator(github).run_dry_run()

        self.assertIsNotNone(report.eligible_issue)
        self.assertEqual(report.eligible_issue.action, "recover result")

    def test_only_first_eligible_issue_is_reported(self) -> None:
        github = MultiIssueFakeGitHub(
            {
                1: {"labels": ["orchestration-paused"]},
                2: {"labels": ["ready-for-development"], "title": "Second issue"},
                3: {"labels": ["ready-for-development"], "title": "Third issue"},
            }
        )

        report = self.make_orchestrator(github).run_dry_run()

        self.assertIsNotNone(report.eligible_issue)
        self.assertEqual(report.eligible_issue.issue_number, 2)
        self.assertEqual(len(report.ineligible_issues), 1)
        self.assertEqual(report.ineligible_issues[0].issue_number, 1)


if __name__ == "__main__":
    unittest.main()
