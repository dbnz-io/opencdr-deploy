# Native engine ownership transfer

Native Core/Fleet transfer and reverse recovery passed the isolated P7 engine
rehearsal on 2026-11-11. See [the acceptance record](../../opencdr-internal/docs/roadmaps/p7-engine-acceptance-2026-11-11.md). Public release acceptance remains P8.
Use only the reviewed installed Deploy candidate and exact Core/Fleet releases
containing its ownership contract. Application/SaaS migration and hosted consumers
remain P10.

The unit of transfer is the entire case or Library family of one engine. An initial
destination must have no native rows in that family. Existing grants, connectors,
review policies, execution claims, routing activation and jobs are not imported as
authority. Core account execution remains at Core. Archives retain those records
as inert history; account workers can continue while the case family is fenced.
Populated-destination merge is unsupported and must not be simulated by deleting
existing rows. This is an explicit limitation of the accepted native engine workflow.

Before a transfer, establish the destination's normal independently reviewed
registration and access. Inventory every portable CLI/MCP/runner connection that
uses the family; stop its mutation loop and record its destination connection.
Hosted application consumers are outside this engine-only workflow. Do not claim
consumer cutover from an ownership marker alone.

Plan with exact accounts, regions, installed releases and an explicit JSON agent
map. A Library map is `{}`. A case map lists every source scope exactly once and
maps it to a unique valid destination scope; it never creates a grant.

```sh
opencdr-deploy ownership plan \
  --source-profile "$SOURCE_PROFILE" --source-account-id "$SOURCE_ACCOUNT" \
  --source-region "$REGION" --source-stack "$SOURCE_STACK" \
  --source-release "$SOURCE_RELEASE" --source-release-sha256 "$SOURCE_RELEASE_SHA" \
  --target-profile "$TARGET_PROFILE" --target-account-id "$TARGET_ACCOUNT" \
  --target-region "$REGION" --target-stack "$TARGET_STACK" \
  --target-release "$TARGET_RELEASE" --target-release-sha256 "$TARGET_RELEASE_SHA" \
  --family cases --agent-map agent-map.json --retention-days 30 \
  --reason "Reviewed isolated engine ownership transfer" --output transfer-plan.json
```

The command prints the SHA256 of canonical plan bytes. Independently review the
physical table IDs, stack/change-set identities, runtime contract and full function
hashes, source/destination epochs, map, empty destination and retention window.
No AWS write occurs during planning. Preserve the printed digest as `PLAN_SHA`.
The following commands require both profiles and the exact reviewed plan:

```sh
opencdr-deploy ownership freeze \
  --source-profile "$SOURCE_PROFILE" --target-profile "$TARGET_PROFILE" \
  --plan transfer-plan.json --sha256 "$PLAN_SHA" --output freeze.json
opencdr-deploy ownership capture \
  --source-profile "$SOURCE_PROFILE" --target-profile "$TARGET_PROFILE" \
  --plan transfer-plan.json --sha256 "$PLAN_SHA" \
  --directory final-delta --output capture.json
```

Freeze uses the existing Deploy recovery lease in each account and atomic family
ownership rows. It does not manually stop/reset detection consumers or change Core
feed activation. Runtime transactions that cross the fence fail atomically; prior
committed data remains readable. Source/destination physical identity and leases
are rechecked during controller operations. Old code that ignores fences is refused
by the runtime pin; later guarded releases cannot remove the ownership contract.
Legacy source-driven deployment routes refuse engines with operational storage.

Review `final-delta/manifest.json` and its printed digest (`MANIFEST_SHA`) separately:
counts, all relationships, evidence bytes, immutable history, active-link constraints,
identity mapping, inert authority/job records and zero unresolved findings. The
final family scan must match the pinned archive behind its fence. The archive is
retained in the source artifact bucket before import. During family-only capture,
nonmoving account job/policy history is observational, not a frozen execution state.

```sh
opencdr-deploy ownership stage \
  --source-profile "$SOURCE_PROFILE" --target-profile "$TARGET_PROFILE" \
  --plan transfer-plan.json --sha256 "$PLAN_SHA" \
  --directory final-delta --manifest-sha256 "$MANIFEST_SHA" --output stage.json
opencdr-deploy ownership activate \
  --source-profile "$SOURCE_PROFILE" --target-profile "$TARGET_PROFILE" \
  --plan transfer-plan.json --sha256 "$PLAN_SHA" \
  --directory final-delta --manifest-sha256 "$MANIFEST_SHA" --output activate.json
opencdr-deploy ownership complete \
  --source-profile "$SOURCE_PROFILE" --target-profile "$TARGET_PROFILE" \
  --plan transfer-plan.json --sha256 "$PLAN_SHA" --output complete.json
```

Staging uses immutable object puts and conditional row transactions. Retries compare
existing bytes; they never silently overwrite a conflicting target. Activation
verifies imported rows/objects, atomically retires the source with its durable
journal, then activates the destination. An uncertain target activation leaves the
source retired. Re-run the same command with a fresh output filename; do not
unfreeze the source. Completion independently checks final ownership and pinned
runtimes, retains receipts, then releases only that plan's leases. All source data
and migration archives remain retained. The retention date is a minimum recovery
commitment; this workflow performs no automatic deletion or fence expiry.

Switch the inventoried portable connections to the verified destination, check their
scope and actor identity, read retained cases/evidence/history and Library artifacts,
make a scoped destination write, and verify old-source writes fail. Record each
consumer result. Configuration/provider activation needs its own owner review;
importing historical authority never enables it.

Before staging begins, `ownership cancel` restores active source/destination epochs
(or the prior retired destination for a cancelled reverse transfer). It increments
epochs so late freeze attempts cannot win. Follow cancellation with `complete` to
release the verified leases. Once staging begins, finish the transfer and use the
reviewed reverse path. Never delete a lease or write an ownership row manually.
A failed first lease acquisition can be cancelled without touching operational
ownership: the controller creates a terminal cancellation receipt before releasing
its own partial lease. Other controllers' leases are left intact.

For reverse recovery, first verify the prior `complete` receipt and retain all new
destination writes. Run `ownership plan` with source/target swapped, an inverse
agent map, and `--previous-plan transfer-plan.json --previous-sha256 "$PLAN_SHA"`.
The planner requires the actual durable prior completion and unchanged retained
original data. Follow the same freeze/capture/review/stage/activate/complete sequence.
The reverse archive must retain immutable history and nondecreasing case revisions/
Library versions. Legitimately removed active-case links are removed conditionally;
new post-cutover writes and actors are preserved. This is not restoration of the
old snapshot. Switch and verify the portable consumers again.

After a local machine interruption, recover pinned files without AWS mutations:

```sh
opencdr-deploy ownership retrieve --profile "$SOURCE_PROFILE" \
  --account-id "$SOURCE_ACCOUNT" --region "$REGION" \
  --artifact-bucket "$SOURCE_ARTIFACT_BUCKET" --sha256 "$PLAN_SHA" \
  --manifest-sha256 "$MANIFEST_SHA" --directory recovered-transfer --output retrieve.json
```

The source bucket retains final-delta archives; both owners retain the plan and
completion receipt. Retrieval enforces digest, safe chunk paths and size bounds.
Use `ownership status` with the original plan/profiles for durable reconciliation.
Commands fail promptly on AWS timeout or a changed pin/lease; they do not poll
indefinitely, retry a different plan, or automatically resume an unknown effect.
Preserve failure output and use a new output file for each reconciliation attempt.
