"""Agent configuration loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: int
    private_key_secret_ref: str


def load_github_app_config(config_path: Path) -> GitHubAppConfig:
    with config_path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)

    try:
        github_app = config["github_app"]
        return GitHubAppConfig(
            app_id=int(github_app["app_id"]),
            private_key_secret_ref=str(github_app["private_key_secret_ref"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid GitHub App configuration in {config_path}") from error

