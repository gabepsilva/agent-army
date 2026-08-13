# Project Owner agent

This agent owns issue-level product direction and routine, reversible decisions.
It records its decisions in GitHub so specialist agents can act from the same
durable source of truth.

Before running it, create its GitHub App and replace the `null` values in
`agent-config.yaml`. Store the App private key in `pass` and reference that
entry through `private_key_secret_ref`.
