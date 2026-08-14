"""Agent configuration loading."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from agent_army.credentials import SECRET_SOURCES


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


CODEX_BACKEND = "codex"
CLAUDE_BACKEND = "claude"
BACKENDS = (CODEX_BACKEND, CLAUDE_BACKEND)

# Permission modes that make sense without a terminal to answer prompts.
# "plan" and "manual" are rejected because -p mode cannot satisfy them.
CLAUDE_PERMISSION_MODES = ("acceptEdits", "auto", "dontAsk", "bypassPermissions")

# Matches `claude --effort`.
CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

DEFAULT_CONFIG_PATH = Path("config.yaml")


@dataclass(frozen=True)
class CodexBackendConfig:
    sandbox: str = "workspace-write"
    # Reserved, not yet wired into CodexCliExecutor's command line. Codex takes
    # a model via -m/--model and reasoning effort via `-c
    # model_reasoning_effort=<level>`; these fields exist so agent-config.yaml
    # can carry the same shape as the claude section ahead of that wiring.
    model: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class ClaudeBackendConfig:
    permission_mode: str = "bypassPermissions"
    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    """Which agent CLI runs a role card, and how it is confined."""

    backend: str = CODEX_BACKEND
    codex: CodexBackendConfig = CodexBackendConfig()
    claude: ClaudeBackendConfig = ClaudeBackendConfig()
    # Where GitHub App private keys are read from. "pass" keeps them
    # encrypted at rest but needs an interactive unlock; "env" trades that
    # for unattended operation.
    credentials_source: str = "pass"
    env_file: Path = Path(".env")

    def with_backend(self, backend: str | None) -> "RuntimeConfig":
        """Return this configuration with a command-line backend override applied."""
        if backend is None:
            return self
        return replace(self, backend=_validated_backend(backend, DEFAULT_CONFIG_PATH))


def load_runtime_config(config_path: Path = DEFAULT_CONFIG_PATH) -> RuntimeConfig:
    """Load the repository-wide runtime defaults.

    A missing config.yaml is not an error: it yields the same behavior the
    project had before backends were selectable.
    """
    if not config_path.is_file():
        return RuntimeConfig()
    config = _mapping(_read_yaml(config_path), "configuration", config_path)
    return _merge_runtime(config, RuntimeConfig(), config_path)


def load_agent_runtime_config(
    config_path: Path, base: RuntimeConfig | None = None
) -> RuntimeConfig:
    """Layer one agent's `runtime:` section over the repository-wide defaults.

    Precedence runs most-specific-first: a command-line --backend beats an
    agent's own setting, which beats config.yaml, which beats the built-in
    defaults. Every key is optional, so an agent-config.yaml with no `runtime:`
    section simply inherits.
    """
    base = base or RuntimeConfig()
    if not config_path.is_file():
        return base
    config = _mapping(_read_yaml(config_path), "configuration", config_path)
    runtime = config.get("runtime")
    if runtime is None:
        return base
    return _merge_runtime(_mapping(runtime, "'runtime'", config_path), base, config_path)


def _read_yaml(config_path: Path) -> Any:
    with config_path.open(encoding="utf-8") as config_file:
        return yaml.safe_load(config_file) or {}


def _mapping(value: Any, label: str, config_path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {label} in {config_path}: expected a mapping.")
    return value


def _merge_runtime(
    section: dict[str, Any], base: RuntimeConfig, config_path: Path
) -> RuntimeConfig:
    backends = section.get("backends")
    if backends is None:
        # An agent's `runtime:` section nests backend settings directly.
        backends = section
    backends = _mapping(backends, "'backends'", config_path)
    credentials = _mapping(section.get("credentials") or {}, "'credentials'", config_path)
    source = str(credentials.get("source", base.credentials_source))
    if source not in SECRET_SOURCES:
        raise ValueError(
            f"Unknown credential source {source!r} in {config_path}; "
            f"expected one of {', '.join(SECRET_SOURCES)}."
        )
    return RuntimeConfig(
        backend=_validated_backend(section.get("backend", base.backend), config_path),
        codex=_codex_config(backends.get(CODEX_BACKEND) or {}, base.codex, config_path),
        claude=_claude_config(backends.get(CLAUDE_BACKEND) or {}, base.claude, config_path),
        credentials_source=source,
        env_file=Path(str(credentials.get("env_file", base.env_file))),
    )


def _validated_backend(backend: Any, config_path: Path) -> str:
    if backend not in BACKENDS:
        raise ValueError(
            f"Unknown backend {backend!r} in {config_path}; expected one of {', '.join(BACKENDS)}."
        )
    return str(backend)


def _codex_config(
    section: Any, base: CodexBackendConfig, config_path: Path
) -> CodexBackendConfig:
    section = _mapping(section, "'codex' backend settings", config_path)
    sandbox = section.get("sandbox", base.sandbox)
    if not sandbox or not isinstance(sandbox, str):
        raise ValueError(f"Invalid codex 'sandbox' in {config_path}: expected a string.")

    model = section.get("model", base.model)
    if model is not None and not isinstance(model, str):
        raise ValueError(f"Invalid codex 'model' in {config_path}: expected a string.")

    reasoning_effort = section.get("reasoning_effort", base.reasoning_effort)
    if reasoning_effort is not None and not isinstance(reasoning_effort, str):
        raise ValueError(
            f"Invalid codex 'reasoning_effort' in {config_path}: expected a string."
        )

    return CodexBackendConfig(
        sandbox=sandbox, model=model or None, reasoning_effort=reasoning_effort or None
    )


def _claude_config(
    section: Any, base: ClaudeBackendConfig, config_path: Path
) -> ClaudeBackendConfig:
    section = _mapping(section, "'claude' backend settings", config_path)

    permission_mode = section.get("permission_mode", base.permission_mode)
    if permission_mode not in CLAUDE_PERMISSION_MODES:
        raise ValueError(
            f"Unknown claude 'permission_mode' {permission_mode!r} in {config_path}; "
            f"expected one of {', '.join(CLAUDE_PERMISSION_MODES)}."
        )

    model = section.get("model", base.model)
    if model is not None and not isinstance(model, str):
        raise ValueError(f"Invalid claude 'model' in {config_path}: expected a string.")

    effort = section.get("effort", base.effort)
    if effort is not None and effort not in CLAUDE_EFFORT_LEVELS:
        raise ValueError(
            f"Unknown claude 'effort' {effort!r} in {config_path}; "
            f"expected one of {', '.join(CLAUDE_EFFORT_LEVELS)}."
        )

    return ClaudeBackendConfig(permission_mode=permission_mode, model=model or None, effort=effort)
