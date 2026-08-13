"""Render validated agent results into concise GitHub comments."""

from __future__ import annotations

import json
import re
from typing import Any


REQUIRED_RESULT_FIELDS = {
    "summary",
    "evidence",
    "questions",
    "recommended_actions",
    "files_changed",
    "commands_run",
}
WORKFLOW_STATES = {
    "needs-grooming",
    "needs-design-signoff",
    "needs-decision",
    "needs-documentation",
    "ready-for-development",
    "needs-user-guidance",
    "needs-requirements-challenge",
    "needs-optimization-review",
    "ready-for-merge",
}
PROJECT_OWNER_STATES = {
    "needs-grooming",
    "needs-design-signoff",
    "needs-decision",
    "needs-documentation",
    "ready-for-development",
    "needs-user-guidance",
    "needs-requirements-challenge",
}
REVIEW_OUTCOMES = {"approved", "changes-requested", "unable-to-assess"}
REVIEW_OUTCOME_STATES = {
    "approved": "ready-for-merge",
    "changes-requested": "ready-for-development",
    "unable-to-assess": "needs-decision",
}
OPTIMIZATION_REVIEW_CHECK_NAME = "Agent Army / Optimization Review"
REQUIREMENTS_CHALLENGE_OUTCOMES = {
    "concerns-found",
    "no-material-concerns",
    "unable-to-assess",
}

# The JSON Schema only enforces shape (string/array-of-string), so a
# schema-valid but content-empty response -- e.g. summary "test", evidence
# ["a"] -- would otherwise sail through untouched and get published as the
# durable record. These are deliberately blunt, deterministic checks against
# obvious placeholder text, not a judgment of decision quality; a genuinely
# short but real answer should still clear them easily.
_PLACEHOLDER_TEXT = {
    "test", "tests", "n/a", "na", "todo", "tbd", "wip", "x", "-", "a", "none", "...", "tba",
}
_MIN_SUMMARY_LENGTH = 15
_MIN_LIST_ITEM_LENGTH = 10
_PROSE_LIST_FIELDS = ("evidence", "recommended_actions", "questions")

BLOCKING = "blocking"
FINDING_SEVERITIES = {BLOCKING, "should-fix", "nit"}
DEVELOPER_DISPOSITIONS = {"accepted", "disputed"}
REVIEWER_DISPOSITIONS = {"conceded", "held"}
# Rounds are deliberately not capped: an argument runs until it converges. This
# is the point at which an unconverged argument stops being productive and gets
# a human involved instead of quietly spending more on further rounds.
MAX_CONVERGENCE_ROUNDS = 7
# The sign-off gate checks whether the write-up faithfully records what was
# already argued -- not the substance again -- so it gets a smaller budget.
# Failing to agree on a transcription in three passes is itself the signal.
MAX_SIGNOFF_ROUNDS = 3
FINAL_DESIGN_HEADING = "## Final Design:"
SIGNOFF_OUTCOMES = {"accepted", "correction"}

# A claim that cannot be re-checked is an opinion. Blocking findings and
# disputes -- the two moves that cost the other side real work -- must point at
# something the other agent can independently verify: a file:line, a backticked
# command or symbol, or a URL. This is deliberately lenient; it cannot tell a
# good argument from a bad one, only an anchored one from "typically you'd
# want...".
_RECHECKABLE_PATTERNS = (
    re.compile(r"[\w./-]+\.\w+:\d+"),
    re.compile(r"`[^`]+`"),
    re.compile(r"https?://\S+"),
)
_PAYLOAD_PATTERN = re.compile(r"<!-- agent-army:payload (?P<payload>\{.*?\}) -->", re.DOTALL)


