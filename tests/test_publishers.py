import unittest

from agent_army.publishers import render_documentation_analysis


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
