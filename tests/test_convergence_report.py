import unittest

from agent_army.convergence_report import (
    CLEAN_APPROVAL,
    DEVELOPER_DEFERENCE,
    EMPTY_FIX,
    ESCALATED,
    REVIEWER_STUBBORNNESS,
    analyze_issue,
    render_report,
)
from agent_army.publishers import encode_payload


def finding(finding_id: str = "F1", severity: str = "blocking") -> dict:
    return {
        "id": finding_id,
        "severity": severity,
        "claim": "The retry path re-runs the agent on every failure.",
        "evidence": ["src/example.py:42 runs before the guard."],
    }


def review_comment(
    *, round_number: int, head: str, outcome: str = "changes-requested", findings=None,
    dispute_responses=None,
) -> dict:
    payload = {"findings": findings if findings is not None else [finding()], "round": round_number}
    if dispute_responses is not None:
        payload["dispute_responses"] = dispute_responses
    return {
        "body": "<!-- agent-army:result role=optimization-reviewer "
        f"from=needs-optimization-review next=ready-for-development outcome={outcome} "
        f"pr=9 head={head} round={round_number} -->\n" + encode_payload(payload)
    }


def developer_comment(*, head: str, responses=None) -> dict:
    return {
        "body": "<!-- agent-army:result role=developer from=ready-for-development "
        f"next=needs-optimization-review pr=9 branch=b head={head} -->\n"
        + encode_payload({"responses": responses or []})
    }


def accepted(finding_id: str = "F1") -> dict:
    return {
        "finding_id": finding_id,
        "disposition": "accepted",
        "rationale": "Moved the guard ahead of the agent run.",
    }


def disputed(finding_id: str = "F1") -> dict:
    return {
        "finding_id": finding_id,
        "disposition": "disputed",
        "rationale": "Already guarded at src/example.py:88.",
    }


class ArgumentMeasurementTests(unittest.TestCase):
    def test_counts_rounds_findings_and_dispositions(self) -> None:
        comments = [
            developer_comment(head="sha1"),
            review_comment(round_number=1, head="sha1"),
            developer_comment(head="sha2", responses=[disputed()]),
            review_comment(
                round_number=2,
                head="sha2",
                outcome="approved",
                findings=[],
                dispute_responses=[
                    {
                        "finding_id": "F1",
                        "disposition": "conceded",
                        "rationale": "The existing guard does cover this.",
                    }
                ],
            ),
        ]

        report = analyze_issue(7, comments)

        self.assertEqual(report.pull_request, 9)
        self.assertEqual(report.rounds, 2)
        self.assertEqual(report.findings_raised, 1)
        self.assertEqual(report.developer_disputed, 1)
        self.assertEqual(report.reviewer_conceded, 1)
        self.assertEqual(report.outcome, "approved")
        self.assertEqual(report.flags, ())

    def test_issue_with_no_argument_reports_empty(self) -> None:
        report = analyze_issue(7, [{"body": "just a human comment"}])
        self.assertEqual(report.rounds, 0)
        self.assertEqual(report.flags, ())


    def test_a_requirements_challenge_is_not_counted_as_a_review_round(self) -> None:
        # The issue-level challenge shares the reviewer role and the result
        # marker kind but carries no pr attribute; counting it inflated the
        # round total on real data.
        comments = [
            {
                "body": "<!-- agent-army:result role=optimization-reviewer "
                "mode=requirements_challenge from=needs-requirements-challenge "
                "next=needs-decision round=1 outcome=concerns-found -->"
            },
            developer_comment(head="sha1"),
            review_comment(round_number=1, head="sha1", outcome="approved", findings=[]),
        ]

        self.assertEqual(analyze_issue(7, comments).rounds, 1)