# Agents occasionally emit "\\n" inside a JSON string instead of a real
# newline, which renders on GitHub as a literal backslash-n and turns the
# durable record -- the one artifact a human actually reads -- into an
# unreadable run-on blob. Repairing that blindly would corrupt legitimate
# content, since these roles routinely discuss code: a finding arguing for
# `\n` over `\r\n` must survive intact. Two guards keep the repair narrow:
# text that already contains real newlines is left alone (the agent formatted
# it properly, so any escape in it is deliberate), and inline-code spans are
# never touched.
_INLINE_CODE_PATTERN = re.compile(r"(`[^`]*`)")


def normalize_escaped_newlines(text: str) -> str:
    """Repair whole-string newline escaping without touching real content."""
    if "\n" in text or "\\n" not in text:
        return text
    parts = _INLINE_CODE_PATTERN.split(text)
    return "".join(
        part
        if index % 2
        else part.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
        for index, part in enumerate(parts)
    )


def _prose(text: str) -> str:
    """Agent-authored prose, ready to render."""
    return normalize_escaped_newlines(text.strip())


def _is_recheckable(text: str) -> bool:
    return any(pattern.search(text) for pattern in _RECHECKABLE_PATTERNS)


def encode_payload(payload: dict[str, Any]) -> str:
    """Embed durable structured state in a comment.

    The rendered prose is for humans; this is the machine-readable record the
    next round reads back, so an argument survives an orchestrator restart.
    `>` is escaped so a finding's own text can never terminate the HTML
    comment early.
    """
    serialized = json.dumps(payload, separators=(",", ":")).replace(">", "\\u003e")
    return f"<!-- agent-army:payload {serialized} -->"


def decode_payload(body: str) -> dict[str, Any]:
    """Read back the structured state embedded by encode_payload."""
    match = _PAYLOAD_PATTERN.search(body or "")
    if match is None:
        return {}
    try:
        payload = json.loads(match["payload"])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _reject_placeholder_text(field: str, text: str, *, min_length: int) -> None:
    stripped = text.strip()
    if stripped.lower() in _PLACEHOLDER_TEXT or len(stripped) < min_length:
        raise ValueError(
            f"Analysis result {field} looks like placeholder text, not a real answer: {text!r}"
        )


def validate_analysis_result(result: dict[str, Any]) -> None:
    missing = REQUIRED_RESULT_FIELDS - result.keys()
    if missing:
        raise ValueError(f"Analysis result is missing fields: {', '.join(sorted(missing))}")
    if not isinstance(result["summary"], str):
        raise ValueError("Analysis result summary must be text.")
    for field in REQUIRED_RESULT_FIELDS - {"summary"}:
        if not isinstance(result[field], list) or not all(
            isinstance(item, str) for item in result[field]
        ):
            raise ValueError(f"Analysis result {field} must be a list of text items.")
    _reject_placeholder_text("summary", result["summary"], min_length=_MIN_SUMMARY_LENGTH)
    for field in _PROSE_LIST_FIELDS:
        for item in result[field]:
            _reject_placeholder_text(field, item, min_length=_MIN_LIST_ITEM_LENGTH)


def render_documentation_analysis(result: dict[str, Any]) -> str:
    """Render an issue-grooming report without exposing internal execution details."""
    validate_analysis_result(result)
    lines = ["## Doku: documentation analysis", "", _prose(result["summary"])]
    _append_section(lines, "Evidence", result["evidence"])
    _append_section(lines, "Questions to resolve", result["questions"])
    _append_section(lines, "Recommended next steps", result["recommended_actions"])
    if result["files_changed"]:
        _append_section(lines, "Documentation files changed", result["files_changed"])
    lines.extend(
        [
            "",
            "_Generated by Doku from the issue, repository workspace, and configured evidence rules. "
            "No GitHub changes were made beyond this comment._",
        ]
    )
    return "\n".join(lines)


