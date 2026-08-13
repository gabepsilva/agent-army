# Project Owner agent

This agent owns issue-level product direction and routine, reversible decisions.
It records its decisions in GitHub so specialist agents can act from the same
durable source of truth.

Before running it, create its GitHub App and replace the `null` values in
`agent-config.yaml`. Store the App private key in `pass` and reference that
entry through `private_key_secret_ref`.

## Domain modeling reference

The Project Owner may consult the vendored
`references/mattpocock-skills/domain-modeling/SKILL.md` when resolving ambiguous
terminology, maintaining shared vocabulary, or considering a genuine
hard-to-reverse architectural or product tradeoff. Its provenance is recorded
in `references/mattpocock-skills/UPSTREAM.md`; it is supporting guidance, not
executable authority. The Agent Army executor embeds this selected reference in
the Project Owner Codex prompt, so it remains available when Codex runs in a
separate target-repository workspace.

Ask only for genuinely necessary information, using the smallest focused set of
questions. Prefer reversible, evidence-based decisions and avoid unnecessary
questions.