class SmellTests(unittest.TestCase):
    """The flags do not judge argument quality -- they surface the shapes
    that are worth a human actually reading."""

    def test_clean_first_round_approval_on_a_large_diff_is_flagged(self) -> None:
        comments = [
            developer_comment(head="sha1"),
            review_comment(round_number=1, head="sha1", outcome="approved", findings=[]),
        ]

        report = analyze_issue(7, comments, diff_lines=412)

        self.assertIn(CLEAN_APPROVAL, report.flags)

    def test_the_same_clean_approval_on_a_small_diff_is_not_flagged(self) -> None:
        comments = [
            developer_comment(head="sha1"),
            review_comment(round_number=1, head="sha1", outcome="approved", findings=[]),
        ]

        self.assertEqual(analyze_issue(7, comments, diff_lines=12).flags, ())

    def test_a_developer_that_never_disputes_is_flagged(self) -> None:
        comments = [
            developer_comment(head="sha1"),
            review_comment(round_number=1, head="sha1"),
            developer_comment(
                head="sha2", responses=[accepted("F1"), accepted("F2"), accepted("F3")]
            ),
        ]

        report = analyze_issue(7, comments)

        self.assertIn(DEVELOPER_DEFERENCE, report.flags)

    def test_a_reviewer_that_never_concedes_is_flagged(self) -> None:
        comments = [
            developer_comment(head="sha1"),
            review_comment(round_number=1, head="sha1"),
            developer_comment(head="sha2", responses=[disputed("F1"), disputed("F2")]),
            review_comment(
                round_number=2,
                head="sha2",
                dispute_responses=[
                    {
                        "finding_id": "F1",
                        "disposition": "held",
                        "rationale": "Still reachable via src/example.py:12.",
                    },
                    {
                        "finding_id": "F2",
                        "disposition": "held",
                        "rationale": "Still reachable via src/example.py:20.",
                    },
                ],
            ),
        ]

        report = analyze_issue(7, comments)

        self.assertIn(REVIEWER_STUBBORNNESS, report.flags)

    def test_accepting_a_finding_then_changing_nothing_is_flagged(self) -> None:
        comments = [
            developer_comment(head="sha1"),
            review_comment(round_number=1, head="sha1"),
            developer_comment(head="sha2", responses=[accepted()]),
        ]

        report = analyze_issue(7, comments, compare_lines=lambda base, head: 0)

        self.assertIn(EMPTY_FIX, report.flags)

    def test_accepting_a_finding_and_actually_changing_code_is_not_flagged(self) -> None:
        comments = [
            developer_comment(head="sha1"),
            review_comment(round_number=1, head="sha1"),
            developer_comment(head="sha2", responses=[accepted()]),
        ]

        report = analyze_issue(7, comments, compare_lines=lambda base, head: 40)

        self.assertNotIn(EMPTY_FIX, report.flags)

    def test_empty_fix_is_skipped_when_commits_cannot_be_compared(self) -> None:
        comments = [
            developer_comment(head="sha1"),
            review_comment(round_number=1, head="sha1"),
            developer_comment(head="sha2", responses=[accepted()]),
        ]

        self.assertNotIn(EMPTY_FIX, analyze_issue(7, comments).flags)

    def test_an_escalated_argument_is_flagged(self) -> None:
        comments = [
            developer_comment(head="sha1"),
            review_comment(round_number=1, head="sha1"),
            {
                "body": "<!-- agent-army:escalation role=optimization-reviewer "
                "from=needs-optimization-review next=needs-user-guidance "
                "pr=9 round=7 -->"
            },
        ]

        report = analyze_issue(7, comments)

        self.assertTrue(report.escalated)
        self.assertIn(ESCALATED, report.flags)


class RenderTests(unittest.TestCase):
    def test_renders_a_scannable_table_with_a_summary(self) -> None:
        comments = [
            developer_comment(head="sha1"),
            review_comment(round_number=1, head="sha1", outcome="approved", findings=[]),
        ]

        rendered = render_report([analyze_issue(7, comments, diff_lines=412)])

        self.assertIn("ISSUE", rendered)
        self.assertIn("#7", rendered)
        self.assertIn(CLEAN_APPROVAL, rendered)
        self.assertIn("1 flagged", rendered)


if __name__ == "__main__":
    unittest.main()
