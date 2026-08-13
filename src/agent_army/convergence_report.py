"""Read-only convergence instrumentation for Developer/Reviewer arguments.

Convergence is a property of the process, not of the world: two agents
agreeing tells you about the agents, not about the code. Nothing here judges
whether an argument was *good* -- that needs a verifier at least as good as
the arguers. What it does is compute cheap, deterministic signals from state
the orchestrator already persists, so a human knows which arguments are worth
opening and reading.

Every input is read back from durable issue comments, so this never touches
the orchestrator's write path and can be run at any time.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from agent_army.orchestrator import DEVELOPER, OPTIMIZATION_REVIEWER, _iter_markers
from agent_army.publishers import BLOCKING, decode_payload


# A clean first-round approval is unremarkable on a small change and a strong
# smell on a large one. Lines of diff, additions plus deletions.
LARGE_DIFF_LINES = 200
# Below this many responses, "never disputed" is not yet evidence of anything.
MIN_RESPONSES_FOR_DEFERENCE = 3
MIN_DISPUTES_FOR_STUBBORNNESS = 2
# A revision that accepted a finding but changed essentially nothing.
EMPTY_FIX_LINES = 3

CLEAN_APPROVAL = "clean-approval-on-large-diff"
DEVELOPER_DEFERENCE = "developer-never-disputed"
REVIEWER_STUBBORNNESS = "reviewer-never-conceded"
EMPTY_FIX = "empty-fix-acceptance"
ESCALATED = "escalated-unconverged"


@dataclass(frozen=True)
class ArgumentReport:
    """One issue's argument, measured."""

    issue_number: int
    pull_request: int | None = None
    rounds: int = 0
    findings_raised: int = 0
    blocking_raised: int = 0
    developer_accepted: int = 0
    developer_disputed: int = 0
    reviewer_conceded: int = 0
    reviewer_held: int = 0
    diff_lines: int = 0
    outcome: str | None = None
    escalated: bool = False
    flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def responses(self) -> int:
        return self.developer_accepted + self.developer_disputed

    @property
    def disputes_answered(self) -> int:
        return self.reviewer_conceded + self.reviewer_held


