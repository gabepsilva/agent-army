# Developer agent

The Developer role is a credential-ready foundation for implementing an
approved `ready-for-development` issue in an isolated workspace. It follows
repository conventions, adds focused tests, and reports implementation evidence.

The role is now wired for the `ready-for-development` path. The orchestrator
creates the isolated worktree, owns the branch commit/push and pull request, and
keeps GitHub credentials out of Codex. The role itself does not merge, deploy,
manage branches or secrets, or make product decisions beyond documented
acceptance criteria.

The Developer App needs repository metadata read access, Issues write access for
the final source-issue result, and Pull requests write access to create the PR.
The branch push uses the orchestrator's configured Git remote credentials; it
does not pass an App token to Codex.

## Reference

The role uses the vendored Domain Modeling reference at
`references/mattpocock-skills/domain-modeling/SKILL.md` to preserve canonical
terminology and recognize domain-boundary ambiguity during implementation. Its
provenance is recorded in `references/mattpocock-skills/UPSTREAM.md`; the Agent
Army executor injects only this selected reference into the Codex prompt.

## GitHub App setup

After creating the Developer GitHub App, fill the public `app_id` and
`client_id` fields in `agent-config.yaml`. Store the App private key only in
`pass` at the configured `private_key_secret_ref`; never commit key material or
an installation token.
