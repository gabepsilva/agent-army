# Agent Army

## Verify the documentation agent

Create the project environment and run the read-only verifier with UV:

```bash
uv sync
uv run verify-documentation-agent
```

The verifier loads the documentation agent's role and configuration, retrieves its GitHub App private key from `pass`, creates a short-lived installation token, and lists the repositories available to the App. It does not write to GitHub or print credentials.

## Read a documentation task

Fetch a bounded, read-only context bundle for an issue or pull request:

```bash
uv run read-documentation-task https://github.com/OWNER/REPOSITORY/issues/NUMBER
```

Replace `issues` with `pull` for a pull-request URL. The command emits structured JSON containing the issue, discussion, and—when applicable—the pull request's changed files, patches, reviews, and documentation signals. It makes no GitHub writes.

## Read a shared work item

All agents can reuse the normalized issue and pull-request reader:

```bash
uv run read-work-item https://github.com/OWNER/REPOSITORY/issues/NUMBER
```

`read-documentation-task` builds on this shared reader and adds only Doku-specific documentation signals.

## Execute a role with a coding-agent CLI

First save a shared work item to a file, then run the role in an isolated Git
workspace:

```bash
uv run read-work-item https://github.com/OWNER/REPOSITORY/issues/NUMBER > work-item.json
uv run execute-agent \
  --role agents/documentation/ROLE.md \
  --workspace /path/to/target-repository \
  --work-item work-item.json
```

The executor does not pass GitHub App credentials, GitHub tokens, or provider
API keys into the agent; both CLIs authenticate from their own credential store,
and GitHub operations remain in the Python orchestrator. It also redirects
`gh`'s and git's own credential lookups (`GH_CONFIG_DIR`, `GIT_CONFIG_GLOBAL`,
`GIT_CONFIG_NOSYSTEM`, `GIT_SSH_COMMAND`, `SSH_AUTH_SOCK`) to an empty,
per-invocation directory, so a role card with shell access can't authenticate
to GitHub as the operator via their own ambient `gh`/git session — the only
path to GitHub stays the orchestrator's App-token-authenticated client. This
is shared, backend-independent code in `CliAgentExecutor`, so it applies to
every coding-agent CLI the same way, including ones not yet added. Agent
subprocess progress is captured quietly; only the final JSON result is used by
the workflow. If the role directory contains
the supported Domain Modeling reference, its selected skill text is embedded
in the prompt explicitly; unrelated reference files are not included.

## Choose the backend

`config.yaml` in the repository root selects which coding-agent CLI executes
role cards:

```yaml
backend: codex   # codex | claude
```

Both backends are held to the same JSON Schemas in `schemas/`, so the
orchestrator's validators and publishers cannot tell them apart. Workflow state
lives in GitHub labels and comments rather than in the executor, so an issue can
move between backends mid-workflow without losing anything.

Each agent can override that default in its own `agents/<role>/agent-config.yaml`
under a `runtime:` section, so one role can run on Claude while the rest stay on
Codex:

```yaml
runtime:
  backend: claude
  claude:
    permission_mode: bypassPermissions
    model: claude-opus-5
```

Settings layer most-specific-first:

```
--backend flag  >  agents/<role>/agent-config.yaml  >  config.yaml  >  built-in defaults
```

Every key in `runtime:` is optional and inherits what it does not set, so an
agent that only pins a model keeps the repository-wide backend and permission
mode. An agent with no `runtime:` section inherits everything.

`--backend` overrides the file for one run and `--config` points at a different
root configuration file:

```bash
uv run execute-agent --backend claude ...
uv run run-orchestrator --backend codex ...     # forces every agent to Codex
```

Codex is the default, and a missing `config.yaml` behaves exactly as the project
did before backends were selectable.

### Reported cost

`AgentExecutionResult.cost_usd` carries what a run reported. Claude returns real
dollars from its result envelope; Codex reports token counts rather than
dollars, and only on a JSONL event stream that would displace the structured
result on stdout, so it reports `0.0`. Treat the number as a lower bound on real
spend, not a total. `execute-agent` prints it to stderr, and the orchestrator
exposes a running `total_cost_usd`.

