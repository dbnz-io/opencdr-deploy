# Owner automation runners and notification initialization

`workflow` uses authenticated owner reviews and fixed request IDs. Keep request files unchanged after an uncertain response and inspect the owner receipt. Direct policy and grant administration requires an IAM administrator. Web and MCP use the same owner revisions and policy gates.

## Portable routing runner

After a reviewed routing activation, schedule the installed Deploy client on a customer-controlled runner:

```sh
opencdr-deploy routing-runner --connection core.json --mode core \
  --approval-id REVIEW_ID --revision 2 --agent ACCOUNT:REGION:STAGE \
  --state routing.sqlite --max-batches 4 --reason 'Reviewed routing service'
```

For Fleet use `--mode fleet` and its exact registered agent ID. The SQLite file is durable state, not disposable cache. A request is committed before transport; an uncertain response retains it for reconciliation on the next invocation. Concurrent local invocations fail on the lock. Owner receipts prevent duplicate effects if a process dies after the response. Do not copy the database to another concurrently active runner or delete it to bypass an unknown result. Each invocation bounds pages; scheduling and credential refresh belong to the customer runner. Revoked or expired owner authority stops work. Renew authority through owner reviews; never extend a review by changing local state.

## New notification subscription

The hosted worker's subscription name is exactly `organization-id:deployment-id`. Initialize it with the same service IAM principal and connection that will consume it. These commands do not enable delivery, change a schedule or retire a webhook.

Capture an explicit source checkpoint and notification cutover time, and preserve the review explaining what historical range is excluded. A retention gap is not proof of absence. Existing consumer receipts must be reconciled separately; this command never resets them.

```sh
opencdr-deploy subscription plan --connection service.json --mode core \
  --subscription ORGANIZATION_ID:DEPLOYMENT_ID --initial-cursor REVIEWED_CURSOR \
  --not-before TIMESTAMP_WITH_TIMEZONE --reason 'Reviewed initial notification cutover' \
  --plan subscription-plan.json
# Independently review the exact plan, principal, source, cursor and excluded history.
opencdr-deploy subscription apply --connection service.json --mode core \
  --plan subscription-plan.json --sha256 EXACT_REVIEWED_FILE_SHA256
```

Fleet adds `--agent REGISTERED_AGENT` to both commands. Its existing scoped service grant is required; registration identity/revision is rechecked at application. Core enforces checkpoint generation and create-only registration. Plans expire after 30 minutes. A lost registration response is reconciled using the unchanged plan and digest; an existing different checkpoint is rejected. Preserve plan/receipt evidence. Only after live receipt/outbox verification and reviewed cutover should an operator enable durable delivery with the matching cutover time. The legacy delivery path remains the default until that separate action.

Canonical case indicators and retained provider results are accessible through `workflow indicators`, `provider-history`, `curate-indicator` and `import-provider-history`. Imported results are explicitly unverified history; these operations never execute providers, page responders or authorize payment.
