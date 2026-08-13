"""Run Doku end-to-end for one issue without persisting its JSON result."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from agent_army.codex_executor import CodexCliExecutor, CodexExecutionRequest
from agent_army.config import load_github_app_config
from agent_army.credentials import SessionCredentialBroker
from agent_army.documentation_context import add_documentation_signals
from agent_army.github_app import GitHubAppClient
from agent_army.publishers import render_documentation_analysis
from agent_army.work_items import WorkItemReader, parse_github_target


def run(issue_url: str, agent_directory: Path, workspace: Path, schema_path: Path) -> str:
    """Run Doku's issue-grooming flow and return the published comment URL."""
    target = parse_github_target(issue_url)
    if target.kind != "issue":
        raise ValueError("Doku's first end-to-end workflow accepts issue URLs only.")

    config = load_github_app_config(agent_directory / "agent-config.yaml")
    role_path = agent_directory / "ROLE.md"
    with SessionCredentialBroker() as broker:
        github = GitHubAppClient(config, broker)
        work_item = WorkItemReader(github).read(target)
        analysis = CodexCliExecutor().execute(
            CodexExecutionRequest(
                role_path=role_path,
                workspace=workspace,
                work_item=add_documentation_signals(work_item),
                output_schema_path=schema_path,
            )
        )
        comment = render_documentation_analysis(analysis)
        response = github.create_issue_comment(target, comment)
    return response["html_url"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Doku on one GitHub issue and publish its analysis."
    )
    parser.add_argument("issue_url", help="HTTPS GitHub issue URL.")
    parser.add_argument(
        "--workspace", type=Path, required=True, help="Git repository workspace for Codex."
    )
    parser.add_argument(
        "--agent-directory",
        type=Path,
        default=Path("agents/documentation"),
        help="Directory containing Doku's role card and configuration.",
    )
    parser.add_argument(
        "--output-schema",
        type=Path,
        default=Path("schemas/agent-analysis.schema.json"),
        help="JSON Schema for Doku's final result.",
    )
    args = parser.parse_args()
    try:
        comment_url = run(args.issue_url, args.agent_directory, args.workspace, args.output_schema)
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Doku workflow failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(f"Doku published its analysis: {comment_url}")


if __name__ == "__main__":
    main()
