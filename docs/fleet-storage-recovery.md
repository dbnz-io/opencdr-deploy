# Reattach retained Fleet storage

This procedure covers a deleted Fleet CloudFormation stack whose original
DynamoDB tables and detection evidence bucket still exist. It imports those
physical resources together, then installs compatible runtime code with both
scheduled pollers paused. It does not copy, delete or restore data to an earlier
point in time. TTL and S3 lifecycle expiration continue during recovery.

Implementation status: locally tested source; no published compatible candidate
or live recovery acceptance is claimed. Use a pinned candidate containing this
CLI and Fleet's `detectionEvidenceBucketName` parameter. The existing default tag
is not proof of compatibility. Record the candidate digests before rehearsal.

## Supported storage and prerequisites

- Use the original account, region and stage, a named AWS profile, and the
  original deployment record. A live or failed stack is not deleted automatically.
- Every recovered table must be ACTIVE, on demand, and carry matching original
  CloudFormation stack/logical-ID tags. DetectionTable requires its paired
  DetectionBucket, with the same original stack lineage. Table UUIDs are bound
  into the reviewed plan, so recreating a same-named table invalidates it.
- Supported evidence storage is private, AES256 encrypted, bucket-owner enforced,
  unversioned, unreplicated, with the service's single retention rule. Custom
  bucket policies require a separate plan; the standard TLS-deny policy or no
  policy is supported. Retention must be 7–365 days.
- Custom table KMS keys, replicas, streams, storage classes, throughput caps and
  custom PITR periods need separate review. The plan preserves supported TTL,
  PITR, keys/indexes, encryption and deletion protection. The first runtime
  package must match them; incompatible historical schemas fail before deploy.
  Custom resource tags must also survive the first update because they can
  control access. Only matching service Project/Stage tags can be inherited
  through the runtime stack instead of explicit resource properties.
- The account's operator needs STS identity verification, CloudFormation
  describe/get/import/change-set permissions, DynamoDB describe/tag/TTL/backup
  reads, KMS DescribeKey, and S3 bucket metadata reads (location, tags, encryption,
  ownership, public access, versioning, replication, lifecycle and policy).
  Runtime packaging/deployment uses the usual deployment permissions. These are
  operator permissions, not additional permissions for Fleet pollers.

The inventory verifies configuration and resource identity; it neither scans
detection rows nor lists/reads evidence objects. `data_completeness_verified`
remains false. Existing CloudFormation provenance tags are service-reserved;
arbitrary user labels are not accepted as equivalent proof. See the
[AWS resource tag reference](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-resource-tags.html).
DynamoDB `SSEEnabled: true` selects the AWS-managed KMS key by default, which is
distinct from an AWS-owned key; recovery checks the key metadata rather than
assuming all KMS keys are customer managed. See
[AWS SSESpecification](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-dynamodb-table-ssespecification.html).

## 1. Create and inspect a plan

The following is an example target. Substitute the profile, account, region and
stage from the deployment record. Keep the deployment registry and plan in a
restricted operator directory.

```bash
opencdr-deploy fleet recover security-prod \
  --profile security-admin --security-account-id 735252692417 \
  --region us-east-1 --stage prod --out fleet-recovery.json
```

Expected: a read-only JSON plan and review instructions. `--out` creates a new
file with mode 0600 and refuses to overwrite it. Without `--out`, stdout includes
both JSON and instructions; do not redirect that mixed output as an executable
plan file. No local deployment configuration is replaced at this stage.

Review the target, original stack ARN, every table UUID/ARN, bucket name, exact
import resources/template, retention, protection and `plan_digest`. All actions
must be imports and all resources must have Retain on deletion/replacement.
The plan must contain both detection resources or neither for a legacy deployment.

