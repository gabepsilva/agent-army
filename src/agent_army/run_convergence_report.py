"""Command-line entry point for the read-only convergence report."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from agent_army.config import load_github_app_config
from agent_army.convergence_report import ArgumentReport, analyze_issue, render_report
from agent_army.credentials import SessionCredentialBroker
from agent_army.github_app import GitHubAppClient, GitHubTarget


def _parse_repository(repository: str) -> tuple[str, str]:
    owner, _, name = repository.partition("/")
    if not owner or not name:
        raise ValueError("Repository must be given as OWNER/REPOSITORY.")
    return owner, name


def collect_reports(
    client: GitHubAppClient, owner: str, repository: str, *, limit: int | None = None
) -> list[ArgumentReport]:
    """Measure every open issue's argument. Read-only from start to finish."""
    reports: list[ArgumentReport] = []
    issues = sorted(
        client.list_open_issues(owner, repository), key=lambda item: int(item["number"])
    )
    for issue in issues[: limit or len(issues)]:
        number = int(issue["number"])
        target = GitHubTarget(owner, repository, number, "issue")
        comments = client.get_issue_comments(target)
        report = analyze_issue(number, comments)
        if report.pull_request is not None:
            report = _measure_pull_request(client, owner, repository, comments, report)
        reports.append(report)
    return reports


def _measure_pull_request(
    client: GitHubAppClient,
    owner: str,
    repository: str,
    comments: list[dict],
    report: ArgumentReport,
) -> ArgumentReport:
    """Re-run the analysis with real diff sizes attached."""
    pull_target = GitHubTarget(owner, repository, report.pull_request or 0, "pull_request")
    try:
        files = client.get_pull_request_files(pull_target)
    except Exception:
        return report
    diff_lines = sum(
        int(entry.get("additions", 0)) + int(entry.get("deletions", 0)) for entry in files
    )

    def compare_lines(base: str, head: str) -> int:
        try:
            comparison = client.compare_commits(owner, repository, base, head)
        except Exception:
            # Unknown is not the same as empty; skip the check rather than
            # flagging an argument on a failed API call.
            return EMPTY_FIX_SENTINEL
        return sum(
            int(entry.get("additions", 0)) + int(entry.get("deletions", 0))
            for entry in comparison.get("files", [])
        )

    return analyze_issue(
        report.issue_number, comments, diff_lines=diff_lines, compare_lines=compare_lines
    )


# Large enough that a failed comparison can never look like an empty fix.
EMPTY_FIX_SENTINEL = 1_000_000


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report on Developer/Reviewer convergence. Makes no GitHub writes."
    )
    parser.add_argument("--repository", required=True, help="OWNER/REPOSITORY to report on.")
    parser.add_argument(
        "--agent-directory",
        type=Path,
        default=Path("agents/optimization-reviewer"),
        help="Agent directory used only for GitHub App authentication.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Report on at most N issues.")
    args = parser.parse_args()
    try:
        owner, repository = _parse_repository(args.repository)
        config = load_github_app_config(args.agent_directory / "agent-config.yaml")
        with SessionCredentialBroker() as broker:
            client = GitHubAppClient(config, broker)
            reports = collect_reports(client, owner, repository, limit=args.limit)
        print(render_report(reports))
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Convergence report failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
