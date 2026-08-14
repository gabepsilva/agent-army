import unittest

from agent_army.publishers import (
    MAX_CONVERGENCE_ROUNDS,
    decode_payload,
    encode_payload,
    validate_developer_result,
    validate_optimization_review_result,
)


def finding(**overrides) -> dict:
    result = {
        "id": "F1",
        "severity": "blocking",
        "claim": "The retry loop re-runs the agent on every permissions failure.",
        "evidence": ["src/agent_army/orchestrator.py:772 creates the check run after the run."],
    }
    result.update(overrides)
    return result


def developer_result(**overrides) -> dict:
    result = {
        "status": "completed",
        "summary": "Addressed the review findings on the retry path.",
        "responses": [],
        "evidence": ["Focused tests pass under `uv run python -m unittest`."],
        "questions": [],
        "recommended_actions": ["Re-review the updated retry path."],
        "files_changed": ["src/agent_army/orchestrator.py"],
        "commands_run": ["uv run python -m unittest"],
    }
    result.update(overrides)
    return result


def review_result(**overrides) -> dict:
    result = {
        "outcome": "approved",
        "reviewed_commit": "abcdef1234567",
        "summary": "The retry path now terminates as intended.",
        "findings": [],
        "dispute_responses": [],
        "evidence": ["Re-read src/agent_army/orchestrator.py:772 after the change."],
        "questions": [],
        "recommended_actions": [],
        "files_changed": [],
        "commands_run": ["uv run python -m unittest"],
    }
    result.update(overrides)
    return result


class DeveloperMustEngageTests(unittest.TestCase):
    """The proposer accepts the pushback or rejects it with justification --
    it may not silently ignore a blocking finding."""

    def test_blocking_finding_must_be_accepted_or_disputed(self) -> None:
        with self.assertRaises(ValueError) as raised:
            validate_developer_result(developer_result(), [finding()])
        self.assertIn("F1", str(raised.exception))

    def test_accepting_a_blocking_finding_is_enough(self) -> None:
        validate_developer_result(
            developer_result(
                responses=[
                    {
                        "finding_id": "F1",
                        "disposition": "accepted",
                        "rationale": "Fixed by moving the check-run call out of the failure path.",
                    }
                ]
            ),
            [finding()],
        )

    def test_dispute_requires_recheckable_evidence(self) -> None:
        with self.assertRaises(ValueError) as raised:
            validate_developer_result(
                developer_result(
                    responses=[
                        {
                            "finding_id": "F1",
                            "disposition": "disputed",
                            "rationale": "Typically you would not want to restructure this.",
                        }
                    ]
                ),
                [finding()],
            )
        self.assertIn("re-checkable", str(raised.exception))

    def test_dispute_with_a_file_line_citation_is_accepted(self) -> None:
        validate_developer_result(
            developer_result(
                responses=[
                    {
                        "finding_id": "F1",
                        "disposition": "disputed",
                        "rationale": "Already guarded at src/agent_army/orchestrator.py:788.",
                    }
                ]
            ),
            [finding()],
        )

    def test_non_blocking_findings_need_no_response(self) -> None:
        validate_developer_result(developer_result(), [finding(severity="nit")])

    def test_responding_to_an_unknown_finding_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_developer_result(
                developer_result(
                    responses=[
                        {
                            "finding_id": "F9",
                            "disposition": "accepted",
                            "rationale": "Fixed the thing that was never raised.",
                        }
                    ]
                ),
                [finding()],
            )


class ReviewerMustEngageTests(unittest.TestCase):
    """The symmetric half: a Reviewer may not ignore a dispute, and may not
    approve while it is still holding a blocking finding."""

    def test_dispute_must_be_conceded_or_held(self) -> None:
        disputes = [
            {"finding_id": "F1", "disposition": "disputed", "rationale": "See file.py:10."}
        ]
        with self.assertRaises(ValueError) as raised:
            validate_optimization_review_result(review_result(), "abcdef1234567", disputes)
        self.assertIn("F1", str(raised.exception))

    def test_conceding_drops_the_finding(self) -> None:
        disputes = [
            {"finding_id": "F1", "disposition": "disputed", "rationale": "See file.py:10."}
        ]
        validate_optimization_review_result(
            review_result(
                dispute_responses=[
                    {
                        "finding_id": "F1",
                        "disposition": "conceded",
                        "rationale": "The existing guard does cover this; withdrawing.",
                    }
                ]
            ),
            "abcdef1234567",
            disputes,
        )

    def test_holding_requires_counter_evidence_and_keeps_the_finding_open(self) -> None:
        disputes = [
            {"finding_id": "F1", "disposition": "disputed", "rationale": "See file.py:10."}
        ]
        with self.assertRaises(ValueError) as raised:
            validate_optimization_review_result(
                review_result(
                    outcome="changes-requested",
                    findings=[finding()],
                    dispute_responses=[
                        {
                            "finding_id": "F1",
                            "disposition": "held",
                            "rationale": "I still think this is wrong.",
                        }
                    ],
                ),
                "abcdef1234567",
                disputes,
            )
        self.assertIn("re-checkable", str(raised.exception))

    def test_cannot_approve_while_a_blocking_finding_is_open(self) -> None:
        with self.assertRaises(ValueError) as raised:
            validate_optimization_review_result(
                review_result(outcome="approved", findings=[finding()]), "abcdef1234567"
            )
        self.assertIn("blocking", str(raised.exception))

    def test_changes_requested_requires_a_blocking_finding(self) -> None:
        with self.assertRaises(ValueError):
            validate_optimization_review_result(
                review_result(outcome="changes-requested", findings=[finding(severity="nit")]),
                "abcdef1234567",
            )

    def test_blocking_finding_requires_recheckable_evidence(self) -> None:
        with self.assertRaises(ValueError) as raised:
            validate_optimization_review_result(
                review_result(
                    outcome="changes-requested",
                    findings=[finding(evidence=["This is generally considered bad practice."])],
                ),
                "abcdef1234567",
            )
        self.assertIn("re-checkable", str(raised.exception))

    def test_a_nit_may_be_argued_without_a_citation(self) -> None:
        # Nits do not cost the other side a revise cycle, so they are not
        # held to the blocking-finding evidence bar.
        validate_optimization_review_result(
            review_result(findings=[finding(severity="nit", evidence=["Naming reads awkwardly."])]),
            "abcdef1234567",
        )


class PayloadTests(unittest.TestCase):
    def test_round_trips_structured_state(self) -> None:
        payload = {"findings": [finding()], "round": 3}
        self.assertEqual(decode_payload(encode_payload(payload)), payload)

    def test_a_finding_containing_a_comment_terminator_cannot_break_the_payload(self) -> None:
        payload = {"findings": [finding(claim="The guard --> here is wrong.")], "round": 1}
        encoded = encode_payload(payload)
        self.assertNotIn("--> here", encoded)
        self.assertEqual(decode_payload(encoded), payload)

    def test_missing_payload_reads_as_empty(self) -> None:
        self.assertEqual(decode_payload("## A comment with no payload"), {})


class ConvergenceBoundTests(unittest.TestCase):
    def test_escalation_threshold_is_seven_rounds(self) -> None:
        self.assertEqual(MAX_CONVERGENCE_ROUNDS, 7)


if __name__ == "__main__":
    unittest.main()
