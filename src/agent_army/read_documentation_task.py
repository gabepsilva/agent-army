"""Read a GitHub issue or pull request for Doku without writing to GitHub."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from agent_army.config import load_github_app_config
from agent_army.credentials import SessionCredentialBroker
from agent_army.github_app import GitHubAppClient, GitHubTarget


GITHUB_URL_PATTERN = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repository>[^/]+)/(?P<kind>issues|pulls)/(?P<number>\d+)/?$"
)
MAX_TEXT_LENGTH = 6_000
MAX_PATCH_LENGTH = 4_000


def parse_target(url: str) -> GitHubTarget:
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
    value = value or ""
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n\n[Truncated after {limit} characters]"


def compact_comment(comment: dict) -> dict:
    return {
        "author": comment.get("user", {}).get("login"),
        "created_at": comment.get("created_at"),
        "body": bounded_text(comment.get("body")),
    }


def build_context(target: GitHubTarget, client: GitHubAppClient) -> dict:
    issue = client.get_issue(target)
    context = {
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
            "comments": [compact_comment(comment) for comment in client.get_issue_comments(target)],
        },
    }

    if target.kind == "pull_request":
        pull_request = client.get_pull_request(target)
        files = client.get_pull_request_files(target)
        reviews = client.get_pull_request_reviews(target)
        context["pull_request"] = {
            "title": pull_request["title"],
            "body": bounded_text(pull_request.get("body")),
            "state": pull_request["state"],
            "base": pull_request["base"]["ref"],
            "head": pull_request["head"]["ref"],
            "reviews": [compact_comment(review) | {"state": review.get("state")} for review in reviews],
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
        paths = [changed_file["filename"] for changed_file in files]
        context["documentation_signals"] = {
            "documentation_files_changed": [
                path for path in paths if path.lower().endswith((".md", ".rst", ".adoc")) or path.startswith("docs/")
            ],
            "test_files_changed": [
                path for path in paths if "test" in path.lower() or "spec" in path.lower()
            ],
        }
    return context


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read an issue or pull request for Doku without writing to GitHub."
    )
    parser.add_argument("url", help="HTTPS GitHub issue or pull-request URL.")
    parser.add_argument(
        "--agent-directory",
        type=Path,
        default=Path("agents/documentation"),
        help="Directory containing agent-config.yaml.",
    )
    args = parser.parse_args()

    try:
        target = parse_target(args.url)
        config = load_github_app_config(args.agent_directory / "agent-config.yaml")
        with SessionCredentialBroker() as broker:
            context = build_context(target, GitHubAppClient(config, broker))
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Read-only task failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    print(json.dumps(context, indent=2))


if __name__ == "__main__":
    main()