def validate_orchestration_result(
    result: dict[str, Any], role: str, *, prior_challenge_round: int | None = None
) -> None:
    """Validate the small result contract used by the polling orchestrator."""
    validate_analysis_result(result)
    next_state = result.get("next_state")
    if next_state not in PROJECT_OWNER_STATES:
        raise ValueError("Orchestrator result next_state must be a Project Owner workflow label.")
    if len(result["questions"]) > 1:
        raise ValueError("An orchestrated agent may ask at most one focused question.")

    if role == "project-owner":
        challenge_round = result.get("requirements_challenge_round")
        scope_changed = result.get("requirements_scope_changed")
        if next_state == "needs-requirements-challenge":
            if not isinstance(challenge_round, int) or challenge_round not in {1, 2}:
                raise ValueError(
                    "Requirements challenge routing must specify round 1 or round 2."
                )
            if not isinstance(scope_changed, bool):
                raise ValueError(
                    "Requirements challenge routing must specify whether scope materially changed."
                )
            expected_round = 1 if prior_challenge_round is None else prior_challenge_round + 1
            if challenge_round != expected_round:
                raise ValueError("Requirements challenge round is not the next allowed round.")
            if challenge_round == 1 and scope_changed:
                raise ValueError("The initial requirements challenge cannot claim a scope revision.")
            if challenge_round == 2 and not scope_changed:
                raise ValueError("A follow-up requirements challenge requires a material scope change.")
        elif challenge_round is not None or scope_changed is not None:
            raise ValueError(
                "Requirements challenge metadata is only valid when routing to its workflow state."
            )
        if next_state == "needs-user-guidance" and len(result["questions"]) != 1:
            raise ValueError(
                "Project Owner must include exactly one focused question for needs-user-guidance."
            )
        if next_state != "needs-user-guidance" and result["questions"]:
            raise ValueError(
                "Project Owner questions must be routed through needs-user-guidance."
            )
    elif role == "documentation":
        if next_state != "needs-decision":
            raise ValueError("Documentation results must return to Project Owner via needs-decision.")
    else:
        raise ValueError(f"Unsupported orchestrated role: {role}")


def validate_developer_result(
    result: dict[str, Any], open_findings: list[dict[str, Any]] | None = None
) -> None:
    """Validate the implementation report returned by Developer.

    When the Developer is revising against review findings, it must take an
    explicit position on every blocking one: fix it, or dispute it with
    re-checkable evidence. Silent compliance and silent omission are both
    rejected -- convergence requires an argument, not deference.
    """
    validate_analysis_result(result)
    if result.get("status") not in {"completed", "blocked"}:
        raise ValueError("Developer must return completed or blocked status.")
    if len(result["questions"]) > 1:
        raise ValueError("Developer may ask at most one focused question.")
    if result["status"] == "blocked" and len(result["questions"]) != 1:
        raise ValueError("A blocked Developer result must include one focused question.")
    if result["status"] == "completed" and result["questions"]:
        raise ValueError("A completed Developer result cannot leave an unresolved question.")

    responses = result.get("responses") or []
    known_ids = {str(finding["id"]) for finding in (open_findings or [])}
    seen: set[str] = set()
    for response in responses:
        finding_id = str(response["finding_id"])
        if open_findings is not None and finding_id not in known_ids:
            raise ValueError(f"Developer responded to an unknown finding: {finding_id}")
        if finding_id in seen:
            raise ValueError(f"Developer responded twice to finding {finding_id}.")
        seen.add(finding_id)
        if response["disposition"] not in DEVELOPER_DISPOSITIONS:
            raise ValueError("Developer finding disposition must be accepted or disputed.")
        _reject_placeholder_text(
            "response rationale", response["rationale"], min_length=_MIN_LIST_ITEM_LENGTH
        )
        if response["disposition"] == "disputed" and not _is_recheckable(response["rationale"]):
            raise ValueError(
                f"Disputing finding {finding_id} requires re-checkable evidence "
                "(a file:line, a `command`, or a URL)."
            )

    for finding in open_findings or []:
        if finding.get("severity") == BLOCKING and str(finding["id"]) not in seen:
            raise ValueError(
                f"Developer must accept or dispute blocking finding {finding['id']}."
            )