Bucket discovery uses the original stack's resource record. AWS retains deleted
stack metadata for 90 days; if it is unavailable, supply
`--evidence-bucket ORIGINAL_NAME`. An explicit name still requires matching
reserved lineage tags, account and region. Missing tags or access denial is an
error, never permission to create a replacement bucket. See
[AWS DescribeStackResources](https://docs.aws.amazon.com/cli/latest/reference/cloudformation/describe-stack-resources.html).

## 2. Execute the reviewed import, or resume it

Copy the digest from the reviewed plan into the command; do not modify the file
to force it to match changed live storage.

```bash
opencdr-deploy fleet recover security-prod \
  --profile security-admin --security-account-id 735252692417 \
  --region us-east-1 --stage prod \
  --execute --plan fleet-recovery.json \
  --expected-plan-digest REVIEWED_64_CHARACTER_SHA256
```

The CLI verifies the current account/region and re-reads storage before creating
and executing the import. A deterministic change-set name/client token binds the
operation to this digest. It checks the change-set template, provenance, exact
resource set and import-only actions, then checks imported physical identities
and configuration. This follows AWS's requirement that an import template
describe the resource's current configuration; an import does not establish
data completeness. See the
[AWS import workflow](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/resource-import-existing-stack.html).

If the response is lost, rerun the **same command, file and digest**. An existing
matching import resumes without another execution. `CHANGE_SET_PENDING` or
`IMPORT_IN_PROGRESS` means pending, even if the process exits successfully. Each
wait is bounded to about 45 seconds; read the returned status, not only the exit
code. A failed change set/import produces an error. Inspect its events; the CLI
does not delete a failed stack or retained resources to retry.

`IMPORT_COMPLETE` means resource reattachment and post-import checks succeeded.
The CLI saves the original evidence bucket binding, observed retention and
reviewed storage properties, clears the obsolete API URL/artifact, and disables
both configuration/health and detection polling. Preserve this registry. If it
is lost before runtime deployment, resume the saved plan to reconstruct guards.

Run CloudFormation drift detection and inspect results before installing runtime.
Do not create a fresh recovery plan against the newly imported stack. After a
successful runtime update, the old import plan is no longer a general-purpose
reset command.

## 3. Review the first runtime package

Choose a pinned compatible Fleet candidate. `FLEET_VERSION` below must already
contain its real immutable release tag; no compatible tag is asserted here.

```bash
: "${FLEET_VERSION:?Set the reviewed compatible Fleet release tag}"
opencdr-deploy fleet package security-prod \
  --fleet-version "$FLEET_VERSION" --output ./fleet-recovery-runtime
opencdr-deploy fleet deploy security-prod --artifact ./fleet-recovery-runtime
```

Before the second command, review the package manifest/template and recorded
source commit. Both schedules must be DISABLED. All retained resources must keep
their physical names, Retain policies and reviewed properties. A schema,
retention, encryption or protection mismatch blocks packaging/deployment; choose
compatible source rather than weakening the saved guard. Portable `release plan`
refuses this first runtime update because it lacks the local storage guard.

After runtime deployment, the temporary first-update guard is removed, while
the bucket binding and paused settings persist. Later source upgrades verify
the binding against the live stack, including when local metadata is missing.
Portable upgrades preserve it through `DetectionEvidenceBucketName` and refuse
older artifacts that cannot do so. Keep normal upgrades reviewed: explicit
retention changes after this first update still have their usual effects.

## 4. Validate data and ownership before resuming collection

Do not enable detection polling solely because the import completed. A deleted
and recreated worker role has a different IAM principal ID, even if its ARN is
the same. Existing Core consumers remain owned by the old principal. This flow
does not transfer ownership, reconcile a new feed incarnation or reset a cursor.
Use the separate [reviewed principal handoff](https://github.com/dbnz-io/opencdr/blob/main/docs/detection-handoffs.md)
for a known old/new owner in the same feed incarnation. Deploy's read-only
`fleet worker-identity NAME` resolves the replacement worker's immutable identity.
Feed replacement, unknown historical owner discovery and live role-replacement
acceptance remain gates. Historical gap reconciliation does not itself transfer
ownership or bypass those restrictions.

In a disposable rehearsal, compare the following with an inventory captured
before the outage. If no inventory exists, record the missing evidence rather
than asserting completeness:

| Measurement | Capture and acceptance |
| --- | --- |
| Physical storage | Original/imported table UUIDs, ARNs and bucket; zero replacements. |
| Data continuity | Enumerate all query pages in a fixed retained window; compare unique `(agent_id, detection_id)` sets and both kinds. Account for expected expiry and GSI propagation. |
| Evidence integrity | Fetch representative inline/oversized records through Fleet; independently compare referenced S3 hashes with the pre-outage manifest. No wrong-bucket lookup. |
| Checkpoints/ownership | Compare generation, durable cursor, pending receipt and Core owner identity; zero progress changes caused by import. Changed owners must fail explicitly. |
| Configuration | Retention, TTL, PITR, encryption and paused schedules match the plan. Import completion is separate from readiness to poll. |
| Recovery time/loss | Record outage start, import completion, runtime readiness and eventual healthy collection separately. Report unavailable/expired ranges; reattachment is not a zero-RPO guarantee. |
| Cost | Count inventory/verification calls, retries, elapsed import time and any explicit data-validation reads. No data scans, object copies or per-row S3 reads occur during planning. Capture actual AWS usage separately from local test counts. |

Exercise lost create/execute responses, a pending import, changed table UUID,
wrong-account bucket, missing tags, changed retention, custom KMS/policy, stale
digest and incompatible runtime/portable packages. No rejected path may silently
replace storage or enable polling. Live CloudFormation import/drift, IAM identity,
100-account catch-up and measured AWS costs are still required for P8 acceptance.


After resources are available, [reviewed feed recovery](https://github.com/dbnz-io/opencdr/blob/main/docs/detection-feed-recovery.md)
can reconnect a restored Fleet checkpoint through a fresh consumer, including
when Core's feed or the worker identity changed. Existing Fleet evidence and
metadata remain intact. This complements retained-resource reattachment; neither
procedure performs the physical PITR restore. Keep polling paused until the
separate checkpoint transition and data validation are complete.
