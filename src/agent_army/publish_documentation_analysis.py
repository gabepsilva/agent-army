"""Publish one validated Doku issue-grooming analysis."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from agent_army.config import load_github_app_config
from agent_army.config import load_runtime_config
from agent_army.credentials import SessionCredentialBroker, build_secret_loader
from agent_army.github_app import GitHubAppClient
from agent_army.publishers import render_documentation_analysis
from agent_army.work_items import parse_github_target


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish a validated Doku analysis to an issue.")
    parser.add_argument("issue_url", help="HTTPS GitHub issue URL.")
    parser.add_argument("--analysis", type=Path, required=True, help="Doku JSON result file.")
    parser.add_argument(
        "--agent-directory",
        type=Path,
        default=Path("agents/documentation"),
        help="Directory containing agent-config.yaml.",
    )
    args = parser.parse_args()

    try:
        target = parse_github_target(args.issue_url)
        if target.kind != "issue":
            raise ValueError("Provide an issue URL; pull-request publication is not enabled yet.")
        analysis = json.loads(args.analysis.read_text(encoding="utf-8"))
        comment = render_documentation_analysis(analysis)
        config = load_github_app_config(args.agent_directory / "agent-config.yaml")
        with SessionCredentialBroker(
            build_secret_loader(
                (_rc := load_runtime_config()).credentials_source, env_path=_rc.env_file
            )
        ) as broker:
            response = GitHubAppClient(config, broker).create_issue_comment(target, comment)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as error:
        print(f"Analysis publication failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    print(f"Published Doku analysis: {response['html_url']}")


if __name__ == "__main__":
    main()
