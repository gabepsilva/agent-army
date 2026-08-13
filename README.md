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

## Execute a role with Codex CLI

First save a shared work item to a file, then run the role in an isolated Git
workspace:

```bash
uv run read-work-item https://github.com/OWNER/REPOSITORY/issues/NUMBER > work-item.json
uv run execute-agent \
  --role agents/documentation/ROLE.md \
  --workspace /path/to/target-repository \
  --work-item work-item.json
```

The executor runs `codex exec` with a workspace-write sandbox. It does not pass
GitHub App credentials or GitHub tokens into Codex; GitHub operations remain in
the Python orchestrator. Codex subprocess progress is captured quietly; only
the final JSON result is used by the workflow. If the role directory contains
the supported Domain Modeling reference, its selected skill text is embedded
in the prompt explicitly; unrelated reference files are not included.

## Publish a Doku issue analysis

After reviewing Doku's JSON result, publish its concise Markdown report to the
same issue:

```bash
uv run publish-documentation-analysis \
  https://github.com/OWNER/REPOSITORY/issues/NUMBER \
  --analysis doku-analysis.json
```

This is the first GitHub write action. It accepts issue URLs only, validates the
expected Doku result shape, and posts one issue comment. Pull-request comments
and pull-request creation are not enabled yet.

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
transitions, one-result-comment behavior, and retry behavior are documented in
[ORCHESTRATOR.md](ORCHESTRATOR.md). A human can add `orchestration-paused` to
make the orchestrator ignore an issue completely until that label is removed.

## Session credentials

`SessionCredentialBroker` is the orchestrator-owned, in-memory cache for secrets.
It retrieves an entry from `pass` only on its first request during a running session,
then clears cached references when that session ends. Agents must receive only
high-level GitHub operations, not the broker or any raw secret.
