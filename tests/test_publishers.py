import unittest

from agent_army.publishers import (
    REVIEW_OUTCOME_STATES,
    normalize_escaped_newlines,
    render_documentation_analysis,
    validate_analysis_result,
    validate_optimization_review_result,
)


class PlaceholderTextRejectionTests(unittest.TestCase):
    """Regression coverage for issue #9: a schema-valid but content-empty
    Project Owner result ("test" / ["a"] / ["a"]) was published as the
    durable decision on a real issue. The schema only enforces shape, so
    these checks are the only thing standing between that and a repeat."""

    @staticmethod
    def _result(**overrides) -> dict:
        result = {
            "summary": "Resolved the round-1 requirements challenge with concrete decisions.",
            "evidence": ["orchestrator.py:_select_task enumerates the ineligibility branches."],
            "questions": [],
            "recommended_actions": ["Developer should implement per the decisions above."],
            "files_changed": [],
            "commands_run": [],
        }
        result.update(overrides)
        return result

    def test_accepts_a_real_result(self) -> None:
        validate_analysis_result(self._result())

    def test_rejects_the_actual_placeholder_summary_from_issue_9(self) -> None:
        with self.assertRaises(ValueError):
            validate_analysis_result(self._result(summary="test"))

    def test_rejects_single_letter_evidence_items(self) -> None:
        with self.assertRaises(ValueError):
            validate_analysis_result(self._result(evidence=["a"]))

    def test_rejects_single_letter_recommended_actions(self) -> None:
        with self.assertRaises(ValueError):
            validate_analysis_result(self._result(recommended_actions=["a"]))

    def test_rejects_blocklisted_filler_regardless_of_length(self) -> None:
        with self.assertRaises(ValueError):
            validate_analysis_result(self._result(summary="n/a"))

    def test_identifier_fields_are_not_length_checked(self) -> None:
        # files_changed/commands_run are identifiers, not prose, and can be
        # legitimately short (e.g. a filename); they're intentionally
        # excluded from the placeholder-text guard.
        validate_analysis_result(self._result(files_changed=["a.py"], commands_run=["ls"]))


class EscapedNewlineRepairTests(unittest.TestCase):
    """Issue #13's first Project Owner comment shipped 31 literal backslash-n
    sequences, rendering the durable decision as one unreadable blob."""

    def test_repairs_a_wholly_escaped_string(self) -> None:
        self.assertEqual(
            normalize_escaped_newlines("Decision.\\n\\n**Scope**\\n- item"),
            "Decision.\n\n**Scope**\n- item",
        )

    def test_leaves_inline_code_alone(self) -> None:
        # A finding arguing for one line ending over another must survive.
        text = "prefer `\\n` over `\\r\\n` in the writer"
        self.assertEqual(normalize_escaped_newlines(text), text)

    def test_leaves_properly_formatted_text_alone(self) -> None:
        # Real newlines present means the agent formatted it; any remaining
        # escape is deliberate content, not the bug.
        text = "Real line\nand a literal \\n token"
        self.assertEqual(normalize_escaped_newlines(text), text)

    def test_text_without_escapes_is_untouched(self) -> None:
        self.assertEqual(normalize_escaped_newlines("plain summary"), "plain summary")

    def test_rendered_comment_has_no_literal_escapes(self) -> None:
        rendered = render_documentation_analysis(
            {
                "summary": "Needs clarification.\\n\\nSecond paragraph here.",
                "evidence": ["README exists.\\n- nested point"],
                "questions": ["Who is the audience?"],
                "recommended_actions": ["Clarify scope."],
                "files_changed": [],
                "commands_run": ["tests"],
            }
        )

        self.assertNotIn("\\n", rendered)
        self.assertIn("Second paragraph here.", rendered)


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
