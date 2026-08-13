"""Polling orchestration for the issue-driven Agent Army workflow."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_army.agent_executor import (
    AgentExecutionRequest,
    AgentExecutor,
    CodexCliExecutor,
    role_reference_paths,
)
from agent_army.github_app import DESIGN_SIGNOFF_REACTION, GitHubAppClient, GitHubTarget
from agent_army.git_worktrees import (
    IsolatedGitWorktree,
    branch_name,
    commit_and_push,
    git_ref_exists,
    run_git,
)
from agent_army.publishers import (
    BLOCKING,
    FINAL_DESIGN_HEADING,
    MAX_CONVERGENCE_ROUNDS,
    MAX_SIGNOFF_ROUNDS,
    OPTIMIZATION_REVIEW_CHECK_NAME,
    REVIEW_OUTCOME_STATES,
    WORKFLOW_STATES,
    decode_payload,
    render_convergence_escalation,
    render_developer_blocked_result,
    render_developer_result,
    render_optimization_review_marker_comment,
    render_optimization_review_result,
    render_final_design,
    render_orchestration_result,
    render_requirements_challenge_result,
    render_signoff_correction,
    validate_developer_result,
    validate_optimization_review_result,
    validate_orchestration_result,
    validate_requirements_challenge_result,
    validate_signoff_result,
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
    executor: AgentExecutor


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
class DryRunCandidate:
    """One issue considered and skipped during a dry-run scan."""

    issue_number: int
    reason: str


@dataclass(frozen=True)
class DryRunOutcome:
    """A preview of what run_once() would do next, without any side effects.

    status is one of "no-open-issues", "no-eligible-issue", "would-invoke", or
    "would-recover". The latter two describe the single selected issue; every
    issue considered and passed over along the way is recorded in `skipped`.
    """

    status: str
    issue_number: int | None = None
    current_label: str | None = None
    role: str | None = None
    mode: str | None = None
    next_state: str | None = None
    skipped: tuple[DryRunCandidate, ...] = ()


MARKER_PATTERN = re.compile(r"<!-- agent-army:(?P<kind>\w+) (?P<attributes>[^>]+?) -->")
ATTRIBUTE_PATTERN = re.compile(r"(?P<key>[a-z_]+)=(?P<value>[^\s]+)")


class IssueOrchestrator:
    """Poll one repository and run at most one state-changing task per pass.

    GitHub clients are intentionally injected separately from the agent executor.
    The clients retain credentials inside this Python process; only normalized
    work-item data is sent to the agent CLI.
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
        design_signoff_output_schema_path: Path | None = None,
        executor: AgentExecutor | None = None,
        executors: Mapping[str, AgentExecutor] | None = None,
        work_item_reader: WorkItemReader | None = None,
        id_factory: Callable[[], str] | None = None,
        worktree_factory: Callable[..., Any] | None = None,
        git_command_runner: Callable[..., Any] | None = None,
    ) -> None:
        self.owner, self.repository = _parse_repository(repository)
        self._project_owner_github = project_owner_github
        self._workspace = workspace
        self._output_schema_path = output_schema_path
        default_executor = executor or CodexCliExecutor()
        # Each role may run on its own backend; anything unlisted inherits the
        # repository-wide default.
        per_agent = dict(executors or {})
        self._executor_for = lambda name: per_agent.get(name, default_executor)
        self._total_cost_usd = 0.0
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
                self._executor_for(PROJECT_OWNER),
            ),
            DOCUMENTATION: OrchestratedAgent(
                DOCUMENTATION,
                "issue_grooming",
                documentation_role,
                documentation_github,
                output_schema_path,
                self._executor_for(DOCUMENTATION),
            ),
        }
        if developer_github and developer_role and developer_output_schema_path:
            self._agents[DEVELOPER] = OrchestratedAgent(
                DEVELOPER,
                "issue_implementation",
                developer_role,
                developer_github,
                developer_output_schema_path,
                self._executor_for(DEVELOPER),
            )
        if reviewer_github and reviewer_role and reviewer_output_schema_path:
            self._agents[OPTIMIZATION_REVIEWER] = OrchestratedAgent(
                OPTIMIZATION_REVIEWER,
                "pull_request_review",
                reviewer_role,
                reviewer_github,
                reviewer_output_schema_path,
                self._executor_for(OPTIMIZATION_REVIEWER),
            )
        self._reviewer_github = reviewer_github
        self._review_reader = WorkItemReader(reviewer_github) if reviewer_github else None
        self._requirements_challenge_schema_path = requirements_challenge_output_schema_path
        self._signoff_schema_path = design_signoff_output_schema_path

    @property
    def total_cost_usd(self) -> float:
        """Reported spend so far. Codex runs report 0.0; see AgentExecutionResult."""
        return self._total_cost_usd

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

    def dry_run(self) -> DryRunOutcome:
        """Preview run_once()'s selection for the next pass, read-only.

        Walks the same eligibility scan as run_once()/_select_task() over every
        open issue in ascending order, but never reaches a write call: no
        comment, label update, executor/Codex invocation, or git/worktree
        operation happens. For the selected issue (if any), also distinguishes
        whether the real pass would invoke the agent or only recover a label
        (see _describe_selection) -- the two are easy to conflate from the
        selected task alone, since several _run_* methods short-circuit to a
        label-only transition when a matching result is already posted.
        """
        issues = self._project_owner_github.list_open_issues(self.owner, self.repository)
        sorted_issues = sorted(issues, key=lambda item: int(item["number"]))
        if not sorted_issues:
            return DryRunOutcome("no-open-issues")
        skipped: list[DryRunCandidate] = []
        for issue in sorted_issues:
            target = GitHubTarget(self.owner, self.repository, int(issue["number"]), "issue")
            work_item = self._reader.read(target)
            task, reason = self._evaluate_task(work_item)
            if task is None:
                skipped.append(DryRunCandidate(target.number, reason))
                continue
            labels = [str(label) for label in work_item["issue"].get("labels", [])]
            current_label = next(
                (label for label in labels if label in WORKFLOW_STATES), SOURCE_UNLABELED
            )
            status, next_state = self._describe_selection(work_item, task)
            return DryRunOutcome(
                status,
                issue_number=target.number,
                current_label=current_label,
                role=task.agent.name,
                mode=task.invocation_mode,
                next_state=next_state,
                skipped=tuple(skipped),
            )
        return DryRunOutcome("no-eligible-issue", skipped=tuple(skipped))

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
        task, _reason = self._evaluate_task(work_item)
        return task

    def _evaluate_task(self, work_item: dict[str, Any]) -> tuple[_Task | None, str]:
        """Select a task, or explain why none is eligible right now.

        The reason string is only for reporting (dry-run and diagnostics); it
        has no effect on selection and is empty whenever a task is returned.
        """
        issue = work_item["issue"]
        labels = [str(label) for label in issue.get("labels", [])]
        if ORCHESTRATION_PAUSED_LABEL in labels:
            # This human-controlled guard wins over intake and every workflow state.
            return None, "orchestration-paused"
        workflow_labels = [label for label in labels if label in WORKFLOW_STATES]
        comments = issue.get("comments", [])

        if len(workflow_labels) > 1:
            # Ambiguous state is left untouched until a human restores the invariant.
            return None, "ambiguous workflow labels: " + ", ".join(sorted(workflow_labels))
        if not workflow_labels:
            if self._has_completed_intake(comments):
                return None, "unlabeled intake already completed"
            return _Task(self._agents[PROJECT_OWNER], SOURCE_UNLABELED), ""

        current_state = workflow_labels[0]
        if current_state == "needs-user-guidance":
            return None, "needs-user-guidance (blocked on human)"
        if current_state == "ready-for-merge":
            if OPTIMIZATION_REVIEWER not in self._agents:
                return None, "ready-for-merge (optimization reviewer not configured)"
            if self._needs_fresh_review(work_item):
                return _Task(self._agents[OPTIMIZATION_REVIEWER], "needs-optimization-review"), ""
            return None, "ready-for-merge (no fresh review needed)"
        if current_state == "needs-grooming":
            return _Task(self._agents[PROJECT_OWNER], current_state), ""
        if current_state == "needs-decision":
            return _Task(self._agents[PROJECT_OWNER], current_state), ""
        if current_state == "needs-documentation":
            return _Task(self._agents[DOCUMENTATION], current_state), ""
        if current_state == "needs-requirements-challenge":
            if OPTIMIZATION_REVIEWER not in self._agents:
                return None, "needs-requirements-challenge (optimization reviewer not configured)"
            pending_round = self._latest_project_owner_challenge_round(comments)
            challenge_task = _Task(
                self._agents[OPTIMIZATION_REVIEWER],
                current_state,
                "requirements_challenge",
            )
            if self._find_requirements_challenge_result(comments, pending_round) is not None:
                return challenge_task, ""
            if self._latest_requirements_challenge_round(comments) >= 2:
                # A second challenge may only be recovered, never started again.
                return None, "needs-requirements-challenge round >= 2 (recovery only)"
            return challenge_task, ""
        if current_state == "needs-design-signoff":
            if OPTIMIZATION_REVIEWER not in self._agents:
                return None, "needs-design-signoff (optimization reviewer not configured)"
            return (
                _Task(self._agents[OPTIMIZATION_REVIEWER], current_state, "design_signoff"),
                "",
            )
        if current_state == "ready-for-development":
            if DEVELOPER not in self._agents:
                return None, "ready-for-development (developer not configured)"
            return _Task(self._agents[DEVELOPER], current_state), ""
        if current_state == "needs-optimization-review":
            if OPTIMIZATION_REVIEWER not in self._agents:
                return None, "needs-optimization-review (optimization reviewer not configured)"
            return _Task(self._agents[OPTIMIZATION_REVIEWER], current_state), ""
        return None, f"unsupported workflow state: {current_state}"

    def _describe_selection(
        self, work_item: dict[str, Any], task: _Task
    ) -> tuple[str, str | None]:
        """Would the real pass invoke the agent, or only recover a label?

        Mirrors the same recovered-result check each _run_* method makes
        before doing any work, without performing the write that follows it.
        Developer (ready-for-development) has no such check here: unlike the
        other roles, none of its recovery paths (a blocked result, an existing
        pull request) are gated on "does the next pass invoke Codex or not" in
        a way distinguishable up front without duplicating _run_developer_task's
        branching -- it is always reported as "would invoke".
        """
        comments = work_item["issue"].get("comments", [])
        if task.agent.name == DEVELOPER:
            return "would-invoke", None
        if task.agent.name == OPTIMIZATION_REVIEWER:
            if task.invocation_mode == "requirements_challenge":
                pending_round = self._latest_project_owner_challenge_round(comments)
                recovered = self._find_requirements_challenge_result(comments, pending_round)
                if recovered is not None:
                    return "would-recover", recovered.get("next")
                return "would-invoke", None
            if task.invocation_mode == "design_signoff":
                design = self._find_final_design_comment(comments)
                if design is not None and self._is_stamped(design):
                    return "would-recover", "ready-for-development"
                return "would-invoke", None
            # ready-for-merge (already screened by _needs_fresh_review) and
            # needs-optimization-review both run through _run_reviewer_task,
            # which re-checks for a recovered review result or check run.
            developer_marker = self._find_developer_marker(comments)
            if developer_marker is None:
                return "would-invoke", None
            pull_target = GitHubTarget(
                self.owner, self.repository, int(developer_marker["pr"]), "pull_request"
            )
            pull_conversation_target = GitHubTarget(
                self.owner, self.repository, int(developer_marker["pr"]), "issue"
            )
            pull_request = self._reviewer_github.get_pull_request(pull_target)
            head_sha = str(pull_request["head"]["sha"])
            pull_comments = self._reviewer_github.get_issue_comments(pull_conversation_target)
            recovered = self._find_review_result(pull_comments, task, head_sha)
            if recovered is not None:
                return "would-recover", recovered.get("next")
            if self._find_review_check(head_sha) is not None:
                return "would-recover", None
            return "would-invoke", None
        # Project Owner and Documentation both run through _run_issue_task.
        recovered = self._find_result(comments, task)
        if recovered is not None:
            return "would-recover", recovered.get("next")
        return "would-invoke", None

    def _run_task(
        self, target: GitHubTarget, work_item: dict[str, Any], task: _Task
    ) -> OrchestrationOutcome:
        if task.agent.name == DEVELOPER:
            return self._run_developer_task(target, work_item, task)
        if task.agent.name == OPTIMIZATION_REVIEWER:
            if task.invocation_mode == "requirements_challenge":
                return self._run_requirements_challenge_task(target, work_item, task)
            if task.invocation_mode == "design_signoff":
                return self._run_design_signoff_task(target, work_item, task)
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
        self._attach_linked_pull_request(work_item)

        try:
            execution = task.agent.executor.execute(
                AgentExecutionRequest(
                    role_path=task.agent.role_path,
                    workspace=self._workspace,
                    work_item=work_item,
                    output_schema_path=task.agent.output_schema_path,
                    reference_paths=role_reference_paths(
                        task.agent.role_path, invocation_mode=task.invocation_mode
                    ),
                )
            )
            self._total_cost_usd += execution.cost_usd
            result = execution.output
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
            if next_state == "needs-design-signoff":
                # The canonical converged spec, posted as its own artifact so
                # the Reviewer has one unambiguous thing to stamp. A later
                # correction revises this comment in place rather than adding
                # a competing version.
                existing = self._find_final_design_comment(
                    work_item["issue"].get("comments", [])
                )
                design_body = render_final_design(
                    str(result.get("final_design") or result["summary"]),
                    source_state=task.source_state,
                    next_state=next_state,
                    invocation_id=invocation_id,
                    revision=self._signoff_revision(work_item["issue"].get("comments", [])) + 1,
                )
                if existing is not None and existing.get("id") is not None:
                    task.agent.github.update_issue_comment(
                        self.owner, self.repository, int(existing["id"]), design_body
                    )
                else:
                    task.agent.github.create_issue_comment(target, design_body)
        except Exception as error:
            return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
        try:
            self._apply_transition(target, work_item, task, next_state)
        except Exception as error:
            # The result comment is durable, so the next pass can retry only this
            # label update instead of invoking the agent again.
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
        if challenge_round > MAX_CONVERGENCE_ROUNDS:
            # The issue-level argument is not converging on its own. Same bound
            # and same handover as the PR-review loop.
            try:
                escalation = render_convergence_escalation(
                    source_state=task.source_state,
                    next_state="needs-user-guidance",
                    invocation_id=self._id_factory(),
                    pull_request_number=0,
                    pull_request_url="",
                    round_number=challenge_round - 1,
                    open_findings=[
                        finding
                        for finding in self._open_challenge_findings(comments)
                        if finding.get("severity") == BLOCKING
                    ],
                )
                self._reviewer_github.create_issue_comment(target, escalation)
                self._apply_transition(target, work_item, task, "needs-user-guidance")
            except Exception as error:
                return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
            return OrchestrationOutcome("escalated", target.number, task.agent.name)

        open_disputes = self._open_project_owner_disputes(comments)
        challenge_work_item = dict(work_item)
        challenge_work_item["requirements_challenge"] = {
            "round": challenge_round,
            "prior_round": challenge_round - 1 or None,
        }
        challenge_work_item["open_disputes"] = open_disputes
        challenge_work_item["prior_findings"] = self._open_challenge_findings(comments)
        try:
            execution = task.agent.executor.execute(
                AgentExecutionRequest(
                    role_path=task.agent.role_path,
                    workspace=self._workspace,
                    work_item=challenge_work_item,
                    output_schema_path=self._requirements_challenge_schema_path,
                    reference_paths=role_reference_paths(
                        task.agent.role_path, invocation_mode=task.invocation_mode
                    ),
                )
            )
            self._total_cost_usd += execution.cost_usd
            result = execution.output
            validate_requirements_challenge_result(result, challenge_round, open_disputes)
            comment = render_requirements_challenge_result(
                result,
                source_state=task.source_state,
                next_state="needs-decision",
                invocation_id=self._id_factory(),
                challenge_round=challenge_round,
                findings=result.get("findings") or [],
            )
            self._reviewer_github.create_issue_comment(target, comment)
            self._apply_transition(target, work_item, task, "needs-decision")
        except Exception as error:
            return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
        return OrchestrationOutcome("processed", target.number, task.agent.name)

    def _find_final_design_comment(
        self, comments: Iterable[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """The canonical Final Design comment, if one has been posted."""
        for comment in reversed(list(comments)):
            if "<!-- agent-army:final_design " in str(comment.get("body") or ""):
                return comment
        return None

    @staticmethod
    def _signoff_revision(comments: Iterable[dict[str, Any]]) -> int:
        """How many correction rounds the sign-off gate has already spent."""
        return sum(
            1
            for attributes in _iter_markers(comments)
            if attributes.get("kind") == "signoff"
            and attributes.get("outcome") == "correction"
        )

    def _is_stamped(self, comment: dict[str, Any]) -> bool:
        """Has the Reviewer stamped this comment as a faithful record?"""
        if self._reviewer_github is None:
            return False
        comment_id = comment.get("id")
        if comment_id is None:
            return False
        try:
            reactions = self._reviewer_github.list_reactions(
                self.owner, self.repository, int(comment_id)
            )
        except Exception:
            return False
        return any(
            reaction.get("content") == DESIGN_SIGNOFF_REACTION for reaction in reactions
        )

    def _run_design_signoff_task(
        self, target: GitHubTarget, work_item: dict[str, Any], task: _Task
    ) -> OrchestrationOutcome:
        """Gate development on the Reviewer agreeing the write-up is faithful.

        This checks fidelity, not substance: the argument is already settled by
        the time a Final Design exists. The Reviewer either stamps the comment
        or says what it misrecords, and Project Owner revises it in place.
        """
        if self._reviewer_github is None or self._signoff_schema_path is None:
            return OrchestrationOutcome(
                "failed", target.number, task.agent.name, "Design sign-off is not configured."
            )
        comments = work_item["issue"].get("comments", [])
        design = self._find_final_design_comment(comments)
        if design is None:
            return OrchestrationOutcome(
                "failed", target.number, task.agent.name, "No Final Design comment to sign off."
            )
        if self._is_stamped(design):
            # Already agreed in a prior pass; only the label lagged behind.
            try:
                self._apply_transition(target, work_item, task, "ready-for-development")
            except Exception as error:
                return OrchestrationOutcome(
                    "retrying-label-update", target.number, task.agent.name, str(error)
                )
            return OrchestrationOutcome("recovered", target.number, task.agent.name)

        revision = self._signoff_revision(comments)
        if revision >= MAX_SIGNOFF_ROUNDS:
            try:
                self._reviewer_github.create_issue_comment(
                    target,
                    "## Agent Army: Final Design sign-off did not converge\n\n"
                    f"The Reviewer has posted {revision} corrections without accepting the "
                    "Final Design write-up. Failing to agree on a transcription of an "
                    "already-settled argument needs a human.\n\n"
                    f"Workflow transition: `{task.source_state}` → `needs-user-guidance`.",
                )
                self._apply_transition(target, work_item, task, "needs-user-guidance")
            except Exception as error:
                return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
            return OrchestrationOutcome("escalated", target.number, task.agent.name)

        signoff_work_item = dict(work_item)
        signoff_work_item["final_design"] = str(design.get("body") or "")
        signoff_work_item["signoff_revision"] = revision + 1
        try:
            execution = task.agent.executor.execute(
                AgentExecutionRequest(
                    role_path=task.agent.role_path,
                    workspace=self._workspace,
                    work_item=signoff_work_item,
                    output_schema_path=self._signoff_schema_path,
                    reference_paths=role_reference_paths(task.agent.role_path),
                )
            )
            self._total_cost_usd += execution.cost_usd
            result = execution.output
            validate_signoff_result(result)
            if result["outcome"] == "accepted":
                self._reviewer_github.create_reaction(
                    self.owner, self.repository, int(design["id"]), DESIGN_SIGNOFF_REACTION
                )
                self._apply_transition(target, work_item, task, "ready-for-development")
                return OrchestrationOutcome("processed", target.number, task.agent.name)
            correction = render_signoff_correction(
                result, invocation_id=self._id_factory(), revision=revision + 1
            )
            self._reviewer_github.create_issue_comment(target, correction)
            # Stay in this state: Project Owner revises the design in place.
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
            # The review outcome is recorded on the pull request now.
            pull_conversation = GitHubTarget(
                self.owner, self.repository, int(pull_request["number"]), "issue"
            )
            review_comments = task.agent.github.get_issue_comments(pull_conversation)
            if self._latest_review_outcome(review_comments, pull_request) == "changes-requested":
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
        if branch_exists and self._branch_is_ahead(branch, base_branch):
            # A prior run already committed and pushed this branch -- most likely
            # create_pull_request failed after a successful push (for example, a
            # missing App permission) -- and left the issue in ready-for-development
            # with nothing new to implement. Re-running the executor here would only
            # find an empty diff and fail again on every retry. Open the pull request
            # for the existing commits instead of re-invoking the executor.
            return self._open_pull_request_for_pushed_branch(
                target, work_item, task, branch, base_branch, title
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
                execution = task.agent.executor.execute(
                    AgentExecutionRequest(
                        role_path=task.agent.role_path,
                        workspace=worktree.path,
                        work_item=work_item,
                        output_schema_path=task.agent.output_schema_path,
                        reference_paths=role_reference_paths(task.agent.role_path),
                    )
                )
                self._total_cost_usd += execution.cost_usd
                result = execution.output
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
        # The findings the Developer is answering live on the pull request,
        # where the review argument happens -- not on the originating issue.
        # implementation_work_item was read from the PR target, so its
        # comments are the PR conversation. It must fix or dispute each
        # blocking one; validate_developer_result rejects silent omission.
        pull_comments = implementation_work_item["issue"].get("comments", [])
        open_findings = self._open_review_findings(pull_comments)
        implementation_work_item["review_findings"] = open_findings
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
                execution = task.agent.executor.execute(
                    AgentExecutionRequest(
                        role_path=task.agent.role_path,
                        workspace=worktree.path,
                        work_item=implementation_work_item,
                        output_schema_path=task.agent.output_schema_path,
                        reference_paths=role_reference_paths(task.agent.role_path),
                    )
                )
                self._total_cost_usd += execution.cost_usd
                result = execution.output
                validate_developer_result(result, open_findings)
                if result["status"] != "completed":
                    # Route the question to Project Owner and hand over the
                    # label. Previously this returned with no comment and no
                    # transition, so the issue stayed in ready-for-development
                    # and every later poll re-ran the whole agent again.
                    blocked_comment = render_developer_blocked_result(
                        result,
                        source_state=task.source_state,
                        next_state="needs-decision",
                        invocation_id=self._id_factory(),
                    )
                    task.agent.github.create_issue_comment(target, blocked_comment)
                    self._apply_transition(target, work_item, task, "needs-decision")
                    return OrchestrationOutcome(
                        "processed", target.number, task.agent.name, result["summary"]
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

    def _branch_is_ahead(self, branch: str, base_branch: str) -> bool:
        """Whether `branch` already carries commits `base_branch` does not.

        Used only to decide whether a prior Developer run already finished and
        pushed; any failure to answer safely falls back to False, which simply
        re-runs the executor as before.
        """
        try:
            count = run_git(
                ["git", "rev-list", "--count", f"{base_branch}..{branch}"],
                self._workspace,
                **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
            ).stdout.strip()
        except RuntimeError:
            return False
        return count not in ("", "0")

    def _open_pull_request_for_pushed_branch(
        self,
        target: GitHubTarget,
        work_item: dict[str, Any],
        task: _Task,
        branch: str,
        base_branch: str,
        title: str,
    ) -> OrchestrationOutcome:
        """Open the pull request for a branch a prior run already pushed.

        No new implementation work happens here -- the branch already carries
        it. This exists for the case where commit_and_push previously
        succeeded but create_pull_request then failed (for example, the App
        installation was missing the Pull requests permission), leaving the
        issue in ready-for-development with nothing left to implement.
        """
        try:
            head_sha = run_git(
                ["git", "rev-parse", branch],
                self._workspace,
                **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
            ).stdout.strip()
            pull_request = task.agent.github.create_pull_request(
                self.owner,
                self.repository,
                title=f"Implement #{target.number}: {title}",
                head=branch,
                base=base_branch,
                body=(
                    f"Implements #{target.number}.\n\n"
                    "A previous Developer run committed and pushed this branch, but the "
                    "pull request could not be opened at the time. No further "
                    "implementation changes were made; this pull request forwards the "
                    "existing commit(s) for review."
                ),
            )
            next_state = "needs-optimization-review"
            comment = (
                f"<!-- agent-army:result role=developer from={task.source_state} "
                f"next={next_state} pr={pull_request['number']} branch={branch} "
                f"head={head_sha} -->\n"
                "## Agent Army: Developer PR opened\n\n"
                "A previous run already implemented and pushed this branch; the pull "
                "request could not be created at that time. It is now open: "
                f"[{pull_request['html_url']}]({pull_request['html_url']})."
            )
            task.agent.github.create_issue_comment(target, comment)
            self._apply_transition(target, work_item, task, next_state)
        except Exception as error:
            return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
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
        pull_conversation_target = GitHubTarget(self.owner, self.repository, pull_number, "issue")
        pull_request = self._reviewer_github.get_pull_request(pull_target)
        head_sha = str(pull_request["head"]["sha"])
        comments = work_item["issue"].get("comments", [])
        # The review argument is recorded on the pull request, so that is
        # where a prior round's durable marker lives.
        pull_comments = self._reviewer_github.get_issue_comments(pull_conversation_target)
        recovered = self._find_review_result(pull_comments, task, head_sha)
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
                attributes = self._review_marker_attributes(summary)
                next_state = attributes["next"]
                issue_comment = render_optimization_review_marker_comment(
                    invocation_id=attributes.get("invocation") or self._id_factory(),
                    source_state=attributes["from"],
                    next_state=next_state,
                    outcome=attributes["outcome"],
                    pull_request_number=pull_number,
                    branch=attributes.get("branch", pull_request["head"]["ref"]),
                    head_sha=attributes.get("head", head_sha),
                    pull_request_url=pull_request["html_url"],
                    round_number=int(attributes.get("round") or 1),
                    findings=decode_payload(summary).get("findings") or [],
                )
                self._reviewer_github.create_issue_comment(
                    pull_conversation_target, issue_comment
                )
                self._apply_transition(target, work_item, task, next_state)
                self._close_out_review(target, next_state, pull_number, pull_request)
            except Exception as error:
                return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
            return OrchestrationOutcome("recovered", target.number, task.agent.name)

        round_number = self._review_round(pull_comments, pull_number) + 1
        if round_number > MAX_CONVERGENCE_ROUNDS:
            # The argument is not converging on its own. Stop spending agent
            # runs on it and put the contested findings in front of a human.
            try:
                escalation = render_convergence_escalation(
                    source_state=task.source_state,
                    next_state="needs-user-guidance",
                    invocation_id=self._id_factory(),
                    pull_request_number=pull_number,
                    pull_request_url=pull_request["html_url"],
                    round_number=round_number - 1,
                    open_findings=[
                        finding
                        for finding in self._open_review_findings(pull_comments)
                        if finding.get("severity") == BLOCKING
                    ],
                )
                # Escalation is the one thing that must reach the issue: it
                # is a workflow handover to a human, not review discussion.
                self._reviewer_github.create_issue_comment(target, escalation)
                self._apply_transition(target, work_item, task, "needs-user-guidance")
            except Exception as error:
                return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
            return OrchestrationOutcome("escalated", target.number, task.agent.name)

        open_disputes = self._open_developer_disputes(pull_comments)
        review_work_item = self._review_reader.read(pull_target)
        review_work_item["source_issue"] = work_item
        # The argument so far, structured: what the Developer disputed and
        # why, and this round's number. The Reviewer must answer each dispute.
        review_work_item["open_disputes"] = open_disputes
        review_work_item["prior_findings"] = self._open_review_findings(pull_comments)
        review_work_item["review_round"] = round_number
        try:
            with self._worktree_factory(
                self._workspace,
                base_ref=head_sha,
                branch=f"review-{pull_number}",
                create_branch=False,
                **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
            ) as worktree:
                execution = task.agent.executor.execute(
                    AgentExecutionRequest(
                        role_path=task.agent.role_path,
                        workspace=worktree.path,
                        work_item=review_work_item,
                        output_schema_path=task.agent.output_schema_path,
                        reference_paths=role_reference_paths(task.agent.role_path),
                    )
                )
                self._total_cost_usd += execution.cost_usd
                result = execution.output
                validate_optimization_review_result(result, head_sha, open_disputes)
                status = run_git(
                    ["git", "status", "--porcelain"],
                    worktree.path,
                    **({"command_runner": self._git_command_runner} if self._git_command_runner else {}),
                )
                if status.stdout.strip():
                    raise ValueError("Optimization Reviewer modified the review workspace.")
            next_state = REVIEW_OUTCOME_STATES[result["outcome"]]
            invocation_id = self._id_factory()
            full_comment = render_optimization_review_result(
                result,
                source_state=task.source_state,
                next_state=next_state,
                invocation_id=invocation_id,
                pull_request_number=pull_number,
                pull_request_url=pull_request["html_url"],
                branch=pull_request["head"]["ref"],
                head_sha=head_sha,
                round_number=round_number,
            )
            conclusion = {
                "approved": "success",
                "changes-requested": "failure",
                "unable-to-assess": "neutral",
            }[result["outcome"]]
            check_run_error: str | None = None
            try:
                self._reviewer_github.create_check_run(
                    self.owner,
                    self.repository,
                    name=OPTIMIZATION_REVIEW_CHECK_NAME,
                    head_sha=head_sha,
                    conclusion=conclusion,
                    summary=full_comment,
                    details_url=pull_request["html_url"],
                )
            except Exception as error:
                # The check run is a supplementary status; the issue comment
                # marker below is what recovery keys off of. Don't burn
                # another full agent run on the next poll just because this
                # call failed (e.g. a transient permissions error).
                check_run_error = str(error)
            # Review discussion lives entirely on the pull request, next to
            # the diff it is about. The issue stays quiet during the review
            # loop; Project Owner does the issue-side housekeeping once the
            # PR is settled. The PR comment carries the durable marker, so
            # recovery reads the PR conversation rather than the issue.
            self._reviewer_github.create_issue_comment(pull_conversation_target, full_comment)
            self._apply_transition(target, work_item, task, next_state)
            self._close_out_review(target, next_state, pull_number, pull_request)
        except Exception as error:
            return OrchestrationOutcome("failed", target.number, task.agent.name, str(error))
        if check_run_error is not None:
            return OrchestrationOutcome(
                "processed", target.number, task.agent.name,
                f"Optimization Review check run could not be created: {check_run_error}",
            )
        return OrchestrationOutcome("processed", target.number, task.agent.name)

    def _attach_linked_pull_request(self, work_item: dict[str, Any]) -> None:
        """Give issue-side agents (mainly Project Owner) the linked PR's real
        content -- diff, reviews, discussion -- directly, the same way
        _run_reviewer_task already attaches the issue as `source_issue` for
        the Reviewer. This is what lets the Optimization Reviewer stop
        duplicating its full write-up onto the issue: any agent that needs
        the full findings gets them here instead. Best-effort: a PR-read
        failure should not block the issue-side task, which can still
        proceed with issue-only context.
        """
        if self._review_reader is None:
            return
        marker = self._find_developer_marker(work_item["issue"].get("comments", []))
        if marker is None:
            return
        pull_target = GitHubTarget(self.owner, self.repository, int(marker["pr"]), "pull_request")
        try:
            pull_work_item = self._review_reader.read(pull_target)
        except Exception:
            return
        work_item["pull_request"] = pull_work_item["pull_request"]

    @staticmethod
    def _latest_payload(comments: Iterable[dict[str, Any]], role: str) -> dict[str, Any]:
        """Read back the newest structured argument state posted by one role.

        The rendered prose is for humans; this is what the next round actually
        reasons over, so a restarted orchestrator resumes the argument where it
        left off instead of starting a fresh one.
        """
        for comment in reversed(list(comments)):
            body = str(comment.get("body") or "")
            if f"role={role}" not in body:
                continue
            payload = decode_payload(body)
            if payload:
                return payload
        return {}

    def _open_challenge_findings(
        self, comments: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """The challenge findings Project Owner must currently answer for."""
        for comment in reversed(list(comments)):
            body = str(comment.get("body") or "")
            if "mode=requirements_challenge" not in body:
                continue
            payload = decode_payload(body)
            if payload:
                return list(payload.get("findings") or [])
        return []

    def _open_project_owner_disputes(
        self, comments: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """The Project Owner disputes the Reviewer must concede or hold."""
        responses = self._latest_payload(comments, PROJECT_OWNER).get("responses") or []
        return [item for item in responses if item.get("disposition") == "disputed"]

    def _open_review_findings(self, comments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """The findings the Developer must currently answer for."""
        return list(self._latest_payload(comments, OPTIMIZATION_REVIEWER).get("findings") or [])

    def _open_developer_disputes(
        self, comments: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """The Developer disputes the Reviewer must currently concede or hold."""
        comments = list(comments)
        responses = self._latest_payload(comments, DEVELOPER).get("responses") or []
        return [item for item in responses if item.get("disposition") == "disputed"]

    @staticmethod
    def _review_round(comments: Iterable[dict[str, Any]], pull_number: int) -> int:
        """How many review rounds this pull request has already had."""
        return sum(
            1
            for attributes in _iter_markers(comments)
            if attributes.get("kind") == "result"
            and attributes.get("role") == OPTIMIZATION_REVIEWER
            and attributes.get("pr") == str(pull_number)
        )

    def _close_out_review(
        self,
        target: GitHubTarget,
        next_state: str,
        pull_number: int,
        pull_request: dict[str, Any],
    ) -> None:
        """Record the settled review on the issue, in Project Owner's voice.

        The argument itself stays on the pull request. This is issue-side
        housekeeping -- the trace that says why the label moved -- and it is
        best-effort: the label transition already happened and the pull
        request holds the durable record, so failing to post must not undo it.
        """
        if next_state != "ready-for-merge":
            return
        try:
            self._project_owner_github.create_issue_comment(
                target,
                "## Agent Army: ready to merge\n\n"
                f"[Pull request #{pull_number}]({pull_request['html_url']}) passed "
                "Optimization Review. The review discussion is on the pull request.\n\n"
                f"Workflow transition: `needs-optimization-review` → `{next_state}`.",
            )
        except Exception:
            return

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
        # A prior review for this exact head is recorded on the pull request.
        pull_conversation = GitHubTarget(
            self.owner, self.repository, int(marker["pr"]), "issue"
        )
        pull_comments = self._reviewer_github.get_issue_comments(pull_conversation)
        if self._find_review_result(pull_comments, task, head_sha):
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
    def _review_marker_attributes(summary: str) -> dict[str, str]:
        markers = list(_iter_markers([{"body": summary}]))
        for attributes in reversed(markers):
            if (
                attributes.get("role") == OPTIMIZATION_REVIEWER
                and attributes.get("next") in REVIEW_OUTCOME_STATES.values()
            ):
                return attributes
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
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    if outcome.status == "idle":
        return f"[{timestamp}] Agent Army poll complete: no eligible issue."
    issue = f" issue #{outcome.issue_number}" if outcome.issue_number else ""
    role = f" ({outcome.role})" if outcome.role else ""
    detail = f": {outcome.detail}" if outcome.detail else ""
    return f"[{timestamp}] Agent Army poll complete: {outcome.status}{issue}{role}{detail}"


def format_dry_run_outcome(outcome: DryRunOutcome) -> str:
    """Render a DryRunOutcome as one human-readable report line."""
    skipped = "; ".join(f"#{candidate.issue_number} {candidate.reason}" for candidate in outcome.skipped)
    if outcome.status == "no-open-issues":
        return "no open issues"
    if outcome.status == "no-eligible-issue":
        return f"no eligible issue (skipped: {skipped})" if skipped else "no eligible issue"
    prefix = f"skipped: {skipped}; " if skipped else ""
    mode = f" ({outcome.mode})" if outcome.mode else ""
    label = outcome.current_label
    if outcome.status == "would-invoke":
        return f"{prefix}issue #{outcome.issue_number} [{label}] would invoke {outcome.role}{mode}"
    if outcome.status == "would-recover":
        next_state = outcome.next_state or "unknown next state"
        return (
            f"{prefix}issue #{outcome.issue_number} [{label}] would recover "
            f"(relabel only, no agent invoked) -> {next_state}"
        )
    raise ValueError(f"Unknown dry-run status: {outcome.status}")
