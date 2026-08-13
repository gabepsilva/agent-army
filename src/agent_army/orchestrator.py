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
from agent_army.git_worktrees import (
    IsolatedGitWorktree,
    branch_name,
    commit_and_push,
    git_ref_exists,
    run_git,
)
from agent_army.publishers import (
    OPTIMIZATION_REVIEW_CHECK_NAME,
    REVIEW_OUTCOME_STATES,
    WORKFLOW_STATES,
    render_developer_blocked_result,
    render_developer_result,
    render_optimization_review_result,
    render_orchestration_result,
    render_requirements_challenge_result,
    validate_developer_result,
    validate_optimization_review_result,
    validate_orchestration_result,
    validate_requirements_challenge_result,
)
from agent_army.work_items import WorkItemReader


PROJECT_OWNER = "project-owner"
DOCUMENTATION = "documentation"
DEVELOPER = "developer"
OPTIMIZATION_REVIEWER = "optimization-reviewer"
SOURCE_UNLABELED = "unlabeled"
ORCHESTRATION_PAUSED_LABEL = "orchestration-paused"


@dataclass(frozen=True)
class OrchestratedAgent:
    """The orchestrator-owned runtime details for one role."""

    name: str
    mode: str
    role_path: Path
    github: GitHubAppClient
    output_schema_path: Path


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
    invocation_mode: str | None = None


@dataclass(frozen=True)
class DryRunIneligibleIssue:
    """One open issue that the next polling pass would skip, and why."""

    issue_number: int
    reason: str


@dataclass(frozen=True)
class DryRunEligibleIssue:
    """The single issue the next polling pass would act on."""

    issue_number: int
    role: str
    source_state: str
    action: str