The two differ in confinement: Codex runs under an OS-level `workspace-write`
sandbox, while Claude Code has no equivalent and relies on
`backends.claude.permission_mode` instead. This project deliberately sets that
to `bypassPermissions`, accepting an unconfined Claude in exchange for
capability parity with Codex. Orchestrated runs execute in isolated git
worktrees; a direct `execute-agent --workspace .` does not, so point it at a
workspace you are willing to lose.

## Try it without GitHub

`examples/work-item.example.json` is a hand-written work item, so a role can be
run end to end without GitHub App credentials:

```bash
uv run execute-agent \
  --role agents/documentation/ROLE.md \
  --workspace . \
  --work-item examples/work-item.example.json \
  --backend claude
```

Swap `--backend codex` to compare the two on identical input.

## Publish a Doku issue analysis

After reviewing Doku's JSON result, publish its concise Markdown report to the
same issue:

```bash
uv run publish-documentation-analysis \
  https://github.com/OWNER/REPOSITORY/issues/NUMBER \
  --analysis doku-analysis.json
```

This is the first GitHub write action for the standalone Doku command. It
accepts issue URLs only, validates the expected Doku result shape, and posts one
issue comment. Pull-request creation is owned by the issue orchestrator below,
not by this standalone command.

## Run Doku end-to-end

The workflow command keeps Doku's structured result in memory, then publishes
the rendered analysis to the originating issue:

```bash
uv run run-documentation-agent \
  https://github.com/OWNER/REPOSITORY/issues/NUMBER \
  --workspace /path/to/target-repository
```

It reads the issue, runs Doku through Codex CLI in the supplied workspace, and
posts one issue comment using Doku's GitHub App. It does not write the analysis
JSON to disk or create a pull request.

## Run the issue-driven orchestrator

The polling service processes one state-changing issue task at a time:

```bash
uv run run-orchestrator \
  --repository OWNER/REPOSITORY \
  --workspace /path/to/target-repository
```

Use `--once` for one polling pass. The supported labels, ownership model,
unlabeled intake path, `orchestration-paused` guard, user-guidance pause,
requirements-challenge path and round limit, Developer pull-request path,
Optimization Reviewer check outcomes, transitions, one-result-comment behavior,
and retry behavior are documented in
[ORCHESTRATOR.md](ORCHESTRATOR.md). A human can add `orchestration-paused` to
make the orchestrator ignore an issue completely until that label is removed.

## Configured workflow roles

The [Developer role](agents/developer/README.md) and [Optimization Reviewer
role](agents/optimization-reviewer/README.md) are wired into the issue-to-
pull-request path, including the issue-level requirements challenge mode.
Branch protection and merge enforcement remain manual repository settings; the
orchestrator does not change them.

## Choose where credentials come from

`pass` keeps the GitHub App private keys encrypted at rest, but decrypting them
needs an interactive GPG unlock. gpg-agent's cache expires and a headless
process has no TTY for pinentry, so an unattended orchestrator eventually
stalls on `pass show`. Set `credentials.source` in `config.yaml` to `env` to
read them from the environment or a dotenv file instead:

```bash
cp .env.example .env && chmod 600 .env   # then fill in the paths
```

Each key's variable name is derived from its secret ref, so nothing needs to be
kept in sync: `agent-army/project-owner/github-app-private-key` becomes
`AGENT_ARMY_PROJECT_OWNER_GITHUB_APP_PRIVATE_KEY`. A value may be the PEM
itself (newlines written as `\n` are restored) or a path to the `.pem` file.

Values read from the dotenv file are kept in the orchestrator's own memory and
are never exported to `os.environ`, so they are not inherited by the agent CLI
subprocesses, which must never receive credentials.

This is a real tradeoff, not a free upgrade: `env` gives up encryption at rest,
making the keys readable by anything running as this user. `.env` is
gitignored; keep it that way and restrict it with `chmod 600`.

## Session credentials

`SessionCredentialBroker` is the orchestrator-owned, in-memory cache for secrets.
It retrieves an entry from `pass` only on its first request during a running session,
then clears cached references when that session ends. Agents must receive only
high-level GitHub operations, not the broker or any raw secret.
