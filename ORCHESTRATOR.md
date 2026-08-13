# Issue-driven orchestrator

`run-orchestrator` is a long-running, single-worker polling service. It polls
one configured repository for open issues, selects the lowest-numbered eligible
issue, and performs at most one state-changing agent task per polling pass.
Webhooks, developer agents, deployment, pull-request creation, and pull-request
write workflows are outside this version.

Start the service with:

```bash
uv run run-orchestrator \
  --repository OWNER/REPOSITORY \
  --workspace /path/to/target-repository
```

Use `--once` for a single poll while developing or testing. The workspace is
passed to Codex with workspace-write permissions, but GitHub credentials remain
inside the Python process and are removed from the Codex subprocess environment.
The orchestrator explicitly embeds the selected Domain Modeling reference text
in the relevant role prompt, so the role can use it even when the target
workspace does not contain Agent Army's reference files.

## Supported workflow labels

The following labels are the complete workflow state vocabulary:

| Label | Meaning | Polling behavior |
| --- | --- | --- |
| `needs-grooming` | Project Owner should route and clarify the issue. | Dispatch Project Owner. |
| `needs-decision` | Project Owner must review evidence or answer a specialist question. | Dispatch Project Owner. |
| `needs-documentation` | Documentation analysis is required. | Dispatch Doku. |
| `ready-for-development` | Product direction is ready for a future implementation workflow. | Leave untouched in this version. |
| `needs-user-guidance` | A human answer is required before the issue can proceed. | Leave untouched until a human replaces this label with an actionable state, normally `needs-decision`. |

`orchestration-paused` is a neutral human-controlled pause label, not a
workflow state. When present, the orchestrator ignores the issue completely,
including an otherwise-unlabeled intake candidate, until a human removes the
label. It may coexist with an ordinary workflow state label, and is preserved
with all unrelated labels.

Other labels are preserved and do not affect routing. At most one supported
workflow state label may be present. Issues with multiple workflow state labels are
ambiguous and are skipped until a human restores that invariant.

An open issue with no supported workflow state label and without
`orchestration-paused` is a one-time intake candidate.
It is dispatched to Project Owner as `unlabeled` and, after a validated result,
receives Project Owner's selected next state. A durable Agent Army result marker
prevents a successful intake from being dispatched again. A transient failed
intake remains unlabeled and is retried on a later poll.

## Ownership and transitions

Project Owner is the initial router and the only agent that selects a workflow
state. The orchestrator applies that selection only after validating the JSON
result and then replaces the old workflow label while preserving unrelated
labels.

The normal paths are:

```text
unlabeled ───────────────► Project Owner ───────────────► selected next state
needs-grooming ──────────► Project Owner ───────────────► selected next state
needs-decision ──────────► Project Owner ───────────────► selected next state
needs-documentation ────► Doku ────────────────────────► needs-decision
needs-user-guidance ────► human replaces label ────────► actionable state
orchestration-paused ────► human removes label ─────────► normal routing
ready-for-development ──► future implementation flow
```

Doku cannot choose a workflow state. Its result is validated, recorded, and
returned to Project Owner through the fixed `needs-decision` transition. A
specialist question is limited to one focused question and is included in the
durable result comment. When user direction is missing, Project Owner must
return one focused question with `needs-user-guidance`; the service will not
reprocess that issue until a human explicitly replaces the blocking label.

## Durable comments

Each successful state-changing task creates exactly one final result comment
containing the summary, evidence, question (if any), recommended actions, and
transition. The result comment contains an internal Agent Army marker so a
label-update failure can be recovered without running the agent twice.

That final result is the durable public record for the question, decision, and
completion. Start, execution-failure, and retry diagnostics remain local
orchestrator outcomes or concise terminal reports; they are not posted as issue
comments. Agent subprocesses never receive a GitHub App private key,
installation token, credential broker, or GitHub client. Only the explicitly
selected Domain Modeling reference is injected for Project Owner and Doku;
unrelated reference content is not copied into the prompt.

## Failure and retry behavior

- GitHub polling failures are reported and retried after the next interval.
- Agent execution, JSON validation, and comment-publication failures are
  reported locally, leave the current workflow label unchanged, and are retried
  on a later poll without creating diagnostic issue comments.
- If a validated result was commented successfully but its label update failed,
  the next poll uses the durable result marker to retry only the label update.
- Only one state-changing task is active at a time. There is no parallel
  dispatch or state mutation race in this version.
- `needs-user-guidance`, `ready-for-development`, and `orchestration-paused`
  are intentional pauses, not failures. A human must change the label(s) to
  make an issue actionable.

The design leaves room for future read-only specialist analyses that can run in
parallel and report evidence without transitioning state. Such extensions are
outside this issue-level milestone and would require their own workflow
contract.
