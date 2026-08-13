# Optimization Reviewer agent

The Optimization Reviewer is an independent pull-request review role.
It will assess performance risks, unnecessary abstractions, duplication,
maintainability, testing, and documentation accuracy, then return one explicit
outcome: `approved`, `changes-requested`, or `unable-to-assess`.

The orchestrator now invokes this role on the issue-level
`needs-optimization-review` path and publishes its outcome as a GitHub check
and durable issue result. It does not modify pull requests, integrate CI,
enforce branch protection or merge gates, deploy, manage secrets, or implement
fixes. The role cannot approve its own work.

The same role and App also support the issue-level
`needs-requirements-challenge` path. In that mode it receives the selected
Grilling reference, stress-tests a Project Owner scope draft, publishes one
concise challenge result, and returns the issue to `needs-decision`. It never
selects final scope or advances the issue to development.

The Reviewer App needs repository metadata read access, Pull requests read
access, Issues write access for the durable source-issue result, and Checks
write access for the `Agent Army / Optimization Review` check. Branch protection
is configured separately by a repository administrator.

## Reference

The role uses the vendored Domain Modeling reference at
`references/mattpocock-skills/domain-modeling/SKILL.md` to identify canonical
terminology, domain-boundary drift, and evidence around hard-to-reverse
architectural tradeoffs. Its provenance is recorded in
`references/mattpocock-skills/UPSTREAM.md`; the Agent Army executor injects only
this selected reference into the Codex prompt.

For `requirements_challenge`, the executor also injects the vendored
`references/mattpocock-skills/grilling/SKILL.md`; no unrelated references are
included. The provenance of both files is recorded in `UPSTREAM.md`.

## GitHub App setup

After creating the Optimization Reviewer GitHub App, fill the public `app_id`
and `client_id` fields in `agent-config.yaml`. Store the App private key only in
`pass` at the configured `private_key_secret_ref`; never commit key material or
an installation token.
