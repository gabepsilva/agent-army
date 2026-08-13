import unittest

from agent_army.work_items import parse_github_target


class DocumentationWorkflowInputTests(unittest.TestCase):
    def test_issue_target_is_accepted(self) -> None:
        target = parse_github_target("https://github.com/acme/widgets/issues/1")
        self.assertEqual(target.kind, "issue")

    def test_pull_request_target_is_not_an_issue_workflow_target(self) -> None:
        target = parse_github_target("https://github.com/acme/widgets/pulls/1")
        self.assertNotEqual(target.kind, "issue")
