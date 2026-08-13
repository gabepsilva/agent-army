"""Read one reusable GitHub work item without writing to GitHub."""

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
from agent_army.work_items import WorkItemReader, parse_github_target


def read_work_item(url: str, agent_directory: Path) -> dict:
    target = parse_github_target(url)
    config = load_github_app_config(agent_directory / "agent-config.yaml")
    with SessionCredentialBroker(
            build_secret_loader(
                (_rc := load_runtime_config()).credentials_source, env_path=_rc.env_file
            )
        ) as broker:
        return WorkItemReader(GitHubAppClient(config, broker)).read(target)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read a GitHub work item without writing.")
    parser.add_argument("url", help="HTTPS GitHub issue or pull-request URL.")
    parser.add_argument(
        "--agent-directory",
        type=Path,
        default=Path("agents/documentation"),
        help="Agent directory used only for GitHub App authentication.",
    )
    args = parser.parse_args()
    try:
        print(json.dumps(read_work_item(args.url, args.agent_directory), indent=2))
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Work-item read failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
