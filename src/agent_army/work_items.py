"""Reusable, read-only GitHub work-item retrieval for all agents."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from agent_army.github_app import GitHubAppClient, GitHubTarget


GITHUB_URL_PATTERN = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repository>[^/]+)/(?P<kind>issues|pulls)/(?P<number>\d+)/?$"
)
MAX_TEXT_LENGTH = 6_000
MAX_PATCH_LENGTH = 4_000


def parse_github_target(url: str) -> GitHubTarget:
    """Parse one HTTPS GitHub issue or pull-request URL."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise ValueError("Provide an HTTPS GitHub issue or pull-request URL.")
    match = GITHUB_URL_PATTERN.match(parsed.path)
    if match is None:
        raise ValueError("URL must point to a GitHub issue or pull request.")
    return GitHubTarget(
        owner=match["owner"],
        repository=match["repository"],
        number=int(match["number"]),
        kind="pull_request" if match["kind"] == "pulls" else "issue",
    )


def bounded_text(value: str | None, limit: int = MAX_TEXT_LENGTH) -> str:
    """Bound API text before it reaches an agent context."""
    value = value or ""
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n\n[Truncated after {limit} characters]"


def compact_comment(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        # The id is what lets the orchestrator stamp or revise a specific
        # comment later; it is an identifier, not content.
        "id": comment.get("id"),
        "author": comment.get("user", {}).get("login"),
        "created_at": comment.get("created_at"),
        "body": bounded_text(comment.get("body")),
    }


class WorkItemReader:
    """Fetches one normalized work item for any role to interpret."""

    def __init__(self, client: GitHubAppClient) -> None:
        self._client = client

    def read(self, target: GitHubTarget) -> dict[str, Any]:
        issue = self._client.get_issue(target)
        work_item: dict[str, Any] = {
            "target": {
                "kind": target.kind,
                "repository": f"{target.owner}/{target.repository}",
                "number": target.number,
                "url": issue["html_url"],
            },
            "issue": {
                "title": issue["title"],
                "body": bounded_text(issue.get("body")),
                "state": issue["state"],
                "labels": [label["name"] for label in issue.get("labels", [])],
                "comments": [
                    compact_comment(comment)
                    for comment in self._client.get_issue_comments(target)
                ],
            },
        }
        if target.kind == "pull_request":
            work_item["pull_request"] = self._read_pull_request(target)
        return work_item

    def _read_pull_request(self, target: GitHubTarget) -> dict[str, Any]:
        pull_request = self._client.get_pull_request(target)
        files = self._client.get_pull_request_files(target)
        reviews = self._client.get_pull_request_reviews(target)
        return {
            "title": pull_request["title"],
            "body": bounded_text(pull_request.get("body")),
            "state": pull_request["state"],
            "base": pull_request["base"]["ref"],
            "head": pull_request["head"]["ref"],
            "base_sha": pull_request["base"]["sha"],
            "head_sha": pull_request["head"]["sha"],
            "reviews": [
                compact_comment(review) | {"state": review.get("state")} for review in reviews
            ],
            "changed_files": [
                {
                    "path": changed_file["filename"],
                    "status": changed_file["status"],
                    "additions": changed_file["additions"],
                    "deletions": changed_file["deletions"],
                    "patch": bounded_text(changed_file.get("patch"), MAX_PATCH_LENGTH),
                }
                for changed_file in files
            ],
        }
