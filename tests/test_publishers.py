import unittest

from agent_army.publishers import (
    REVIEW_OUTCOME_STATES,
    render_documentation_analysis,
    validate_optimization_review_result,
)


class DocumentationAnalysisPublisherTests(unittest.TestCase):
    def test_renders_human_readable_comment(self) -> None:
        rendered = render_documentation_analysis(
            {
                "summary": "Needs clarification.",
                "evidence": ["README exists."],
                "questions": ["Who is the audience?"],
                "recommended_actions": ["Clarify scope."],
                "files_changed": [],
                "commands_run": ["tests"],
            }
        )

        self.assertIn("## Doku: documentation analysis", rendered)
        self.assertIn("### Questions to resolve", rendered)
        self.assertNotIn("commands_run", rendered)

    def test_optimization_review_requires_exact_commit_and_maps_outcomes(self) -> None:
        review = {
            "outcome": "approved",
            "reviewed_commit": "abcdef1234567",
            "summary": "No blocking findings.",
            "evidence": ["Tests passed."],
            "questions": [],
            "recommended_actions": [],
            "files_changed": [],
            "commands_run": ["uv run python -m unittest"],
        }

        validate_optimization_review_result(review, "abcdef1234567")
        self.assertEqual(REVIEW_OUTCOME_STATES["approved"], "ready-for-merge")
        self.assertEqual(REVIEW_OUTCOME_STATES["changes-requested"], "ready-for-development")
        self.assertEqual(REVIEW_OUTCOME_STATES["unable-to-assess"], "needs-decision")
        with self.assertRaises(ValueError):
            validate_optimization_review_result(review, "different-commit")
