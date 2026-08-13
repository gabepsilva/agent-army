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

## Session credentials

`SessionCredentialBroker` is the orchestrator-owned, in-memory cache for secrets.
It retrieves an entry from `pass` only on its first request during a running session,
then clears cached references when that session ends. Agents must receive only
high-level GitHub operations, not the broker or any raw secret.
