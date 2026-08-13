import unittest

from agent_army.read_documentation_task import parse_target


class ParseTargetTests(unittest.TestCase):
    def test_parses_issue_url(self) -> None:
        target = parse_target("https://github.com/acme/widgets/issues/17")
        self.assertEqual((target.owner, target.repository, target.number, target.kind), ("acme", "widgets", 17, "issue"))

    def test_parses_pull_request_url(self) -> None:
        target = parse_target("https://github.com/acme/widgets/pulls/18")
        self.assertEqual((target.owner, target.repository, target.number, target.kind), ("acme", "widgets", 18, "pull_request"))

    def test_rejects_non_github_urls(self) -> None:
        with self.assertRaises(ValueError):
            parse_target("https://example.com/acme/widgets/issues/17")