def _validate_findings(
    result: dict[str, Any], open_disputes: list[dict[str, Any]] | None = None
) -> None:
    """Validate graded findings and the answers to the other side's disputes.

    Shared by PR review and the issue-level requirements challenge: the same
    argument contract applies wherever two roles argue. A reviewer may not
    ignore a dispute -- it either concedes the finding or holds it with its own
    re-checkable evidence.
    """
    findings = result.get("findings") or []
    seen_ids: set[str] = set()
    for finding in findings:
        finding_id = str(finding["id"])
        if finding_id in seen_ids:
            raise ValueError(f"Duplicate finding id: {finding_id}")
        seen_ids.add(finding_id)
        if finding["severity"] not in FINDING_SEVERITIES:
            raise ValueError("Finding severity must be blocking, should-fix, or nit.")
        _reject_placeholder_text("finding claim", finding["claim"], min_length=_MIN_LIST_ITEM_LENGTH)
        for item in finding["evidence"]:
            _reject_placeholder_text("finding evidence", item, min_length=_MIN_LIST_ITEM_LENGTH)
        if finding["severity"] == BLOCKING and not any(
            _is_recheckable(item) for item in finding["evidence"]
        ):
            raise ValueError(
                f"Blocking finding {finding_id} requires re-checkable evidence "
                "(a file:line, a `command`, or a URL)."
            )

    answered = set()
    for response in result.get("dispute_responses") or []:
        finding_id = str(response["finding_id"])
        if response["disposition"] not in REVIEWER_DISPOSITIONS:
            raise ValueError("Reviewer dispute disposition must be conceded or held.")
        _reject_placeholder_text(
            "dispute response rationale", response["rationale"], min_length=_MIN_LIST_ITEM_LENGTH
        )
        if response["disposition"] == "held" and not _is_recheckable(response["rationale"]):
            raise ValueError(
                f"Holding disputed finding {finding_id} requires re-checkable counter-evidence."
            )
        if response["disposition"] == "held" and finding_id not in seen_ids:
            raise ValueError(
                f"Finding {finding_id} was held but is not in the current findings list."
            )
        if response["disposition"] == "conceded" and finding_id in seen_ids:
            raise ValueError(
                f"Finding {finding_id} was conceded but is still listed as an open finding."
            )
        answered.add(finding_id)

    for dispute in open_disputes or []:
        if str(dispute["finding_id"]) not in answered:
            raise ValueError(
                f"Reviewer must concede or hold disputed finding {dispute['finding_id']}."
            )


def render_developer_blocked_result(
    result: dict[str, Any],
    *,
    source_state: str,
    next_state: str,
    invocation_id: str,
) -> str:
    """Render a Developer clarification as the durable handoff to Project Owner."""
    validate_developer_result(result)
    lines = [
        f"<!-- agent-army:result role=developer invocation={invocation_id} "
        f"from={source_state} next={next_state} status=blocked -->",
        "## Agent Army: Developer needs a decision",
        "",
        _prose(result["summary"]),
        "",
        f"Workflow transition: `{source_state}` → `{next_state}`.",
    ]
    _append_section(lines, "Evidence", result["evidence"])
    _append_section(lines, "Focused question", result["questions"])
    _append_section(lines, "Recommended actions", result["recommended_actions"])
    lines.extend(
        [
            "",
            "_Developer could not safely continue without a Project Owner decision._",
        ]
    )
    return "\n".join(lines)


def validate_optimization_review_result(
    result: dict[str, Any],
    expected_commit: str,
    open_disputes: list[dict[str, Any]] | None = None,
) -> None:
    """Validate an independent review outcome for the exact PR head commit."""
    validate_analysis_result(result)
    if result.get("outcome") not in REVIEW_OUTCOMES:
        raise ValueError("Optimization Reviewer must return a supported outcome.")
    if result.get("reviewed_commit") != expected_commit:
        raise ValueError("Optimization Reviewer result does not match the current PR commit.")
    if result["files_changed"]:
        raise ValueError("Optimization Reviewer must not change files.")
    _validate_findings(result, open_disputes)
    has_blocking = any(
        finding["severity"] == BLOCKING for finding in result.get("findings") or []
    )
    if result["outcome"] == "approved" and has_blocking:
        raise ValueError("A review cannot be approved while a blocking finding is open.")
    if result["outcome"] == "changes-requested" and not has_blocking:
        raise ValueError("changes-requested requires at least one blocking finding.")


