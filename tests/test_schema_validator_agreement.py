"""The JSON Schema and the Python validators are two enforcement layers.

The schema constrains what an agent CLI is allowed to emit; the validators
constrain what the orchestrator will publish. Nothing forces them to agree, and
when they drift the result is not a failing test but a production deadlock: on
issue #15 the schema forbade `requirements_scope_changed` while the validator
required it, so Project Owner could not emit any acceptable result and every
poll failed forever. Unit tests missed it because they call the validators
directly with fixtures the schema no longer permits.

These tests compare the layers against each other.
"""

import json
import unittest
from pathlib import Path

from agent_army.publishers import (
    validate_developer_result,
    validate_optimization_review_result,
    validate_orchestration_result,
    validate_requirements_challenge_result,
    validate_signoff_result,
)

SCHEMAS = Path("schemas")


def load(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


def conforms(instance: dict, schema: dict) -> list[str]:
    """Minimal structural conformance: required keys and additionalProperties."""
    problems = []
    for key in schema.get("required", []):
        if key not in instance:
            problems.append(f"missing required key: {key}")
    if schema.get("additionalProperties") is False:
        for key in instance:
            if key not in schema.get("properties", {}):
                problems.append(f"key not allowed by schema: {key}")
    for key, value in instance.items():
        spec = schema.get("properties", {}).get(key)
        if spec and "enum" in spec and value not in spec["enum"]:
            problems.append(f"{key}={value!r} not in enum {spec['enum']}")
    return problems


class SchemaAndValidatorAgreeTests(unittest.TestCase):
    """A result the schema permits must be one the validator accepts."""

    def assert_round_trip(self, instance: dict, schema_name: str, validate) -> None:
        problems = conforms(instance, load(schema_name))
        self.assertEqual(problems, [], f"{schema_name}: {problems}")
        validate(instance)  # must not raise

    def test_project_owner_can_route_to_a_requirements_challenge(self) -> None:
        # The exact shape that deadlocked issue #15.
        self.assert_round_trip(
            {
                "summary": "Drafted scope and routed it for an independent challenge.",
                "evidence": ["Read src/agent_army/orchestrator.py:212 for eligibility."],
                "questions": [],
                "recommended_actions": ["Reviewer should stress-test the draft."],
                "files_changed": [],
                "commands_run": [],
                "next_state": "needs-requirements-challenge",
                "requirements_challenge_round": 1,
            },
            "orchestrator-result.schema.json",
            lambda r: validate_orchestration_result(r, "project-owner"),
        )

    def test_project_owner_can_route_to_design_signoff(self) -> None:
        self.assert_round_trip(
            {
                "summary": "The argument converged; posting the final design.",
                "evidence": ["All blocking findings were conceded in round 2."],
                "questions": [],
                "recommended_actions": ["Reviewer should sign off on the design."],
                "files_changed": [],
                "commands_run": [],
                "next_state": "needs-design-signoff",
                "final_design": "Add --dry-run, gated on --once.",
            },
            "orchestrator-result.schema.json",
            lambda r: validate_orchestration_result(r, "project-owner"),
        )

    def test_requirements_challenge_result_round_trips(self) -> None:
        self.assert_round_trip(
            {
                "outcome": "concerns-found",
                "challenge_round": 1,
                "summary": "One blocking gap in the draft.",
                "findings": [
                    {
                        "id": "C1",
                        "severity": "blocking",
                        "claim": "Empty input behavior is undefined.",
                        "evidence": ["src/agent_army/orchestrator.py:212"],
                    }
                ],
                "dispute_responses": [],
                "agreements": [],
                "evidence": ["Read the draft against the code."],
                "questions": [],
                "recommended_actions": ["Define empty-input behavior."],
                "files_changed": [],
                "commands_run": [],
            },
            "requirements-challenge-result.schema.json",
            lambda r: validate_requirements_challenge_result(r, 1),
        )

    def test_developer_result_round_trips(self) -> None:
        self.assert_round_trip(
            {
                "status": "completed",
                "summary": "Implemented the accepted change.",
                "responses": [],
                "evidence": ["Tests pass under `uv run python -m unittest`."],
                "questions": [],
                "recommended_actions": ["Run the normal review workflow."],
                "files_changed": ["src/example.py"],
                "commands_run": ["uv run python -m unittest"],
            },
            "developer-result.schema.json",
            validate_developer_result,
        )

    def test_review_result_round_trips(self) -> None:
        self.assert_round_trip(
            {
                "outcome": "approved",
                "reviewed_commit": "abcdef1234567",
                "summary": "No blocking findings on this commit.",
                "findings": [],
                "dispute_responses": [],
                "evidence": ["Inspected the diff and tests."],
                "questions": [],
                "recommended_actions": [],
                "files_changed": [],
                "commands_run": ["uv run python -m unittest"],
            },
            "optimization-review-result.schema.json",
            lambda r: validate_optimization_review_result(r, "abcdef1234567"),
        )

    def test_signoff_result_round_trips(self) -> None:
        self.assert_round_trip(
            {
                "outcome": "accepted",
                "summary": "The write-up records the argument faithfully.",
                "findings": [],
                "evidence": ["Compared the write-up against the challenge rounds."],
                "questions": [],
                "recommended_actions": [],
                "files_changed": [],
                "commands_run": [],
            },
            "design-signoff-result.schema.json",
            validate_signoff_result,
        )


class NoRetiredFieldsRemainTests(unittest.TestCase):
    def test_scope_changed_is_gone_from_schema_and_validator(self) -> None:
        schema = load("orchestrator-result.schema.json")
        self.assertNotIn("requirements_scope_changed", schema["properties"])
        source = Path("src/agent_army/publishers.py").read_text()
        self.assertNotIn("requirements_scope_changed", source)

    def test_every_project_owner_state_is_a_workflow_state(self) -> None:
        from agent_army.publishers import PROJECT_OWNER_STATES, WORKFLOW_STATES

        schema_states = set(
            load("orchestrator-result.schema.json")["properties"]["next_state"]["enum"]
        )
        self.assertEqual(schema_states, PROJECT_OWNER_STATES)
        self.assertTrue(schema_states <= WORKFLOW_STATES)


if __name__ == "__main__":
    unittest.main()
