"""Convergence has to be a positive claim.

The challenge result only ever recorded objections -- findings, and answers to
the other side's findings. Agreement existed solely as the absence of a
finding, so a reader could not see what had been settled without diffing one
round against the last, and nothing stopped a reviewer from silently
re-opening something it had already accepted.
"""

import unittest

from agent_army.publishers import (
    accumulate_agreements,
    render_requirements_challenge_result,
    validate_requirements_challenge_result,
)


def agreement(identifier: str = "A1", settled_round: int = 1) -> dict:
    return {
        "id": identifier,
        "claim": "The --once-only constraint is the right scope boundary.",
        "evidence": ["run_orchestrator.py:159 already gates on --once."],
        "settled_round": settled_round,
    }


def finding(identifier: str = "C1", severity: str = "blocking") -> dict:
    return {
        "id": identifier,
        "severity": severity,
        "claim": "Empty input behavior is undefined in the draft.",
        "evidence": ["src/agent_army/orchestrator.py:212"],
    }


def challenge(**overrides) -> dict:
    result = {
        "outcome": "no-material-concerns",
        "challenge_round": 1,
        "summary": "The draft holds up; recording what we settled.",
        "findings": [],
        "dispute_responses": [],
        "agreements": [agreement()],
        "evidence": ["Read the draft against the code."],
        "questions": [],
        "recommended_actions": [],
        "files_changed": [],
        "commands_run": [],
    }
    result.update(overrides)
    return result


class ConvergenceIsAPositiveClaimTests(unittest.TestCase):
    def test_declaring_convergence_requires_saying_what_was_agreed(self) -> None:
        with self.assertRaises(ValueError) as raised:
            validate_requirements_challenge_result(
                challenge(agreements=[]), 1
            )
        self.assertIn("what was agreed", str(raised.exception))

    def test_convergence_with_agreements_is_accepted(self) -> None:
        validate_requirements_challenge_result(challenge(), 1)

    def test_prior_agreements_satisfy_the_requirement(self) -> None:
        # A later round need not restate the whole history to converge.
        validate_requirements_challenge_result(
            challenge(agreements=[], challenge_round=2), 2, None, [agreement()]
        )

    def test_conceding_a_finding_must_record_it_as_settled(self) -> None:
        with self.assertRaises(ValueError) as raised:
            validate_requirements_challenge_result(
                challenge(
                    agreements=[],
                    dispute_responses=[
                        {
                            "finding_id": "C1",
                            "disposition": "conceded",
                            "rationale": "The draft does cover this; withdrawing.",
                        }
                    ],
                ),
                1,
            )
        self.assertIn("C1", str(raised.exception))

    def test_a_concession_recorded_as_an_agreement_is_accepted(self) -> None:
        validate_requirements_challenge_result(
            challenge(
                agreements=[agreement("C1")],
                dispute_responses=[
                    {
                        "finding_id": "C1",
                        "disposition": "conceded",
                        "rationale": "The draft does cover this; withdrawing.",
                    }
                ],
            ),
            1,
        )


class AccumulationTests(unittest.TestCase):
    def test_the_settled_record_grows_across_rounds(self) -> None:
        merged = accumulate_agreements([agreement("A1")], [agreement("A2", 2)], [])
        self.assertEqual([item["id"] for item in merged], ["A1", "A2"])

    def test_a_forgotten_agreement_is_not_lost(self) -> None:
        # The orchestrator owns the history, so a round that restates nothing
        # still keeps everything settled so far.
        merged = accumulate_agreements([agreement("A1")], [], [])
        self.assertEqual([item["id"] for item in merged], ["A1"])

    def test_re_opening_an_item_removes_it_from_settled(self) -> None:
        merged = accumulate_agreements([agreement("A1")], [], [finding("A1")])
        self.assertEqual(merged, [])

    def test_a_restated_agreement_is_not_duplicated(self) -> None:
        merged = accumulate_agreements([agreement("A1")], [agreement("A1")], [])
        self.assertEqual(len(merged), 1)


class RenderingTests(unittest.TestCase):
    def test_convergence_is_stated_in_words_and_settled_items_are_listed(self) -> None:
        rendered = render_requirements_challenge_result(
            challenge(),
            source_state="needs-requirements-challenge",
            next_state="needs-decision",
            invocation_id="abc",
            challenge_round=1,
        )

        self.assertIn("**Converged.**", rendered)
        self.assertIn("### Settled", rendered)
        self.assertIn("A1", rendered)
        self.assertIn("run_orchestrator.py:159", rendered)

    def test_an_unconverged_round_does_not_claim_convergence(self) -> None:
        rendered = render_requirements_challenge_result(
            challenge(outcome="concerns-found", findings=[finding()]),
            source_state="needs-requirements-challenge",
            next_state="needs-decision",
            invocation_id="abc",
            challenge_round=1,
        )

        self.assertNotIn("**Converged.**", rendered)
        self.assertIn("### Findings", rendered)


class WiringTests(unittest.TestCase):
    """Unit-testing the pieces is not enough -- three separate mechanisms
    today were correct in isolation and inert in production because nothing
    connected them. This drives the orchestrator."""

    def test_the_settled_record_survives_a_round_that_restates_nothing(self) -> None:
        import tests.test_orchestrator as orchestrator_tests
        from agent_army.publishers import decode_payload, encode_payload

        prior = [agreement()]
        comments = [
            {
                "body": "<!-- agent-army:result role=optimization-reviewer "
                "mode=requirements_challenge from=needs-requirements-challenge "
                "next=needs-decision round=1 outcome=concerns-found -->\n"
                + encode_payload(
                    {
                        "findings": orchestrator_tests.challenge_result(1)["findings"],
                        "agreements": prior,
                        "round": 1,
                    }
                )
            },
            {
                "body": "<!-- agent-army:result role=project-owner from=needs-decision "
                "next=needs-requirements-challenge challenge_round=2 -->"
            },
        ]
        github = orchestrator_tests.FakeGitHub(["needs-requirements-challenge"], comments)
        result = orchestrator_tests.challenge_result(2)
        result["outcome"] = "no-material-concerns"
        result["findings"] = []
        result["agreements"] = []
        executor = orchestrator_tests.FakeExecutor(result)
        orchestrator = orchestrator_tests.IssueOrchestratorTests().make_reviewer_orchestrator(
            github, executor
        )

        self.assertEqual(orchestrator.run_once().status, "processed")
        self.assertEqual(
            [item["id"] for item in executor.requests[-1].work_item["prior_agreements"]],
            ["A1"],
        )
        body = github.comments[-1]["body"]
        self.assertEqual([item["id"] for item in decode_payload(body)["agreements"]], ["A1"])
        self.assertIn("**Converged.**", body)
        self.assertIn("### Settled", body)


if __name__ == "__main__":
    unittest.main()
