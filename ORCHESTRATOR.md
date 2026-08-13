# Issue-driven orchestrator

`run-orchestrator` is a long-running, single-worker polling service. It polls
one configured repository for open issues, selects the lowest-numbered eligible
issue, and performs at most one state-changing agent task per polling pass.
Webhooks, deployment, merge execution, and branch-protection configuration are
outside this version. Developer pull-request creation and Optimization Reviewer
checks are included in the issue-level flow below.

Start the service with:

```bash
uv run run-orchestrator \
  --repository OWNER/REPOSITORY \
  --workspace /path/to/target-repository
```

Use `--once` for a single poll while developing or testing. The workspace is
passed to Codex with workspace-write permissions, but GitHub credentials remain
inside the Python process and are removed from the Codex subprocess environment.
The orchestrator explicitly embeds the selected role references in the relevant
role prompt, so the role can use them even when the target workspace does not
contain Agent Army's reference files. Requirements challenges receive Domain
Modeling and Grilling; ordinary PR reviews receive only Domain Modeling.

## Supported workflow labels

The following labels are the complete workflow state vocabulary:

| Label | Meaning | Polling behavior |
| --- | --- | --- |
| `needs-grooming` | Project Owner should route and clarify the issue. | Dispatch Project Owner. |
| `needs-decision` | Project Owner must review evidence or answer a specialist question. | Dispatch Project Owner. |
| `needs-documentation` | Documentation analysis is required. | Dispatch Doku. |
| `ready-for-development` | Product direction is approved for implementation. | Dispatch Developer. |
| `needs-requirements-challenge` | The Project Owner's scope draft needs bounded independent stress testing. | Dispatch Optimization Reviewer in `requirements_challenge` mode. |
| `needs-optimization-review` | A Developer pull request needs an independent review. | Dispatch Optimization Reviewer. |
| `ready-for-merge` | The exact pull-request head passed Optimization Review. | Leave untouched; merge remains a human/branch-protection concern. |
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
needs-documentation ───────► Doku ────────────────────────► needs-decision
needs-requirements-challenge ► Optimization Reviewer ───► needs-decision
ready-for-development ─────► Developer ───────────────────► needs-optimization-review
needs-optimization-review ─► Optimization Reviewer ──────► outcome-mapped state
needs-user-guidance ────────► human replaces label ───────► actionable state
orchestration-paused ───────► human removes label ────────► normal routing
ready-for-merge ────────────► human/branch protection ────► merge
```

Doku cannot choose a workflow state. Its result is validated, recorded, and
returned to Project Owner through the fixed `needs-decision` transition. A
specialist question is limited to one focused question and is included in the
durable result comment. When user direction is missing, Project Owner must
return one focused question with `needs-user-guidance`; the service will not
reprocess that issue until a human explicitly replaces the blocking label.

Project Owner may include the initial scope, assumptions, scenarios, and
acceptance-criteria draft in its durable result and route to
`needs-requirements-challenge`. The shared Optimization Reviewer then uses its
`requirements_challenge` mode to stress-test that draft and posts exactly one
concise issue result before returning control to Project Owner through
`needs-decision`. It never selects final scope or routes directly to development.

Challenge dialogue is bounded by durable result markers. The first challenge is
round 1. Project Owner may request round 2 only when its revision materially
changes scope and records that fact in its validated result. After round 2,
Project Owner must resolve the findings or use `needs-user-guidance`; the
orchestrator will not start a third challenge. A successful challenge invocation
creates one final issue comment, and a label-update failure recovers from its
marker without rerunning the Reviewer.

For `ready-for-development`, Developer runs in a temporary Git worktree. The
orchestrator owns the branch commit, push, and pull-request creation, links the
pull request to the source issue, and advances the issue only after the
Developer JSON result and pull request are valid. Developer cannot merge,
deploy, manage secrets, or select product scope.

For `needs-optimization-review`, the orchestrator reads the current pull-request
head, gives Optimization Reviewer a detached worktree at that exact commit,
and validates its explicit outcome. The outcomes map deterministically:

- `approved` → `ready-for-merge`
- `changes-requested` → `ready-for-development`
- `unable-to-assess` → `needs-decision`

Every review is tied to the exact head SHA. A later commit has no matching
review marker and therefore requires a fresh review. Optimization Reviewer is a
separate role and does not modify the pull request.

## Convergence between Developer and Reviewer

A review finding is a claim, not an order. Both roles are pointed at the same
goal — converge on a change that makes the project succeed, or on an
evidence-based recommendation not to build it — and the orchestrator enforces
that neither side may disengage:

- Reviewer findings are structured: a stable `id`, a severity of `blocking`,
  `should-fix`, or `nit`, a claim, and evidence. Only `blocking` gates the
  outcome, so a nit no longer costs a full revise cycle.
- Developer must accept or dispute **every** blocking finding. A validated
  result that silently omits one is rejected and retried.
- Reviewer must then concede or hold **every** dispute. Conceding drops the
  finding; holding keeps it and requires counter-evidence.
- Blocking findings and disputes — the two moves that cost the other side real
  work — must cite something re-checkable: a `path/file.py:120`, a
  `` `command` ``, or a URL. This is a deliberately lenient anchor check; it
  cannot tell a good argument from a bad one, only an anchored one from
  "typically you'd want…".

Rounds are not capped: an argument runs until it converges. After
`MAX_CONVERGENCE_ROUNDS` (7) the issue moves to `needs-user-guidance` with the
contested blocking findings attached, rather than spending further agent runs
on an argument that is not converging.

The structured state of the argument is embedded in each durable comment as an
`agent-army:payload` block, so a restarted orchestrator resumes the argument
where it left off instead of starting a fresh one.

## Durable comments

Each successful state-changing task creates exactly one final result comment
containing the summary, evidence, question (if any), recommended actions, and
transition. The result comment contains an internal Agent Army marker so a
label-update failure can be recovered without running the agent twice.

That final result is the durable public record for the question, decision, and
completion. Developer pull requests are linked from that result. Optimization
Reviewer publishes the same explicit outcome as an issue result and as the
`Agent Army / Optimization Review` completed check on the reviewed commit.
Start, execution-failure, and retry diagnostics remain local orchestrator
outcomes or concise terminal reports; they are not posted as issue comments.
Agent subprocesses never receive a GitHub App private key,
installation token, credential broker, or GitHub client. They also run with
`gh`'s and git's own credential lookups redirected to an empty, per-invocation
directory, so a role card with shell access has no ambient `gh`/git session to
fall back on either — the App-authenticated orchestrator client is the only
path to GitHub. This isolation lives in the shared executor, not in any one
backend's own sandbox, so it holds regardless of which coding-agent CLI runs
the role card. Only the explicitly
selected Domain Modeling reference is injected for each configured role;
unrelated reference content is not copied into the prompt.

## Failure and retry behavior

- GitHub polling failures are reported and retried after the next interval.
- Agent execution, JSON validation, and comment-publication failures are
  reported locally, leave the current workflow label unchanged, and are retried
  on a later poll without creating diagnostic issue comments.
- If a validated result was commented successfully but its label update failed,
  the next poll uses the durable result marker to retry only the label update.
- A Developer pull request is not recreated when its deterministic branch already
  has an open pull request; the existing pull request is recovered and routed.
- A review check/result for an older commit never satisfies a newer pull-request
  head.
- Only one state-changing task is active at a time. There is no parallel
  dispatch or state mutation race in this version.
- `needs-user-guidance`, `ready-for-merge`, and `orchestration-paused` are
  intentional pauses, not failures. A human must change the label(s) to make
  an issue actionable.

The design leaves room for future read-only specialist analyses that can run in
parallel and report evidence without transitioning state. Such extensions are
outside this issue-level milestone and would require their own workflow
contract.

## Branch protection configuration

The orchestrator creates the `Agent Army / Optimization Review` check, but it
does not change repository branch protection. To make review a required merge
gate, a repository administrator must open **Settings → Branches → Branch
protection rules** (or the repository rulesets UI), select the protected base
branch, enable required status checks, add exactly `Agent Army / Optimization
Review`, and save the rule. Until that manual setting is applied, the check is
informational and `ready-for-merge` is an orchestrator label, not an enforced
GitHub merge restriction.
