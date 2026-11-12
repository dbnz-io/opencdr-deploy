# Fresh-client installation prerequisites and first-run sequence

Use this guide before the [operator walkthrough](operator-walkthrough.md).
It removes assumptions about a maintainer workstation or pre-existing artifact
buckets. Public release availability and installed published-version acceptance
remain P8 gates; these instructions alone do not assert those gates passed.
The historical 12-November candidate kit remains unchanged.

## 1. Declare the installation and evidence

Choose standalone Core or Fleet with its member accounts. Record each account,
region, stage, CLI profile, deployment principal and ordinary operator principal
in a fresh worksheet. AWS authentication and an authorized customer account are
external prerequisites. A profile name is local configuration, not proof of an
account identity. Keep bootstrap administration separate from ordinary API access.

Use a new run directory and an empty Deploy registry, restrictive permissions,
and the release-supported Python/OS combination. Obtain the exact Deploy/MCP
packages, dependency locks and Core/Fleet bundles from the reviewed release
channel. Check manifest hashes against separately obtained expected values;
never replace unavailable assets with `latest` or source-build workarounds.
The historical macOS arm64/CPython3.14 wheelhouse is not a portable Linux/Windows
claim. Keep separate Deploy and MCP virtual environments when their locks differ.

Record installed versions and `pip check` results, source URLs, downloaded asset
hashes and tool paths. Use the pinned release manual and document commit. Do not
copy the maintainer's connection registry, role sessions, old request IDs or
change-set receipts. Follow the walkthrough's fresh install commands only after
these prerequisites and account identity checks pass.

## 2. Bootstrap artifact storage explicitly

For each selected account/region, first use read-only AWS inventory to distinguish
an absent stack/bucket from access denial or network failure. The supported bucket
name is `opencdr-artifacts-ACCOUNT_ID-REGION`; the stack is
`opencdr-artifacts-REGION`. Existing resources must be reconciled, not silently
adopted or deleted. The CLI does not adopt a pre-existing standalone bucket.
A separately reviewed existing customer-owned bucket remains an explicit alternative.

```sh
aws sts get-caller-identity --profile "$PROFILE"
opencdr-deploy release bootstrap --profile "$PROFILE" \
  --account-id "$ACCOUNT" --region "$REGION" > bootstrap-plan.json
```

This creates an unexecuted CloudFormation change set. Record its `change_set_arn`
and independently describe that exact ARN. Require the intended account/region,
CREATE operation and Add-only artifact bucket/policy. Review public-access block,
AES256 encryption, versioning, bucket-owner ownership, TLS-only policy and retained
bucket behavior. This stack contains no engine runtime or customer detections.
After review, execute the same ARN through Deploy:

```sh
opencdr-deploy release execute "$BOOTSTRAP_CHANGE_SET_ARN" \
  --profile "$PROFILE" --account-id "$ACCOUNT" --region "$REGION"
aws cloudformation describe-stacks --profile "$PROFILE" --region "$REGION" \
  --stack-name "opencdr-artifacts-$REGION"
```

Poll every 30 seconds with a bounded 30-minute window. Require CREATE_COMPLETE,
the reviewed change-set identity and the exact bucket output. Independently verify
bucket ownership, region, encryption, public access block, versioning and TLS
policy before giving it to `release plan`. Stop on missing/failed/unknown outcomes;
inspect the same operation rather than creating a second bootstrap blindly.

The deployment principal needs only the privileges required by the reviewed
bootstrap and selected engine templates, plus artifact access and scoped PassRole.
Do not claim administrator-profile invocation proves least privilege. The owner
must provide independently scoped API roles for that negative acceptance scenario.

## 3. Install in the documented mode order

For standalone mode, install Core from the pinned bundle, independently verify
runtime hashes/stack outputs and complete the documented drain, fresh backfill and
reviewed activation. Record historical coverage honestly; an empty scan is not
proof that all historical detections exist.

For a new Fleet installation, plan Fleet paused first, verify its exact identity
and deployed role outputs, then install the member Core stacks with those exact
trusted Fleet roles. Activate each Core and verify its account/region/stage before
registration. Register only intended members and enable collection through the
reviewed workflow after scope and credentials are verified. This avoids guessing
Fleet role ARNs or immediately redeploying Core to add trust. Fleet observations,
detection collection and runtime configuration have distinct freshness/readiness.

A customer adding Fleet to an existing standalone installation follows the
separate reviewed ownership/consumer migration procedure; it is not a fresh
installation shortcut. Do not dual-write, fall back to direct Core after Fleet
failure, or initialize a second consumer over unknown pending receipts.

## 4. Complete sequential acceptance and handoff

Run the release's scenario catalog with explicit modes, roles, fixtures and pass
criteria. Cover source ingestion separately from injected fixtures, identity/scope
negative tests, case/Library revisions and idempotency, guarded reviews/jobs,
delivery/replay, retained evidence and installed portable MCP. Optional external
provider effects require a scoped destination and authorization; inert imported
history is not live provider acceptance.

Capture failures and corrections without rewriting old evidence. Restore temporary
grants/bindings through revision checks and verify cleanup. The operator fills a
new independent worksheet; an agent must not sign it on their behalf.

## 5. Upgrade, restore and uninstall boundaries

Before upgrade, record source/target versions, physical storage, policies, mappings,
consumer checkpoints and current ownership. Review exact replacements/deletions
and retained resources. A submitted change set or successful HTTP request is not
proof of final effect. Unknown writes require receipt/state reconciliation.

For restore, use the guarded storage/ownership recovery manuals and independently
verify metadata/artifact consistency, new-write preservation and released leases.
Never manually remove fences or roll back a fenced failed stack.

For uninstall, first produce a resource/dependency/retention manifest, drain and
fence workers, reconcile provider effects and review the concrete removal plan.
This guide deliberately contains no blanket delete command. The deployed suite
includes retained data that stack deletion may leave behind. Verify both removed
and retained resources and their ongoing cost. P11 will test a separately reviewed
whole-suite teardown and fresh reinstall; shared organization/security/provider
services and immutable evidence are not silently deleted with it.

The hosted control-plane/database/identity setup is supplied by the P10 operator
manual. Keep hosted-operator provisioning separate from the customer's engine
installation and app connection steps. Until that manual and acceptance exist,
this guide establishes an engine procedure only, not whole-suite readiness.