@dataclass(frozen=True)
class DryRunReport:
    """A preview of what the next polling pass would do, with no side effects."""

    eligible_issue: DryRunEligibleIssue | None
    ineligible_issues: tuple[DryRunIneligibleIssue, ...] = ()
    has_open_issues: bool = True


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
        developer_github: GitHubAppClient | None = None,
        reviewer_github: GitHubAppClient | None = None,
        developer_role: Path | None = None,
        reviewer_role: Path | None = None,
        developer_output_schema_path: Path | None = None,
        reviewer_output_schema_path: Path | None = None,
        requirements_challenge_output_schema_path: Path | None = None,
        executor: CodexCliExecutor | None = None,
        work_item_reader: WorkItemReader | None = None,
        id_factory: Callable[[], str] | None = None,
        worktree_factory: Callable[..., Any] | None = None,
        git_command_runner: Callable[..., Any] | None = None,
    ) -> None:
        self.owner, self.repository = _parse_repository(repository)
        self._project_owner_github = project_owner_github
        self._workspace = workspace
        self._output_schema_path = output_schema_path
        self._executor = executor or CodexCliExecutor()
        self._reader = work_item_reader or WorkItemReader(project_owner_github)
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._worktree_factory = worktree_factory or IsolatedGitWorktree
        self._git_command_runner = git_command_runner
        self._agents = {
            PROJECT_OWNER: OrchestratedAgent(
                PROJECT_OWNER,
                "issue_intake",
                project_owner_role,
                project_owner_github,
                output_schema_path,
            ),
            DOCUMENTATION: OrchestratedAgent(
                DOCUMENTATION,
                "issue_grooming",
                documentation_role,
                documentation_github,
                output_schema_path,
            ),
        }
        if developer_github and developer_role and developer_output_schema_path:
            self._agents[DEVELOPER] = OrchestratedAgent(
                DEVELOPER,
                "issue_implementation",
                developer_role,
                developer_github,
                developer_output_schema_path,
            )
        if reviewer_github and reviewer_role and reviewer_output_schema_path:
            self._agents[OPTIMIZATION_REVIEWER] = OrchestratedAgent(
                OPTIMIZATION_REVIEWER,
                "pull_request_review",
                reviewer_role,
                reviewer_github,
                reviewer_output_schema_path,
            )
        self._reviewer_github = reviewer_github
        self._review_reader = WorkItemReader(reviewer_github) if reviewer_github else None
        self._requirements_challenge_schema_path = requirements_challenge_output_schema_path

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

    def run_dry_run(self) -> DryRunReport:
        """Preview the next polling pass without invoking an agent or writing to GitHub."""
        issues = self._project_owner_github.list_open_issues(self.owner, self.repository)
        sorted_issues = sorted(issues, key=lambda item: int(item["number"]))
        if not sorted_issues:
            return DryRunReport(eligible_issue=None, has_open_issues=False)
        ineligible: list[DryRunIneligibleIssue] = []
        for issue in sorted_issues:
            target = GitHubTarget(self.owner, self.repository, int(issue["number"]), "issue")
            work_item = self._reader.read(target)
            task, reason = self._select_task_with_reason(work_item)
            if task is None:
                ineligible.append(
                    DryRunIneligibleIssue(int(issue["number"]), reason or "unhandled-state")
                )
                continue
            action = self._dry_run_action(target, work_item, task)
            return DryRunReport(
                eligible_issue=DryRunEligibleIssue(
                    int(issue["number"]), task.agent.name, task.source_state, action
                ),
                ineligible_issues=tuple(ineligible),
            )
        return DryRunReport(eligible_issue=None, ineligible_issues=tuple(ineligible))

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
        task, _ = self._select_task_with_reason(work_item)
        return task

    def _select_task_with_reason(
        self, work_item: dict[str, Any]
    ) -> tuple[_Task | None, str | None]:
        """Return the eligible task, or None with a short ineligibility reason code."""
        issue = work_item["issue"]
        labels = [str(label) for label in issue.get("labels", [])]
        if ORCHESTRATION_PAUSED_LABEL in labels:
            # This human-controlled guard wins over intake and every workflow state.
            return None, "orchestration-paused"
        workflow_labels = [label for label in labels if label in WORKFLOW_STATES]
        comments = issue.get("comments", [])

        if len(workflow_labels) > 1:
            # Ambiguous state is left untouched until a human restores the invariant.
            return None, "ambiguous-labels"
        if not workflow_labels:
            if self._has_completed_intake(comments):
                return None, "intake-already-completed"
            return _Task(self._agents[PROJECT_OWNER], SOURCE_UNLABELED), None

        current_state = workflow_labels[0]
        if current_state == "needs-user-guidance":
            return None, "needs-user-guidance"
        if current_state == "ready-for-merge":
            if OPTIMIZATION_REVIEWER not in self._agents:
                return None, "no-agent-configured"
            if self._needs_fresh_review(work_item):
                return _Task(self._agents[OPTIMIZATION_REVIEWER], "needs-optimization-review"), None
            return None, "no-fresh-review-needed"
        if current_state == "needs-grooming":
            return _Task(self._agents[PROJECT_OWNER], current_state), None
        if current_state == "needs-decision":
            return _Task(self._agents[PROJECT_OWNER], current_state), None
        if current_state == "needs-documentation":
            return _Task(self._agents[DOCUMENTATION], current_state), None
        if current_state == "needs-requirements-challenge":
            if OPTIMIZATION_REVIEWER not in self._agents:
                return None, "no-agent-configured"
            pending_round = self._latest_project_owner_challenge_round(comments)
            challenge_task = _Task(
                self._agents[OPTIMIZATION_REVIEWER],
                current_state,
                "requirements_challenge",
            )
            if self._find_requirements_challenge_result(comments, pending_round) is not None:
                return challenge_task, None
            if self._latest_requirements_challenge_round(comments) >= 2:
                # A second challenge may only be recovered, never started again.
                return None, "challenge-round-exhausted"
            return challenge_task, None
        if current_state == "ready-for-development":
            if DEVELOPER not in self._agents:
                return None, "no-agent-configured"
            return _Task(self._agents[DEVELOPER], current_state), None
        if current_state == "needs-optimization-review":
            if OPTIMIZATION_REVIEWER not in self._agents:
                return None, "no-agent-configured"
            return _Task(self._agents[OPTIMIZATION_REVIEWER], current_state), None
        return None, "unhandled-state"

    def _dry_run_action(
        self, target: GitHubTarget, work_item: dict[str, Any], task: _Task
    ) -> str:
        """Determine whether the task would invoke an agent or recover a durable result."""
        comments = work_item["issue"].get("comments", [])
        if task.agent.name == DEVELOPER:
            if self._find_blocked_developer_result(comments, task.source_state) is not None:
                return "recover result"
            title = str(work_item["issue"].get("title") or f"Issue #{target.number}")
            branch = branch_name(target.number, title)
            existing = task.agent.github.list_open_pull_requests(
                self.owner, self.repository, head=branch
            )
            if existing:
                if self._latest_review_outcome(comments, existing[0]) == "changes-requested":
                    return "invoke agent"
                return "recover result"
            return "invoke agent"
        if task.agent.name == OPTIMIZATION_REVIEWER:
            if task.invocation_mode == "requirements_challenge":
                pending_round = self._latest_project_owner_challenge_round(comments)
                recovered = self._find_requirements_challenge_result(comments, pending_round)
                return "recover result" if recovered is not None else "invoke agent"
            developer_marker = self._find_developer_marker(comments)
            if developer_marker is None:
                return "invoke agent"
            pull_target = GitHubTarget(
                self.owner, self.repository, int(developer_marker["pr"]), "pull_request"
            )
            pull_request = self._reviewer_github.get_pull_request(pull_target)
            head_sha = str(pull_request["head"]["sha"])
            if self._find_review_result(comments, task, head_sha) is not None:
                return "recover result"
            if self._find_review_check(head_sha) is not None:
                return "recover result"
            return "invoke agent"
        return "recover result" if self._find_result(comments, task) is not None else "invoke agent"

    def _run_task(
        self, target: GitHubTarget, work_item: dict[str, Any], task: _Task
    ) -> OrchestrationOutcome:
        if task.agent.name == DEVELOPER:
            return self._run_developer_task(target, work_item, task)
        if task.agent.name == OPTIMIZATION_REVIEWER:
            if task.invocation_mode == "requirements_challenge":
                return self._run_requirements_challenge_task(target, work_item, task)
            return self._run_reviewer_task(target, work_item, task)
        return self._run_issue_task(target, work_item, task)

    def _run_issue_task(
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
                    output_schema_path=task.agent.output_schema_path,
                    reference_paths=role_reference_paths(
                        task.agent.role_path, invocation_mode=task.invocation_mode
                    ),
                )
            )
            validate_orchestration_result(
                result,
                task.agent.name,
                prior_challenge_round=self._latest_requirements_challenge_round(comments)
                if task.agent.name == PROJECT_OWNER
                else None,
            )
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
                prior_challenge_round=self._latest_requirements_challenge_round(comments)
                if task.agent.name == PROJECT_OWNER
                else None,
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

    def _run_requirements_challenge_task(
        self, target: GitHubTarget, work_item: dict[str, Any], task: _Task
    ) -> OrchestrationOutcome:
        if self._reviewer_github is None or self._requirements_challenge_schema_path is None:
            return OrchestrationOutcome(
                "failed", target.number, task.agent.name, "Requirements challenge is not configured."
            )
        comments = work_item["issue"].get("comments", [])
        pending_round = self._latest_project_owner_challenge_round(comments)
        recovered = self._find_requirements_challenge_result(comments, pending_round)
        if recovered is not None:
            try:
                self._apply_transition(target, work_item, task, recovered["next"])
            except Exception as error:
                return OrchestrationOutcome(
                    "retrying-label-update", target.number, task.agent.name, str(error)
                )
            return OrchestrationOutcome("recovered", target.number, task.agent.name)

        challenge_round = pending_round or self._latest_requirements_challenge_round(comments) + 1
        if challenge_round not in {1, 2}:
            return OrchestrationOutcome(
                "blocked",
                target.number,
                task.agent.name,
                "Requirements challenge round limit reached; Project Owner must resolve or ask for user guidance.",
            )
        challenge_work_item = dict(work_item)
        challenge_work_item["requirements_challenge"] = {
            "round": challenge_round,
            "prior_round": challenge_round - 1 or None,
        }
        try:
            result = self._executor.execute(
                CodexExecutionRequest(
                    role_path=task.agent.role_path,
                    workspace=self._workspace,
                    work_item=challenge_work_item,
                    output_schema_path=self._requirements_challenge_schema_path,
                    reference_paths=role_reference_paths(
                        task.agent.role_path, invocation_mode=task.invocation_mode
                    ),
                )
            )
            validate_requirements_challenge_result(result, challenge_round)
            comment = render_requirements_challenge_result(
                result,
                source_state=task.source_state,
                next_state="needs-decision",
                invocation_id=self._id_factory(),
                challenge_round=challenge_round,
            )
            self._reviewer_github.create_issue_comment(target, comment)
            self._apply_transition(target, work_item, task, "needs-decision")
        except Exception as error:
            return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
        return OrchestrationOutcome("processed", target.number, task.agent.name)

    def _run_developer_task(
        self, target: GitHubTarget, work_item: dict[str, Any], task: _Task
    ) -> OrchestrationOutcome:
        title = str(work_item["issue"].get("title") or f"Issue #{target.number}")
        branch = branch_name(target.number, title)
        blocked_result = self._find_blocked_developer_result(
            work_item["issue"].get("comments", []), task.source_state
        )
        if blocked_result is not None:
            try:
                self._apply_transition(target, work_item, task, blocked_result["next"])
            except Exception as error:
                return OrchestrationOutcome(
                    "retrying-label-update", target.number, task.agent.name, str(error)
                )
            return OrchestrationOutcome("recovered", target.number, task.agent.name)
        existing = task.agent.github.list_open_pull_requests(
            self.owner, self.repository, head=branch
        )
        if existing:
            pull_request = existing[0]
            if self._latest_review_outcome(work_item["issue"].get("comments", []), pull_request) == "changes-requested":
                return self._run_developer_on_existing_pr(
                    target, work_item, task, pull_request, branch
                )
            return self._recover_existing_developer_pr(target, work_item, task, pull_request, branch)

        repository = task.agent.github.get_repository(self.owner, self.repository)
        base_branch = str(repository["default_branch"])
        branch_exists = git_ref_exists(
            self._workspace,
            branch,
            **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
        )
        base_ref = branch if branch_exists else base_branch
        try:
            base_sha = run_git(
                ["git", "rev-parse", base_ref],
                self._workspace,
                **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
            ).stdout.strip()
        except RuntimeError:
            # A checkout may not have a local copy of the configured default
            # branch. Keep the task isolated and fall back to its current HEAD.
            base_sha = run_git(
                ["git", "rev-parse", "HEAD"],
                self._workspace,
                **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
            ).stdout.strip()
        try:
            with self._worktree_factory(
                self._workspace,
                base_ref=base_sha,
                branch=branch,
                create_branch=not branch_exists,
                **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
            ) as worktree:
                result = self._executor.execute(
                    CodexExecutionRequest(
                        role_path=task.agent.role_path,
                        workspace=worktree.path,
                        work_item=work_item,
                        output_schema_path=task.agent.output_schema_path,
                        reference_paths=role_reference_paths(task.agent.role_path),
                    )
                )
                validate_developer_result(result)
                if result["status"] == "blocked":
                    comment = render_developer_blocked_result(
                        result,
                        source_state=task.source_state,
                        next_state="needs-decision",
                        invocation_id=self._id_factory(),
                    )
                    task.agent.github.create_issue_comment(target, comment)
                    self._apply_transition(target, work_item, task, "needs-decision")
                    return OrchestrationOutcome("processed", target.number, task.agent.name)
                head_sha = commit_and_push(
                    worktree,
                    title=f"Implement issue #{target.number}: {title[:60]}",
                    **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
                )
        except Exception as error:
            return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))

        body = (
            f"Implements #{target.number}.\n\n"
            "Agent Army Developer result:\n\n"
            f"{result['summary'].strip()}\n\n"
            "This pull request was created by the orchestrator after validation."
        )
        try:
            pull_request = task.agent.github.create_pull_request(
                self.owner,
                self.repository,
                title=f"Implement #{target.number}: {title}",
                head=branch,
                base=base_branch,
                body=body,
            )
            next_state = "needs-optimization-review"
            comment = render_developer_result(
                result,
                source_state=task.source_state,
                next_state=next_state,
                invocation_id=self._id_factory(),
                pull_request_number=int(pull_request["number"]),
                pull_request_url=pull_request["html_url"],
                branch=branch,
                head_sha=head_sha,
            )
            task.agent.github.create_issue_comment(target, comment)
            self._apply_transition(target, work_item, task, next_state)
        except Exception as error:
            return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
        return OrchestrationOutcome("processed", target.number, task.agent.name)

    def _run_developer_on_existing_pr(
        self,
        target: GitHubTarget,
        work_item: dict[str, Any],
        task: _Task,
        pull_request: dict[str, Any],
        branch: str,
    ) -> OrchestrationOutcome:
        pull_number = int(pull_request["number"])
        pull_target = GitHubTarget(self.owner, self.repository, pull_number, "pull_request")
        implementation_work_item = WorkItemReader(task.agent.github).read(pull_target)
        implementation_work_item["source_issue"] = work_item
        head_sha = str(pull_request["head"]["sha"])
        try:
            run_git(
                ["git", "fetch", "origin", head_sha],
                self._workspace,
                **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
            )
            with self._worktree_factory(
                self._workspace,
                base_ref=head_sha,
                branch=branch,
                create_branch=False,
                **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
            ) as worktree:
                result = self._executor.execute(
                    CodexExecutionRequest(
                        role_path=task.agent.role_path,
                        workspace=worktree.path,
                        work_item=implementation_work_item,
                        output_schema_path=task.agent.output_schema_path,
                        reference_paths=role_reference_paths(task.agent.role_path),
                    )
                )
                validate_developer_result(result)
                if result["status"] != "completed":
                    return OrchestrationOutcome(
                        "blocked", target.number, task.agent.name, result["summary"]
                    )
                new_head_sha = commit_and_push(
                    worktree,
                    title=f"Address review feedback for issue #{target.number}",
                    **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
                )
            comment = render_developer_result(
                result,
                source_state=task.source_state,
                next_state="needs-optimization-review",
                invocation_id=self._id_factory(),
                pull_request_number=pull_number,
                pull_request_url=pull_request["html_url"],
                branch=branch,
                head_sha=new_head_sha,
            )
            task.agent.github.create_issue_comment(target, comment)
            self._apply_transition(target, work_item, task, "needs-optimization-review")
        except Exception as error:
            return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
        return OrchestrationOutcome("processed", target.number, task.agent.name)

    def _recover_existing_developer_pr(
        self,
        target: GitHubTarget,
        work_item: dict[str, Any],
        task: _Task,
        pull_request: dict[str, Any],
        branch: str,
    ) -> OrchestrationOutcome:
        comments = work_item["issue"].get("comments", [])
        if not any(
            attributes.get("kind") == "result"
            and attributes.get("role") == DEVELOPER
            and attributes.get("pr") == str(pull_request["number"])
            for attributes in _iter_markers(comments)
        ):
            body = (
                f"<!-- agent-army:result role=developer from=ready-for-development "
                f"next=needs-optimization-review pr={pull_request['number']} "
                f"branch={branch} head={pull_request['head']['sha']} -->\n"
                "## Agent Army: Developer PR recovered\n\n"
                f"An existing pull request was found for this issue: [{pull_request['html_url']}]"
                f"({pull_request['html_url']}). It is ready for Optimization Reviewer."
            )
            try:
                task.agent.github.create_issue_comment(target, body)
            except Exception as error:
                return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
        try:
            self._apply_transition(target, work_item, task, "needs-optimization-review")
        except Exception as error:
            return OrchestrationOutcome(
                "retrying-label-update", target.number, task.agent.name, str(error)
            )
        return OrchestrationOutcome("recovered", target.number, task.agent.name)

    def _run_reviewer_task(
        self, target: GitHubTarget, work_item: dict[str, Any], task: _Task
    ) -> OrchestrationOutcome:
        if self._reviewer_github is None or self._review_reader is None:
            return OrchestrationOutcome("failed", target.number, task.agent.name, "Reviewer is not configured.")
        developer_marker = self._find_developer_marker(work_item["issue"].get("comments", []))
        if developer_marker is None:
            return OrchestrationOutcome(
                "failed", target.number, task.agent.name, "No Developer pull request is linked to the issue."
            )
        pull_number = int(developer_marker["pr"])
        pull_target = GitHubTarget(self.owner, self.repository, pull_number, "pull_request")
        pull_request = self._reviewer_github.get_pull_request(pull_target)
        head_sha = str(pull_request["head"]["sha"])
        comments = work_item["issue"].get("comments", [])
        recovered = self._find_review_result(comments, task, head_sha)
        if recovered is not None:
            try:
                self._apply_transition(target, work_item, task, recovered["next"])
            except Exception as error:
                return OrchestrationOutcome(
                    "retrying-label-update", target.number, task.agent.name, str(error)
                )
            return OrchestrationOutcome("recovered", target.number, task.agent.name)

        existing_check = self._find_review_check(head_sha)
        if existing_check is not None:
            summary = str(existing_check.get("output", {}).get("summary") or "")
            try:
                self._reviewer_github.create_issue_comment(target, summary)
                next_state = self._marker_next_state(summary)
                self._apply_transition(target, work_item, task, next_state)
            except Exception as error:
                return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
            return OrchestrationOutcome("recovered", target.number, task.agent.name)

        review_work_item = self._review_reader.read(pull_target)
        review_work_item["source_issue"] = work_item
        try:
            with self._worktree_factory(
                self._workspace,
                base_ref=head_sha,
                branch=f"review-{pull_number}",
                create_branch=False,
                **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
            ) as worktree:
                result = self._executor.execute(
                    CodexExecutionRequest(
                        role_path=task.agent.role_path,
                        workspace=worktree.path,
                        work_item=review_work_item,
                        output_schema_path=task.agent.output_schema_path,
                        reference_paths=role_reference_paths(task.agent.role_path),
                    )
                )
                validate_optimization_review_result(result, head_sha)
                status = run_git(
                    ["git", "status", "--porcelain"],
                    worktree.path,
                    **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
                )
                if status.stdout.strip():
                    raise ValueError("Optimization Reviewer modified the review workspace.")
            next_state = REVIEW_OUTCOME_STATES[result["outcome"]]
            comment = render_optimization_review_result(
                result,
                source_state=task.source_state,
                next_state=next_state,
                invocation_id=self._id_factory(),
                pull_request_number=pull_number,
                pull_request_url=pull_request["html_url"],
                branch=pull_request["head"]["ref"],
                head_sha=head_sha,
            )
            conclusion = {
                "approved": "success",
                "changes-requested": "failure",
                "unable-to-assess": "neutral",
            }[result["outcome"]]
            self._reviewer_github.create_check_run(
                self.owner,
                self.repository,
                name=OPTIMIZATION_REVIEW_CHECK_NAME,
                head_sha=head_sha,
                conclusion=conclusion,
                summary=comment,
                details_url=pull_request["html_url"],
            )
            self._reviewer_github.create_issue_comment(target, comment)
            self._apply_transition(target, work_item, task, next_state)
        except Exception as error:
            return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
        return OrchestrationOutcome("processed", target.number, task.agent.name)

    def _find_developer_marker(self, comments: Iterable[dict[str, Any]]) -> dict[str, str] | None:
        for attributes in reversed(list(_iter_markers(comments))):
            if attributes.get("kind") == "result" and attributes.get("role") == DEVELOPER:
                return attributes
        return None

    @staticmethod
    def _latest_requirements_challenge_round(
        comments: Iterable[dict[str, Any]],
    ) -> int:
        rounds = [
            int(attributes["round"])
            for attributes in _iter_markers(comments)
            if attributes.get("role") == OPTIMIZATION_REVIEWER
            and attributes.get("mode") == "requirements_challenge"
            and attributes.get("round", "").isdigit()
        ]
        return max(rounds, default=0)

    @staticmethod
    def _latest_project_owner_challenge_round(
        comments: Iterable[dict[str, Any]],
    ) -> int | None:
        rounds = [
            int(attributes["challenge_round"])
            for attributes in _iter_markers(comments)
            if attributes.get("role") == PROJECT_OWNER
            and attributes.get("next") == "needs-requirements-challenge"
            and attributes.get("challenge_round", "").isdigit()
        ]
        return max(rounds, default=0) or None

    @staticmethod
    def _find_requirements_challenge_result(
        comments: Iterable[dict[str, Any]], challenge_round: int | None = None
    ) -> dict[str, str] | None:
        for attributes in reversed(list(_iter_markers(comments))):
            if (
                attributes.get("kind") == "result"
                and attributes.get("role") == OPTIMIZATION_REVIEWER
                and attributes.get("mode") == "requirements_challenge"
                and attributes.get("next") == "needs-decision"
                and (
                    challenge_round is None
                    or attributes.get("round") == str(challenge_round)
                )
            ):
                return attributes
        return None

    @staticmethod
    def _find_blocked_developer_result(
        comments: Iterable[dict[str, Any]], source_state: str
    ) -> dict[str, str] | None:
        for attributes in reversed(list(_iter_markers(comments))):
            if (
                attributes.get("kind") == "result"
                and attributes.get("role") == DEVELOPER
                and attributes.get("from") == source_state
                and attributes.get("status") == "blocked"
                and attributes.get("next") == "needs-decision"
            ):
                return attributes
        return None

    @staticmethod
    def _latest_review_outcome(
        comments: Iterable[dict[str, Any]], pull_request: dict[str, Any]
    ) -> str | None:
        pull_number = str(pull_request["number"])
        for attributes in reversed(list(_iter_markers(comments))):
            if (
                attributes.get("kind") == "result"
                and attributes.get("role") == OPTIMIZATION_REVIEWER
                and attributes.get("pr") == pull_number
            ):
                return attributes.get("outcome")
        return None

    def _needs_fresh_review(self, work_item: dict[str, Any]) -> bool:
        if self._reviewer_github is None:
            return False
        marker = self._find_developer_marker(work_item["issue"].get("comments", []))
        if marker is None:
            return False
        pull_target = GitHubTarget(
            self.owner, self.repository, int(marker["pr"]), "pull_request"
        )
        pull_request = self._reviewer_github.get_pull_request(pull_target)
        head_sha = str(pull_request["head"]["sha"])
        task = _Task(self._agents[OPTIMIZATION_REVIEWER], "needs-optimization-review")
        if self._find_review_result(work_item["issue"].get("comments", []), task, head_sha):
            return False
        return self._find_review_check(head_sha) is None

    def _find_review_result(
        self, comments: Iterable[dict[str, Any]], task: _Task, head_sha: str
    ) -> dict[str, str] | None:
        for attributes in reversed(list(_iter_markers(comments))):
            if (
                attributes.get("kind") == "result"
                and attributes.get("role") == OPTIMIZATION_REVIEWER
                and attributes.get("from") == task.source_state
                and attributes.get("head") == head_sha
                and attributes.get("next") in REVIEW_OUTCOME_STATES.values()
            ):
                return attributes
        return None

    def _find_review_check(self, head_sha: str) -> dict[str, Any] | None:
        if self._reviewer_github is None:
            return None
        for check in self._reviewer_github.get_check_runs(self.owner, self.repository, head_sha):
            if check.get("name") != OPTIMIZATION_REVIEW_CHECK_NAME:
                continue
            summary = str(check.get("output", {}).get("summary") or "")
            if f"head={head_sha}" in summary:
                return check
        return None

    @staticmethod
    def _marker_next_state(summary: str) -> str:
        markers = list(_iter_markers([{"body": summary}]))
        for attributes in reversed(markers):
            if attributes.get("role") == OPTIMIZATION_REVIEWER:
                next_state = attributes.get("next")
                if next_state in REVIEW_OUTCOME_STATES.values():
                    return next_state
        raise ValueError("Optimization Review check has no recoverable transition.")

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
        if not markers:
            return None
        attributes = markers[-1]
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


def format_dry_run_report(report: DryRunReport) -> str:
    """Render a DryRunReport as the operator-facing dry-run summary."""
    if report.eligible_issue is not None:
        issue = report.eligible_issue
        return (
            f"Agent Army dry run: issue #{issue.issue_number} is eligible "
            f"for {issue.role} (from {issue.source_state}); action: {issue.action}."
        )
    if not report.has_open_issues:
        return "Agent Army dry run: no open issues."
    if not report.ineligible_issues:
        return "Agent Army dry run: no eligible issue."
    lines = ["Agent Army dry run: no eligible issue."]
    for status in report.ineligible_issues:
        lines.append(f"  issue #{status.issue_number}: {status.reason}")
    return "\n".join(lines)