def validate_requirements_challenge_result(
    result: dict[str, Any],
    expected_round: int,
    open_disputes: list[dict[str, Any]] | None = None,
) -> None:
    """Validate an issue-level challenge from the shared Reviewer role.

    The issue-level argument is held to the same contract as PR review: graded
    findings, re-checkable evidence on anything blocking, and no ignoring a
    Project Owner dispute.
    """
    validate_analysis_result(result)
    if result.get("outcome") not in REQUIREMENTS_CHALLENGE_OUTCOMES:
        raise ValueError("Requirements challenge must return a supported outcome.")
    if result.get("challenge_round") != expected_round:
        raise ValueError("Requirements challenge result does not match the current round.")
    if result["files_changed"]:
        raise ValueError("Requirements Reviewer must not change files.")
    if len(result["questions"]) > 1:
        raise ValueError("Requirements Reviewer may ask at most one focused question.")
    _validate_findings(result, open_disputes)
    has_blocking = any(
        finding["severity"] == BLOCKING for finding in result.get("findings") or []
    )
    if result["outcome"] == "concerns-found" and not has_blocking:
        raise ValueError("concerns-found requires at least one blocking finding.")
    if result["outcome"] == "no-material-concerns" and has_blocking:
        raise ValueError("no-material-concerns cannot leave a blocking finding open.")


def render_orchestration_result(
    result: dict[str, Any],
    *,
    role: str,
    source_state: str,
    next_state: str,
    invocation_id: str,
    prior_challenge_round: int | None = None,
) -> str:
    """Render a validated agent result with durable orchestration metadata."""
    validate_orchestration_result(
        result, role, prior_challenge_round=prior_challenge_round
    )
    heading = "Project Owner" if role == "project-owner" else "Doku"
    marker = (
        f"<!-- agent-army:result role={role} invocation={invocation_id} "
        f"from={source_state} next={next_state}"
    )
    if next_state == "needs-requirements-challenge":
        marker += (
            f" challenge_round={result['requirements_challenge_round']}"
            f" scope_changed={str(result['requirements_scope_changed']).lower()}"
        )
    marker += " -->"
    lines = [
        marker,
        f"## Agent Army: {heading} completed",
        "",
        "### Decision" if role == "project-owner" else "### Summary",
        "",
        _prose(result["summary"]),
        "",
        f"Workflow transition: `{source_state}` → `{next_state}`.",
    ]
    _append_section(lines, "Evidence", result["evidence"])
    _append_section(lines, "Focused question", result["questions"])
    _append_section(lines, "Recommended actions", result["recommended_actions"])
    if result["files_changed"]:
        _append_section(lines, "Files changed", result["files_changed"])
    lines.extend(
        [
            "",
            "_This validated result is the durable record used by the Agent Army polling workflow._",
        ]
    )
    return "\n".join(lines)


