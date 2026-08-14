"""Run Doku end-to-end for one issue without persisting its JSON result."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from agent_army.agent_executor import (
    AgentExecutionRequest,
    build_executor,
    role_reference_paths,
)
from agent_army.config import (
    BACKENDS,
    DEFAULT_CONFIG_PATH,
    RuntimeConfig,
    load_agent_runtime_config,
    load_github_app_config,
    load_runtime_config,
)
from agent_army.credentials import SessionCredentialBroker, build_secret_loader
from agent_army.documentation_context import add_documentation_signals
from agent_army.github_app import GitHubAppClient
from agent_army.publishers import render_documentation_analysis
from agent_army.work_items import WorkItemReader, parse_github_target


def run(
    issue_url: str,
    agent_directory: Path,
    workspace: Path,
    schema_path: Path,
    runtime_config: RuntimeConfig | None = None,
) -> str:
    """Run Doku's issue-grooming flow and return the published comment URL."""
    target = parse_github_target(issue_url)
    if target.kind != "issue":
        raise ValueError("Doku's first end-to-end workflow accepts issue URLs only.")

    agent_config_path = agent_directory / "agent-config.yaml"
    config = load_github_app_config(agent_config_path)
    runtime = load_agent_runtime_config(agent_config_path, runtime_config or RuntimeConfig())
    role_path = agent_directory / "ROLE.md"
    with SessionCredentialBroker(
            build_secret_loader(
                (_rc := load_runtime_config()).credentials_source, env_path=_rc.env_file
            )
        ) as broker:
        github = GitHubAppClient(config, broker)
        work_item = WorkItemReader(github).read(target)
        execution = build_executor(runtime).execute(
            AgentExecutionRequest(
                role_path=role_path,
                workspace=workspace,
                work_item=add_documentation_signals(work_item),
                output_schema_path=schema_path,
                reference_paths=role_reference_paths(role_path),
            )
        )
        comment = render_documentation_analysis(execution.output)
        response = github.create_issue_comment(target, comment)
    return response["html_url"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Doku on one GitHub issue and publish its analysis."
    )
    parser.add_argument("issue_url", help="HTTPS GitHub issue URL.")
    parser.add_argument(
        "--workspace", type=Path, required=True, help="Git repository workspace for the agent."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Runtime configuration file (default: config.yaml).",
    )
    parser.add_argument(
        "--backend",
        choices=BACKENDS,
        default=None,
        help="Override the backend selected by the configuration file.",
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
        comment_url = run(
            args.issue_url,
            args.agent_directory,
            args.workspace,
            args.output_schema,
            load_runtime_config(args.config).with_backend(args.backend),
        )
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Doku workflow failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(f"Doku published its analysis: {comment_url}")


if __name__ == "__main__":
    main()
