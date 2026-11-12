# Owner reviews and jobs

Use `opencdr-deploy workflow --connection CONNECTION.json --mode core|fleet ACTION`.
The connection is verified against its exact deployment. Mutation responses must match the
owner, review, request ID and revision. Preserve an unchanged JSON request after an unknown
outcome; read the receipt before retrying. No workflow automatically retries writes.

Read `inbox`, `review --id ID`, `receipt --id ID`, `verify --id ID`, `flags` or `ownership`.
Fleet also supports `groups`, `group --id ID`, `resolve-group --id ID --file REQUEST.json`
and frozen distribution plans/jobs. Routing `drain --id REVIEW --file REQUEST.json` consumes
at most 20 owner records and advances only after durable case outcomes. It is a customer-runner
wake-up, not an installed scheduler.

Policy and grants require a separate direct IAM connection with `access: administrator`
and no `identity` delegation. `policy` and `grant --subject STABLE_PRINCIPAL` read current state.
`configure-policy --file REQUEST.json` and `configure-grant --subject STABLE_PRINCIPAL
--file REQUEST.json` use PUT with exact fields `policy` or `grant`, `expected_rev`, `request_id`,
and `reason`. Revision zero is permitted only for initial configuration. The owner validates
all policy permissions, target scopes, self-review exceptions and emergency actors. Never
copy a grant from another installation or infer authority from a web role.

A policy body declares exact reviewers, `allow_self_if_sole`, `emergency_actors`,
`max_lifetime_seconds` and supported `operations`. A grant declares `enabled`, `expires_at`,
`permissions`, `operations` and fixed `target_ids`. Use the owner's current revision; a
policy/grant change invalidates previously bound authority. Review this document against the
published API schema before administering an installation.

Infrastructure flags are inspected with `release flags DIRECTORY --sha256 SHA256` and applied
only through reviewed Deploy plans. Web connection pause and Fleet ingestion pause do not
change runtime detection flags.