def _events(comments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ordered argument events, each pairing a marker with its payload."""
    events = []
    for comment in comments:
        body = str(comment.get("body") or "")
        for attributes in _iter_markers([{"body": body}]):
            role = attributes.get("role")
            if role in {DEVELOPER, OPTIMIZATION_REVIEWER}:
                events.append({"attributes": attributes, "payload": decode_payload(body)})
                break
    return events


def analyze_issue(
    issue_number: int,
    comments: Iterable[dict[str, Any]],
    *,
    diff_lines: int = 0,
    compare_lines: Callable[[str, str], int] | None = None,
) -> ArgumentReport:
    """Measure one issue's Developer/Reviewer argument.

    `compare_lines` reports the size of the change between two commits; it is
    injected so this stays runnable without GitHub. Omit it to skip the
    empty-fix check rather than guessing.
    """
    events = _events(comments)
    if not events:
        return ArgumentReport(issue_number)

    pull_request: int | None = None
    rounds = 0
    outcome: str | None = None
    escalated = False
    finding_ids: set[str] = set()
    blocking_ids: set[str] = set()
    accepted = disputed = conceded = held = 0
    flags: list[str] = []

    # Walk the argument in order so an acceptance can be checked against the
    # revision that was supposed to implement it.
    pending_acceptance_from: str | None = None
    for event in events:
        attributes = event["attributes"]
        payload = event["payload"]
        role = attributes.get("role")
        if attributes.get("pr", "").isdigit():
            pull_request = int(attributes["pr"])

        if role == OPTIMIZATION_REVIEWER:
            if attributes.get("kind") == "escalation":
                escalated = True
                continue
            if attributes.get("kind") != "result":
                continue
            rounds += 1
            outcome = attributes.get("outcome") or outcome
            for finding in payload.get("findings") or []:
                finding_ids.add(str(finding.get("id")))
                if finding.get("severity") == BLOCKING:
                    blocking_ids.add(str(finding.get("id")))
            for response in payload.get("dispute_responses") or []:
                if response.get("disposition") == "conceded":
                    conceded += 1
                elif response.get("disposition") == "held":
                    held += 1
            pending_acceptance_from = attributes.get("head")
            continue

        if role == DEVELOPER:
            responses = payload.get("responses") or []
            round_accepted = sum(
                1 for item in responses if item.get("disposition") == "accepted"
            )
            accepted += round_accepted
            disputed += sum(1 for item in responses if item.get("disposition") == "disputed")
            head = attributes.get("head")
            if (
                round_accepted
                and compare_lines is not None
                and pending_acceptance_from
                and head
                and pending_acceptance_from != head
                and compare_lines(pending_acceptance_from, head) < EMPTY_FIX_LINES
                and EMPTY_FIX not in flags
            ):
                # Said "accepted", then shipped nothing that could have
                # implemented it.
                flags.append(EMPTY_FIX)
            pending_acceptance_from = None

    responses = accepted + disputed
    if rounds == 1 and not finding_ids and diff_lines >= LARGE_DIFF_LINES:
        flags.append(CLEAN_APPROVAL)
    if responses >= MIN_RESPONSES_FOR_DEFERENCE and disputed == 0:
        flags.append(DEVELOPER_DEFERENCE)
    if disputed >= MIN_DISPUTES_FOR_STUBBORNNESS and conceded == 0 and held > 0:
        flags.append(REVIEWER_STUBBORNNESS)
    if escalated:
        flags.append(ESCALATED)

    return ArgumentReport(
        issue_number=issue_number,
        pull_request=pull_request,
        rounds=rounds,
        findings_raised=len(finding_ids),
        blocking_raised=len(blocking_ids),
        developer_accepted=accepted,
        developer_disputed=disputed,
        reviewer_conceded=conceded,
        reviewer_held=held,
        diff_lines=diff_lines,
        outcome=outcome,
        escalated=escalated,
        flags=tuple(flags),
    )


def _ratio(part: int, whole: int) -> str:
    if not whole:
        return "-"
    return f"{part}/{whole} ({round(100 * part / whole)}%)"


def render_report(reports: list[ArgumentReport]) -> str:
    """Render the arguments as a table a human can scan in seconds."""
    header = (
        f"{'ISSUE':<7}{'PR':<6}{'ROUNDS':<8}{'FINDINGS':<10}"
        f"{'DEV ACCEPTED':<16}{'REV CONCEDED':<16}{'DIFF':<8}FLAGS"
    )
    lines = [header, "-" * len(header)]
    scored = [report for report in reports if report.rounds or report.escalated]
    for report in reports:
        lines.append(
            f"{'#' + str(report.issue_number):<7}"
            f"{('#' + str(report.pull_request)) if report.pull_request else '-':<6}"
            f"{report.rounds or '-':<8}"
            f"{report.findings_raised or '-':<10}"
            f"{_ratio(report.developer_accepted, report.responses):<16}"
            f"{_ratio(report.reviewer_conceded, report.disputes_answered):<16}"
            f"{('+' + str(report.diff_lines)) if report.diff_lines else '-':<8}"
            + (" ".join(f"! {flag}" for flag in report.flags))
        )
    flagged = [report for report in scored if report.flags]
    rounds = sorted(report.rounds for report in scored)
    median = rounds[len(rounds) // 2] if rounds else 0
    lines.extend(
        [
            "",
            f"{len(scored)} argument(s) - {len(flagged)} flagged - median {median} round(s)",
        ]
    )
    if flagged:
        lines.append("Flagged arguments are worth opening and reading; the flags do not")
        lines.append("judge whether an argument was good, only that it looks unusual.")
    return "\n".join(lines)
