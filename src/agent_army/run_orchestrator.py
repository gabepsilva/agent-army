"""Command-line entry point for the long-running issue polling service."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from agent_army.config import load_github_app_config
from agent_army.credentials import SessionCredentialBroker
from agent_army.github_app import GitHubAppClient
from agent_army.orchestrator import IssueOrchestrator, format_dry_run_report


def run(
    repository: str,
    workspace: Path,
    *,
    project_owner_directory: Path,
    documentation_directory: Path,
    developer_directory: Path,
    reviewer_directory: Path,
    output_schema_path: Path,
    developer_output_schema_path: Path,
    reviewer_output_schema_path: Path,
    requirements_challenge_output_schema_path: Path,
    poll_interval: float = 60.0,
    once: bool = False,
    dry_run: bool = False,
) -> str:
    """Start the configured polling service and return a one-shot status."""
    owner_config = load_github_app_config(project_owner_directory / "agent-config.yaml")
    documentation_config = load_github_app_config(documentation_directory / "agent-config.yaml")
    developer_config = load_github_app_config(developer_directory / "agent-config.yaml")
    reviewer_config = load_github_app_config(reviewer_directory / "agent-config.yaml")
    with SessionCredentialBroker() as broker:
        orchestrator = IssueOrchestrator(
            repository=repository,
            project_owner_github=GitHubAppClient(owner_config, broker),
            documentation_github=GitHubAppClient(documentation_config, broker),
            developer_github=GitHubAppClient(developer_config, broker),
            reviewer_github=GitHubAppClient(reviewer_config, broker),
            workspace=workspace,
            project_owner_role=project_owner_directory / "ROLE.md",
            documentation_role=documentation_directory / "ROLE.md",
            developer_role=developer_directory / "ROLE.md",
            reviewer_role=reviewer_directory / "ROLE.md",
            output_schema_path=output_schema_path,
            developer_output_schema_path=developer_output_schema_path,
            reviewer_output_schema_path=reviewer_output_schema_path,
            requirements_challenge_output_schema_path=requirements_challenge_output_schema_path,
        )
        if dry_run:
            print(format_dry_run_report(orchestrator.run_dry_run()))
            return "dry-run"
        if once:
            return orchestrator.run_once().status
        orchestrator.run_forever(poll_interval)
    return "stopped"


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll GitHub and orchestrate Agent Army roles.")
    parser.add_argument("--repository", required=True, help="GitHub repository in OWNER/REPOSITORY form.")
    parser.add_argument("--workspace", type=Path, required=True, help="Git repository workspace for Codex.")
    parser.add_argument(
        "--project-owner-directory",
        type=Path,
        default=Path("agents/project-owner"),
        help="Directory containing Project Owner ROLE.md and agent-config.yaml.",
    )
    parser.add_argument(
        "--documentation-directory",
        type=Path,
        default=Path("agents/documentation"),
        help="Directory containing Doku ROLE.md and agent-config.yaml.",
    )
    parser.add_argument(
        "--developer-directory",
        type=Path,
        default=Path("agents/developer"),
        help="Directory containing Developer ROLE.md and agent-config.yaml.",
    )
    parser.add_argument(
        "--reviewer-directory",
        type=Path,
        default=Path("agents/optimization-reviewer"),
        help="Directory containing Optimization Reviewer ROLE.md and agent-config.yaml.",
    )
    parser.add_argument(
        "--output-schema",
        type=Path,
        default=Path("schemas/orchestrator-result.schema.json"),
        help="JSON Schema for orchestrated agent results.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=60.0,
        help="Seconds between polls (default: 60).",
    )
    parser.add_argument(
        "--developer-output-schema",
        type=Path,
        default=Path("schemas/developer-result.schema.json"),
        help="JSON Schema for Developer's final result.",
    )
    parser.add_argument(
        "--reviewer-output-schema",
        type=Path,
        default=Path("schemas/optimization-review-result.schema.json"),
        help="JSON Schema for Optimization Reviewer's final result.",
    )
    parser.add_argument(
        "--requirements-challenge-output-schema",
        type=Path,
        default=Path("schemas/requirements-challenge-result.schema.json"),
        help="JSON Schema for the Optimization Reviewer's issue-level challenge result.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one eligible issue and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview which issue and agent the next polling pass would select, "
            "without invoking an agent or writing to GitHub. Requires --once."
        ),
    )
    args = parser.parse_args()

    if args.dry_run and not args.once:
        print("Agent Army orchestrator failed: --dry-run requires --once.", file=sys.stderr)
        raise SystemExit(1)

    try:
        status = run(
            args.repository,
            args.workspace,
            project_owner_directory=args.project_owner_directory,
            documentation_directory=args.documentation_directory,
            developer_directory=args.developer_directory,
            reviewer_directory=args.reviewer_directory,
            output_schema_path=args.output_schema,
            developer_output_schema_path=args.developer_output_schema,
            reviewer_output_schema_path=args.reviewer_output_schema,
            requirements_challenge_output_schema_path=args.requirements_challenge_output_schema,
            poll_interval=args.poll_interval,
            once=args.once,
            dry_run=args.dry_run,
        )
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Agent Army orchestrator failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    if args.once and not args.dry_run:
        print(f"Agent Army one-shot poll: {status}")


if __name__ == "__main__":
    main()
