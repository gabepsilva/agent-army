"""Polling orchestration for the issue-driven Agent Army workflow."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_army.codex_executor import (
    CodexCliExecutor,
    CodexExecutionRequest,
    role_reference_paths,
)
from agent_army.github_app import GitHubAppClient, GitHubTarget
from agent_army.publishers import (
    WORKFLOW_STATES,
    render_orchestration_result,
    validate_orchestration_result,
)
from agent_army.work_items import WorkItemReader


PROJECT_OWNER = "project-owner"
DOCUMENTATION = "documentation"
SOURCE_UNLABELED = "unlabeled"
ORCHESTRATION_PAUSED_LABEL = "orchestration-paused"


@dataclass(frozen=True)
class OrchestratedAgent:
    """The orchestrator-owned runtime details for one role."""

    name: str
    mode: str
    role_path: Path
    github: GitHubAppClient


@dataclass(frozen=True)
class OrchestrationOutcome:
    """The result of one polling pass over the first eligible issue."""

    status: str
    issue_number: int | None = None
    role: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _Task:
    agent: OrchestratedAgent
    source_state: str


MARKER_PATTERN = re.compile(r"<!-- agent-army:(?P<kind>\w+) (?P<attributes>[^>]+?) -->")
ATTRIBUTE_PATTERN = re.compile(r"(?P<key>[a-z_]+)=(?P<value>[^\s]+)")


class IssueOrchestrator:
    """Poll one repository and run at most one state-changing task per pass.

    GitHub clients are intentionally injected separately from the Codex executor.
    The clients retain credentials inside this Python process; only normalized
    work-item data is sent to Codex.
    """

    def __init__(
        self,
        *,
        repository: str,
        project_owner_github: GitHubAppClient,
        documentation_github: GitHubAppClient,
        workspace: Path,
        project_owner_role: Path,
        documentation_role: Path,
        output_schema_path: Path,
        executor: CodexCliExecutor | None = None,
        work_item_reader: WorkItemReader | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.owner, self.repository = _parse_repository(repository)
        self._project_owner_github = project_owner_github
        self._workspace = workspace
        self._output_schema_path = output_schema_path
        self._executor = executor or CodexCliExecutor()
        self._reader = work_item_reader or WorkItemReader(project_owner_github)
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._agents = {
            PROJECT_OWNER: OrchestratedAgent(
                PROJECT_OWNER, "issue_intake", project_owner_role, project_owner_github
            ),
            DOCUMENTATION: OrchestratedAgent(
                DOCUMENTATION, "issue_grooming", documentation_role, documentation_github
            ),
        }

    def run_once(self) -> OrchestrationOutcome:
        """Process the lowest-numbered eligible open issue, if there is one."""
        issues = self._project_owner_github.list_open_issues(self.owner, self.repository)
        for issue in sorted(issues, key=lambda item: int(item["number"])):
            target = GitHubTarget(self.owner, self.repository, int(issue["number"]), "issue")
            work_item = self._reader.read(target)
            task = self._select_task(work_item)
            if task is None:
                continue
            return self._run_task(target, work_item, task)
        return OrchestrationOutcome("idle")

    def run_forever(
        self,
        poll_interval: float,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        reporter: Callable[[str], None] = print,
    ) -> None:
        """Keep polling until interrupted, isolating one poll's failure."""
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero.")
        while True:
            try:
                outcome = self.run_once()
                reporter(_format_outcome(outcome))
            except Exception as error:  # keep a transient API failure from killing the service
                reporter(f"Agent Army polling failed; retrying: {error}")
            sleeper(poll_interval)

    def _select_task(self, work_item: dict[str, Any]) -> _Task | None:
        issue = work_item["issue"]
        labels = [str(label) for label in issue.get("labels", [])]
        if ORCHESTRATION_PAUSED_LABEL in labels:
            # This human-controlled guard wins over intake and every workflow state.
            return None
        workflow_labels = [label for label in labels if label in WORKFLOW_STATES]
        comments = issue.get("comments", [])

        if len(workflow_labels) > 1:
            # Ambiguous state is left untouched until a human restores the invariant.
            return None
        if not workflow_labels:
            if self._has_completed_intake(comments):
                return None
            return _Task(self._agents[PROJECT_OWNER], SOURCE_UNLABELED)

        current_state = workflow_labels[0]
        if current_state == "needs-user-guidance" or current_state == "ready-for-development":
            return None
        if current_state == "needs-grooming":
            return _Task(self._agents[PROJECT_OWNER], current_state)
        if current_state == "needs-decision":
            return _Task(self._agents[PROJECT_OWNER], current_state)
        if current_state == "needs-documentation":
            return _Task(self._agents[DOCUMENTATION], current_state)
        return None

    def _run_task(
        self, target: GitHubTarget, work_item: dict[str, Any], task: _Task
    ) -> OrchestrationOutcome:
        comments = work_item["issue"].get("comments", [])
        recovered_result = self._find_result(comments, task)
        if recovered_result is not None:
            try:
                self._apply_transition(target, work_item, task, recovered_result["next"])
            except Exception as error:
                return OrchestrationOutcome(
                    "retrying-label-update", target.number, task.agent.name, str(error)
                )
            return OrchestrationOutcome("recovered", target.number, task.agent.name)

        invocation_id = self._id_factory()

        try:
            result = self._executor.execute(
                CodexExecutionRequest(
                    role_path=task.agent.role_path,
                    workspace=self._workspace,
                    work_item=work_item,
                    output_schema_path=self._output_schema_path,
                    reference_paths=role_reference_paths(task.agent.role_path),
                )
            )
            validate_orchestration_result(result, task.agent.name)
            next_state = (
                result["next_state"] if task.agent.name == PROJECT_OWNER else "needs-decision"
            )
            self._validate_transition(task.source_state, next_state, task.agent.name)
            comment = render_orchestration_result(
                result,
                role=task.agent.name,
                source_state=task.source_state,
                next_state=next_state,
                invocation_id=invocation_id,
            )
            task.agent.github.create_issue_comment(target, comment)
        except Exception as error:
            return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
        try:
            self._apply_transition(target, work_item, task, next_state)
        except Exception as error:
            # The result comment is durable, so the next pass can retry only this
            # label update instead of invoking Codex again.
            return OrchestrationOutcome(
                "retrying-label-update", target.number, task.agent.name, str(error)
            )
        return OrchestrationOutcome("processed", target.number, task.agent.name)

    def _apply_transition(
        self, target: GitHubTarget, work_item: dict[str, Any], task: _Task, next_state: str
    ) -> None:
        self._validate_transition(task.source_state, next_state, task.agent.name)
        labels = [str(label) for label in work_item["issue"].get("labels", [])]
        labels = [label for label in labels if label not in WORKFLOW_STATES]
        labels.append(next_state)
        # Only this Project Owner-owned client mutates workflow state.
        self._project_owner_github.update_issue_labels(target, labels)

    @staticmethod
    def _validate_transition(source_state: str, next_state: str, role: str) -> None:
        if next_state not in WORKFLOW_STATES:
            raise ValueError(f"Unsupported workflow transition target: {next_state}")
        if source_state != SOURCE_UNLABELED and source_state == next_state:
            raise ValueError(f"{role} must advance beyond {source_state}.")
        if role == DOCUMENTATION and next_state != "needs-decision":
            raise ValueError("Documentation can only hand control back as needs-decision.")

    @staticmethod
    def _has_completed_intake(comments: Iterable[dict[str, Any]]) -> bool:
        return any(
            attributes.get("kind") == "result"
            and attributes.get("role") == PROJECT_OWNER
            and attributes.get("from") == SOURCE_UNLABELED
            for attributes in _iter_markers(comments)
        )

    @staticmethod
    def _find_result(comments: Iterable[dict[str, Any]], task: _Task) -> dict[str, str] | None:
        markers = list(_iter_markers(comments))
        for attributes in reversed(markers):
            if (
                attributes.get("kind") == "result"
                and attributes.get("role") == task.agent.name
                and attributes.get("from") == task.source_state
                and attributes.get("next") in WORKFLOW_STATES
            ):
                return attributes
        return None


def _iter_markers(comments: Iterable[dict[str, Any]]) -> Iterable[dict[str, str]]:
    for comment in comments:
        body = str(comment.get("body") or "")
        for match in MARKER_PATTERN.finditer(body):
            attributes = {key: value for key, value in ATTRIBUTE_PATTERN.findall(match["attributes"])}
            attributes["kind"] = match["kind"]
            yield attributes


def _parse_repository(repository: str) -> tuple[str, str]:
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("repository must be in OWNER/REPOSITORY form.")
    return parts[0], parts[1]


def _format_outcome(outcome: OrchestrationOutcome) -> str:
    if outcome.status == "idle":
        return "Agent Army poll complete: no eligible issue."
    issue = f" issue #{outcome.issue_number}" if outcome.issue_number else ""
    role = f" ({outcome.role})" if outcome.role else ""
    detail = f": {outcome.detail}" if outcome.detail else ""
    return f"Agent Army poll complete: {outcome.status}{issue}{role}{detail}"
