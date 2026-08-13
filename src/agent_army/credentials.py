"""Session-scoped secret access for Agent Army."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path


SecretLoader = Callable[[str], str]

PASS_SOURCE = "pass"
ENV_SOURCE = "env"
SECRET_SOURCES = (PASS_SOURCE, ENV_SOURCE)

DEFAULT_ENV_FILE = Path(".env")
_NON_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9]+")


def environment_variable_name(secret_ref: str) -> str:
    """The environment variable a secret ref maps to.

    `agent-army/project-owner/github-app-private-key` becomes
    `AGENT_ARMY_PROJECT_OWNER_GITHUB_APP_PRIVATE_KEY`, so the mapping is
    predictable from the ref alone with nothing to keep in sync.
    """
    return _NON_NAME_CHARACTERS.sub("_", secret_ref).strip("_").upper()


def load_env_file(env_path: Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    """Parse a dotenv file into a mapping without exporting it globally.

    Values stay in the returned mapping rather than os.environ, so a secret
    is not inherited by every subprocess the orchestrator later spawns --
    including the agent CLIs, which must never receive credentials.
    """
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _restore_pem_newlines(value: str) -> str:
    """A PEM squeezed onto one line is unusable until its newlines return."""
    if "\n" in value or "\\n" not in value:
        return value
    return value.replace("\\n", "\n")


def read_from_pass(secret_ref: str) -> str:
    """Read one password-store entry without logging or writing it to disk."""
    result = subprocess.run(
        ["pass", "show", secret_ref],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def make_env_secret_loader(
    env_path: Path = DEFAULT_ENV_FILE,
    environ: dict[str, str] | None = None,
) -> SecretLoader:
    """Read secrets from the environment or a dotenv file.

    The value may be the key material itself or a path to it, so the same
    setting works whether the operator pastes a PEM inline or points at the
    .pem file GitHub downloaded. This trades pass's at-rest encryption for
    unattended operation: protect the file with its permissions and keep it
    out of version control.
    """
    file_values = load_env_file(env_path)
    ambient = os.environ if environ is None else environ

    def read(secret_ref: str) -> str:
        name = environment_variable_name(secret_ref)
        value = ambient.get(name) or file_values.get(name)
        if not value:
            raise RuntimeError(
                f"No secret for {secret_ref}: set {name} in the environment or {env_path}."
            )
        candidate = Path(value).expanduser()
        if "BEGIN" not in value and candidate.is_file():
            return candidate.read_text(encoding="utf-8")
        return _restore_pem_newlines(value)

    return read


def build_secret_loader(
    source: str = PASS_SOURCE, *, env_path: Path = DEFAULT_ENV_FILE
) -> SecretLoader:
    """Select where credentials come from."""
    if source == PASS_SOURCE:
        return read_from_pass
    if source == ENV_SOURCE:
        return make_env_secret_loader(env_path)
    raise ValueError(f"Unsupported credential source: {source}")


class SessionCredentialBroker:
    """Caches only secrets requested during one orchestrator session.

    The broker is owned by the orchestrator. Agent code receives GitHub clients or
    higher-level operations, never direct access to this object or its values.
    """

    def __init__(self, secret_loader: SecretLoader = read_from_pass) -> None:
        self._secret_loader = secret_loader
        self._secrets: dict[str, str] = {}
        self._closed = False

    def get_secret(self, secret_ref: str) -> str:
        """Return a cached secret or lazily retrieve and cache it from pass."""
        if self._closed:
            raise RuntimeError("The credential broker session is closed.")
        if secret_ref not in self._secrets:
            self._secrets[secret_ref] = self._secret_loader(secret_ref)
        return self._secrets[secret_ref]

    def close(self) -> None:
        """Drop cache references when the orchestrator session ends."""
        self._secrets.clear()
        self._closed = True

    def __enter__(self) -> "SessionCredentialBroker":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