def render_developer_result(
    result: dict[str, Any],
    *,
    source_state: str,
    next_state: str,
    invocation_id: str,
    pull_request_number: int,
    pull_request_url: str,
    branch: str,
    head_sha: str,
) -> str:
    """Render Developer's validated implementation and PR handoff."""
    validate_developer_result(result)
    responses = result.get("responses") or []
    disputed = [item for item in responses if item["disposition"] == "disputed"]
    lines = [
        f"<!-- agent-army:result role=developer invocation={invocation_id} "
        f"from={source_state} next={next_state} pr={pull_request_number} "
        f"branch={branch} head={head_sha} -->",
        encode_payload({"responses": responses}),
        "## Agent Army: Developer completed",
        "",
        _prose(result["summary"]),
        "",
        f"Pull request: [{pull_request_url}]({pull_request_url})",
        f"Branch: `{branch}`",
        f"Workflow transition: `{source_state}` → `{next_state}`.",
    ]
    if disputed:
        lines.extend(
            [
                "",
                f"Disputing {len(disputed)} review finding(s); the Reviewer must "
                "concede or hold each one.",
            ]
        )
    lines.extend(render_developer_responses(responses))
    _append_section(lines, "Evidence", result["evidence"])
    _append_section(lines, "Focused question", result["questions"])
    _append_section(lines, "Recommended actions", result["recommended_actions"])
    if result["files_changed"]:
        _append_section(lines, "Files changed", result["files_changed"])
    lines.extend(["", "_Developer work was prepared in an isolated workspace._"])
    return "\n".join(lines)


def render_optimization_review_result(
    result: dict[str, Any],
    *,
    source_state: str,
    next_state: str,
    invocation_id: str,
    pull_request_number: int,
    pull_request_url: str,
    branch: str,
    head_sha: str,
    round_number: int = 1,
) -> str:
    """Render one round of the review argument on the pull request.

    This comment is the durable record for the review loop -- the issue stays
    quiet -- so it carries the marker and the payload that the next round and
    any recovery pass read back.
    """
    validate_optimization_review_result(result, head_sha)
    outcome = result["outcome"]
    findings = result.get("findings") or []
    blocking = [finding for finding in findings if finding["severity"] == BLOCKING]
    lines = [
        f"<!-- agent-army:result role=optimization-reviewer invocation={invocation_id} "
        f"from={source_state} next={next_state} outcome={outcome} "
        f"pr={pull_request_number} branch={branch} head={head_sha} "
        f"round={round_number} -->",
        encode_payload({"findings": findings, "round": round_number}),
        f"## Agent Army: Optimization Reviewer completed — round {round_number}",
        "",
        f"Outcome: **{outcome}**",
        "",
        f"Convergence: **{len(blocking)} blocking**, "
        f"{len([f for f in findings if f['severity'] == 'should-fix'])} should-fix, "
        f"{len([f for f in findings if f['severity'] == 'nit'])} nit",
        "",
        _prose(result["summary"]),
        "",
        f"Pull request: [{pull_request_url}]({pull_request_url})",
        f"Commit reviewed: `{head_sha}`",
        f"Workflow transition: `{source_state}` → `{next_state}`.",
    ]
    _append_findings(lines, result.get("findings") or [])
    _append_dispute_responses(lines, result.get("dispute_responses") or [])
    _append_section(lines, "Evidence", result["evidence"])
    _append_section(lines, "Questions", result["questions"])
    _append_section(lines, "Recommended actions", result["recommended_actions"])
    lines.extend(["", "_This review did not modify the pull request._"])
    return "\n".join(lines)


def _append_findings(lines: list[str], findings: list[dict[str, Any]]) -> None:
    if not findings:
        return
    order = {BLOCKING: 0, "should-fix": 1, "nit": 2}
    lines.extend(["", "### Findings", ""])
    for finding in sorted(findings, key=lambda item: order.get(item["severity"], 3)):
        lines.append(f"- **[{finding['severity']}] {finding['id']}** — {_prose(finding['claim'])}")
        for item in finding["evidence"]:
            lines.append(f"  - {_prose(item)}")


def _append_dispute_responses(lines: list[str], responses: list[dict[str, Any]]) -> None:
    if not responses:
        return
    lines.extend(["", "### Answers to disputes", ""])
    for response in responses:
        lines.append(
            f"- **{response['finding_id']}: {response['disposition']}** — "
            f"{_prose(response['rationale'])}"
        )


def render_developer_responses(responses: list[dict[str, Any]]) -> list[str]:
    """Render the Developer's position on each review finding."""
    if not responses:
        return []
    lines = ["", "### Responses to review findings", ""]
    for response in responses:
        lines.append(
            f"- **{response['finding_id']}: {response['disposition']}** — "
            f"{_prose(response['rationale'])}"
        )
    return lines


