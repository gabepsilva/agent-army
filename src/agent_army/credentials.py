"""Session-scoped secret access for Agent Army."""

from __future__ import annotations

import subprocess
from collections.abc import Callable


SecretLoader = Callable[[str], str]


def read_from_pass(secret_ref: str) -> str:
    """Read one password-store entry without logging or writing it to disk."""
    result = subprocess.run(
        ["pass", "show", secret_ref],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


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

