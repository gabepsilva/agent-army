"""Narrow GitHub App API client for Agent Army agents."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import jwt

from agent_army.config import GitHubAppConfig
from agent_army.credentials import SessionCredentialBroker


GITHUB_API_URL = "https://api.github.com"
USER_AGENT = "agent-army"


@dataclass(frozen=True)
class GitHubTarget:
    owner: str
    repository: str
    number: int
    kind: str


class GitHubAppClient:
    """GitHub App client that keeps credentials inside the orchestrator process."""

    def __init__(self, config: GitHubAppConfig, broker: SessionCredentialBroker) -> None:
        self._config = config
        self._broker = broker
        self._app_jwt: str | None = None
        self._installation_token: str | None = None

    def list_repositories(self) -> list[str]:
        response = self._request("GET", "/installation/repositories?per_page=100", self._token())
        return sorted(repository["full_name"] for repository in response["repositories"])

    def get_issue(self, target: GitHubTarget) -> dict[str, Any]:
        return self._request("GET", self._issue_path(target), self._token())

    def get_repository(self, owner: str, repository: str) -> dict[str, Any]:
        return self._request("GET", f"/repos/{owner}/{repository}", self._token())

    def list_open_issues(self, owner: str, repository: str) -> list[dict[str, Any]]:
        """List open issues for one repository, excluding pull requests."""
        issues = self._request(
            "GET",
            f"/repos/{owner}/{repository}/issues?state=open&per_page=100",
            self._token(),
        )
        return [issue for issue in issues if "pull_request" not in issue]

    def get_issue_comments(self, target: GitHubTarget) -> list[dict[str, Any]]:
        return self._request("GET", f"{self._issue_path(target)}/comments?per_page=100", self._token())

    def create_issue_comment(self, target: GitHubTarget, body: str) -> dict[str, Any]:
        """Post one validated orchestration result to its originating issue."""
        if target.kind != "issue":
            raise ValueError("Issue comments can only be posted to issue targets.")
        return self._request(
            "POST", f"{self._issue_path(target)}/comments", self._token(), {"body": body}
        )

    def update_issue_labels(self, target: GitHubTarget, labels: list[str]) -> dict[str, Any]:
        """Replace an issue's labels without exposing credentials to an agent."""
        if target.kind != "issue":
            raise ValueError("Workflow labels can only be changed on issue targets.")
        return self._request(
            "PATCH", self._issue_path(target), self._token(), {"labels": labels}
        )

    def get_pull_request(self, target: GitHubTarget) -> dict[str, Any]:
        return self._request("GET", self._pull_path(target), self._token())

    def get_pull_request_files(self, target: GitHubTarget) -> list[dict[str, Any]]:
        return self._request("GET", f"{self._pull_path(target)}/files?per_page=100", self._token())

    def get_pull_request_reviews(self, target: GitHubTarget) -> list[dict[str, Any]]:
        return self._request("GET", f"{self._pull_path(target)}/reviews?per_page=100", self._token())

    def list_open_pull_requests(
        self, owner: str, repository: str, *, head: str | None = None
    ) -> list[dict[str, Any]]:
        query = "state=open&per_page=100"
        if head:
            query += f"&head={quote(f'{owner}:{head}', safe=':')}"
        return self._request(
            "GET", f"/repos/{owner}/{repository}/pulls?{query}", self._token()
        )

    def create_pull_request(
        self,
        owner: str,
        repository: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{owner}/{repository}/pulls",
            self._token(),
            {"title": title, "head": head, "base": base, "body": body},
        )

    def create_check_run(
        self,
        owner: str,
        repository: str,
        *,
        name: str,
        head_sha: str,
        conclusion: str,
        summary: str,
        details_url: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": name, "summary": summary},
        }
        if details_url:
            body["details_url"] = details_url
        return self._request(
            "POST", f"/repos/{owner}/{repository}/check-runs", self._token(), body
        )

    def get_check_runs(self, owner: str, repository: str, head_sha: str) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            f"/repos/{owner}/{repository}/commits/{head_sha}/check-runs?per_page=100",
            self._token(),
        )
        return list(response.get("check_runs", []))

    def _token(self) -> str:
        if self._installation_token is None:
            installation = self._find_installation()
            response = self._request(
                "POST",
                f"/app/installations/{installation['id']}/access_tokens",
                self._jwt(),
                {},
            )
            self._installation_token = response["token"]
        return self._installation_token

    def _find_installation(self) -> dict[str, Any]:
        installations = self._request("GET", "/app/installations", self._jwt())
        if not installations:
            raise RuntimeError("The GitHub App is not installed on any account or organization.")
        if len(installations) != 1:
            raise RuntimeError(
                "The GitHub App has multiple installations. Select an installation explicitly before use."
            )
        return installations[0]

    def _jwt(self) -> str:
        if self._app_jwt is None:
            private_key = self._broker.get_secret(self._config.private_key_secret_ref)
            now = int(time.time())
            self._app_jwt = jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": str(self._config.app_id)},
                private_key,
                algorithm="RS256",
            )
        return self._app_jwt

    @staticmethod
    def _issue_path(target: GitHubTarget) -> str:
        return f"/repos/{target.owner}/{target.repository}/issues/{target.number}"

    @staticmethod
    def _pull_path(target: GitHubTarget) -> str:
        return f"/repos/{target.owner}/{target.repository}/pulls/{target.number}"

    @staticmethod
    def _request(method: str, path: str, token: str, body: dict[str, Any] | None = None) -> Any:
        payload = json.dumps(body).encode() if body is not None else None
        request = Request(
            f"{GITHUB_API_URL}{path}",
            data=payload,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except (HTTPError, URLError) as error:
            raise RuntimeError(f"GitHub request failed for {path}: {error}") from error