def render_convergence_escalation(
    *,
    source_state: str,
    next_state: str,
    invocation_id: str,
    pull_request_number: int,
    pull_request_url: str,
    round_number: int,
    open_findings: list[dict[str, Any]],
) -> str:
    """Hand an unconverged argument to a human after MAX_CONVERGENCE_ROUNDS.

    Convergence is the goal, but an argument that has not converged in this
    many rounds is not going to converge by spending another agent run on it.
    """
    lines = [
        # Its own marker kind: _iter_markers takes `kind` from the
        # agent-army:<kind> prefix, so an escalation carrying kind=escalation
        # as an attribute would read back as an ordinary result and be counted
        # as another review round.
        f"<!-- agent-army:escalation role=optimization-reviewer invocation={invocation_id} "
        f"from={source_state} next={next_state} "
        f"pr={pull_request_number} round={round_number} -->",
        "## Agent Army: review did not converge",
        "",
        f"Developer and Optimization Reviewer have exchanged {round_number} rounds on "
        f"[pull request #{pull_request_number}]({pull_request_url}) without resolving "
        "every blocking finding. Further rounds are unlikely to converge on their own, "
        "so this needs a human decision.",
        "",
        f"Workflow transition: `{source_state}` → `{next_state}`.",
    ]
    _append_findings(lines, open_findings)
    lines.extend(
        [
            "",
            "_Resolve the contested findings above, then replace this label with an "
            "actionable workflow state._",
        ]
    )
    return "\n".join(lines)


def render_optimization_review_marker_comment(
    *,
    invocation_id: str,
    source_state: str,
    next_state: str,
    outcome: str,
    pull_request_number: int,
    pull_request_url: str,
    branch: str,
    head_sha: str,
    round_number: int = 1,
    findings: list[dict[str, Any]] | None = None,
) -> str:
    """Render the durable issue-side marker for a review outcome.

    The full argument belongs on the pull request, next to the diff it's
    about. The issue keeps the durable workflow record: the marker, a
    convergence status a human can read in seconds, and the embedded payload
    the next round reads back.
    """
    findings = findings or []
    blocking = [finding for finding in findings if finding["severity"] == BLOCKING]
    lines = [
        f"<!-- agent-army:result role=optimization-reviewer invocation={invocation_id} "
        f"from={source_state} next={next_state} outcome={outcome} "
        f"pr={pull_request_number} branch={branch} head={head_sha} "
        f"round={round_number} -->",
        encode_payload({"findings": findings, "round": round_number}),
        f"## Agent Army: Optimization Reviewer completed — round {round_number}",
        "",
        f"Outcome: **{outcome}**",
        "",
        f"Convergence: **{len(blocking)} blocking**, "
        f"{len([f for f in findings if f['severity'] == 'should-fix'])} should-fix, "
        f"{len([f for f in findings if f['severity'] == 'nit'])} nit",
        "",
        f"Full review: [pull request #{pull_request_number}]({pull_request_url})",
        f"Commit reviewed: `{head_sha}`",
        f"Workflow transition: `{source_state}` → `{next_state}`.",
    ]
    if blocking:
        lines.extend(["", "### Open blocking findings", ""])
        for finding in blocking:
            lines.append(f"- **{finding['id']}** — {finding['claim'].strip()}")
    lines.extend(
        [
            "",
            "_The full argument was posted on the pull request; this comment is "
            "the durable workflow record._",
        ]
    )
    return "\n".join(lines)


