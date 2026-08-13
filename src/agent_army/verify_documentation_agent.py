"""Verify that the documentation agent can authenticate as its GitHub App."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from agent_army.config import GitHubAppConfig, load_github_app_config
from agent_army.config import load_runtime_config
from agent_army.credentials import SessionCredentialBroker, build_secret_loader
from agent_army.github_app import GitHubAppClient


def verify(agent_directory: Path) -> list[str]:
    config_path = agent_directory / "agent-config.yaml"
    role_path = agent_directory / "ROLE.md"
    if not role_path.is_file():
        raise FileNotFoundError(f"Missing role card: {role_path}")
    if not role_path.read_text(encoding="utf-8").strip():
        raise ValueError(f"Role card is empty: {role_path}")

    with SessionCredentialBroker(
            build_secret_loader(
                (_rc := load_runtime_config()).credentials_source, env_path=_rc.env_file
            )
        ) as broker:
        return GitHubAppClient(load_github_app_config(config_path), broker).list_repositories()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify the documentation agent's GitHub App authentication."
    )
    parser.add_argument(
        "--agent-directory",
        type=Path,
        default=Path("agents/documentation"),
        help="Directory containing ROLE.md and agent-config.yaml.",
    )
    args = parser.parse_args()

    try:
        repositories = verify(args.agent_directory)
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        print(f"Verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    print("Documentation agent authentication succeeded.")
    if repositories:
        print("Installed repositories:")
        print("\n".join(f"- {repository}" for repository in repositories))
    else:
        print("The GitHub App installation has no repository access.")


if __name__ == "__main__":
    main()
