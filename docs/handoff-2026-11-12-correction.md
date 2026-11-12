# Candidate handoff correction — 12 November 2026

Read this with the [dated handoff guide](handoff-2026-11-12.md). The original kit,
archive and evidence remain immutable and retain their original hashes.

The kit's `compatibility.json` mistakenly records the sibling rules checkout
commit `03544f6d7a2be17ab0ed393714a451768aac6e58`. The Core source used by promotion pins
`f65fc69a437b9de00f0a7528a5a9c05492e9b922` in `support_files/detection_rules`.
Use the latter as the **promotion catalog source pin**. This is a provenance
correction, not a statement that this catalog is installed or activated in AWS.
Core/Fleet runtime manifests and Deploy/MCP wheel hashes are unchanged.

The corrected catalog's 30 rules and seven reusable lists passed validation using
schema code imported directly from the exact candidate Core runtime ZIP. The older
catalog also validates (30 rules, no reusable lists); schema acceptance does not
prove native evaluator results or response behavior. No catalog was loaded, no
response was armed and no cloud write was performed for this correction.

The operator evidence retains the archived catalog
`detection-rules-f65fc69a437b9de00f0a7528a5a9c05492e9b922.tar`;
SHA256 `7373c7feb85775c7896fa68ee47d5526aa65357af92ede02b4f845651e58b502`.
`rules-provenance.json` alongside it records the parent commit, both catalog file
inventories and the exact validation runtime hash. Derive future release pins from
Core's committed gitlink, not from the independent sibling checkout.

For a real new customer, use [fresh-client prerequisites](fresh-client-installation.md)
before the walkthrough. The historical kit's existing account profiles and artifact
buckets are explicit maintainer fixtures, not universal customer prerequisites.

AWS SSO was refreshed and both account identities verified. A fresh read-only
reconciliation confirmed all18Core runtimes, active recovered ownership and retained
fixtures; Fleet verification compute remains retired (15account functions).
The current Core IAM routes rejected unsigned, expired, wrong-region/service and
body-tampered requests. Restricted-principal authorization and full Fleet acceptance
remain separate checks. Public publication and independent owner acceptance remain open.