def render_final_design(
    design: str,
    *,
    source_state: str,
    next_state: str,
    invocation_id: str,
    revision: int,
) -> str:
    """Render the canonical converged design for Reviewer sign-off.

    One artifact, revised in place. A correction edits this comment rather
    than appending a competing version, so there is never ambiguity about
    which text the stamp applies to.
    """
    return "\n".join(
        [
            f"<!-- agent-army:final_design role=project-owner invocation={invocation_id} "
            f"from={source_state} next={next_state} revision={revision} -->",
            FINAL_DESIGN_HEADING,
            "",
            _prose(design),
            "",
            "---",
            "_Converged design awaiting Optimization Reviewer sign-off. The Reviewer "
            "checks that this faithfully records what was argued and conceded; it "
            "stamps this comment when it does, or posts a correction when it does not._",
        ]
    )


def render_signoff_correction(
    result: dict[str, Any],
    *,
    invocation_id: str,
    revision: int,
) -> str:
    """Render a Reviewer's objection that the write-up misrecords the argument."""
    lines = [
        f"<!-- agent-army:signoff role=optimization-reviewer invocation={invocation_id} "
        f"revision={revision} outcome=correction -->",
        f"## Agent Army: Final Design correction — revision {revision}",
        "",
        _prose(result["summary"]),
    ]
    _append_findings(lines, result.get("findings") or [])
    _append_section(lines, "Recommended actions", result["recommended_actions"])
    lines.extend(
        [
            "",
            "_Project Owner must revise the Final Design comment in place; this gate "
            "checks fidelity to the argument, not the substance of it._",
        ]
    )
    return "\n".join(lines)


def validate_signoff_result(result: dict[str, Any]) -> None:
    """Validate a sign-off verdict on the Final Design write-up."""
    validate_analysis_result(result)
    if result.get("outcome") not in SIGNOFF_OUTCOMES:
        raise ValueError("Sign-off must return accepted or correction.")
    if result["files_changed"]:
        raise ValueError("Sign-off must not change files.")
    findings = result.get("findings") or []
    if result["outcome"] == "correction" and not findings:
        raise ValueError("A sign-off correction must say what the write-up misrecords.")
    if result["outcome"] == "accepted" and findings:
        raise ValueError("An accepted sign-off cannot leave corrections outstanding.")
    _validate_findings(result)


def render_requirements_challenge_result(
    result: dict[str, Any],
    *,
    source_state: str,
    next_state: str,
    invocation_id: str,
    challenge_round: int,
    findings: list[dict[str, Any]] | None = None,
) -> str:
    """Render one round of the issue-level requirements argument."""
    validate_requirements_challenge_result(result, challenge_round)
    outcome = result["outcome"]
    findings = findings if findings is not None else (result.get("findings") or [])
    blocking = [finding for finding in findings if finding["severity"] == BLOCKING]
    lines = [
        f"<!-- agent-army:result role=optimization-reviewer mode=requirements_challenge "
        f"from={source_state} next={next_state} round={challenge_round} outcome={outcome} -->",
        encode_payload({"findings": findings, "round": challenge_round}),
        f"## Agent Army: requirements challenge — round {challenge_round}",
        "",
        f"Outcome: **{outcome}**",
        "",
        f"Convergence: **{len(blocking)} blocking**, "
        f"{len([f for f in findings if f['severity'] == 'should-fix'])} should-fix, "
        f"{len([f for f in findings if f['severity'] == 'nit'])} nit",
        "",
        _prose(result["summary"]),
        "",
        f"Workflow transition: `{source_state}` → `{next_state}`.",
    ]
    _append_findings(lines, findings)
    _append_dispute_responses(lines, result.get("dispute_responses") or [])
    _append_section(lines, "Challenge evidence", result["evidence"])
    _append_section(lines, "Focused question", result["questions"])
    _append_section(lines, "Recommended actions", result["recommended_actions"])
    lines.extend(
        [
            "",
            "_Project Owner must resolve this challenge before selecting the next workflow state._",
        ]
    )
    return "\n".join(lines)


def _append_section(lines: list[str], title: str, items: list[str]) -> None:
    if not items:
        return
    lines.extend(["", f"### {title}", ""])
    lines.extend(f"- {_prose(item)}" for item in items)
