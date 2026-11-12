# Reviewed activation of Core detection reads

This candidate adds a deployment barrier for the first durable detection index
and later guarded upgrades. It does not perform a physical restore, prove complete
history, or replace P1's live acceptance. Use compatible Core/Deploy packages.

Core's indexed list, summary, detail, feed and receipt progress fail with HTTP 503
`detection_activation_required` until the current runtime has a reviewed activation.
Native signal/correlation commits and retained-source backfill continue. Existing
SaaS records and retained cases are not migrated by this operation. Fleet polling
should remain disabled until activation and consumer recovery have been reviewed.

Activation is tied to the exact runtime ZIP hash, feed TableId and epoch. A new
runtime or physical restore cannot inherit authorization to expose the old index.
The retained-source job records both source and feed TableIds. Rotating the feed
revokes activation. An empty feed must be initialized by a backfill before activation.

## First install or upgrade from a Core without a feed

1. Export/install a pinned release with `opencdr-deploy release export`, `plan` and
   `execute`. The exporter binds the runtime digest into Lambda configuration.
   Source-mode `deploy` refuses a Core package containing detection storage;
   an unexported Serverless package deliberately cannot activate reads.
2. Wait for CloudFormation and all runtime Lambdas to finish updating. Keep normal
   detection processing running. Do not set reserved concurrency to zero or
   disable ingress as a shortcut.
3. **At least 16 minutes after the latest stack/Lambda change**, create a new
   retained-source backfill through the IAM administrator API and step it to
   completion. This covers the maximum lifetime of a previously running Lambda
   plus one minute. This applies to standard Lambda event functions; managed
   instances and durable functions are rejected because they need a different
   draining protocol. See the [AWS timeout reference](https://docs.aws.amazon.com/lambda/latest/dg/configuration-timeout.html).
   A job started earlier is rejected even if it finished later.
4. Inspect all backfill results, including expired, invalid and conflicting rows.
   Completion means the retained scan finished, not that all historical detections
   survived. Resolve findings or record the accepted limitations in the reason.
5. Prepare and inspect a read activation plan. This command is read-only in AWS:

```bash
opencdr-deploy release detection-activation plan opencdr-dev \
  --action activate --directory ./core-release \
  --release-sha256 RELEASE_MANIFEST_SHA256 --backfill-id upgrade-history-01 \
  --reason 'Retained scan reviewed; documented unavailable history accepted' \
  --output activation.json --profile opencdr-dev \
  --account-id 850695279560 --region us-east-1
```

6. Review account/region, code/config revisions, physical identities, epoch,
   backfill counts and digest. Apply using the exact plan digest printed above:

```bash
opencdr-deploy release detection-activation apply activation.json \
  --sha256 ACTIVATION_PLAN_SHA256 --profile opencdr-dev \
  --account-id 850695279560 --region us-east-1
```

Apply re-observes AWS and the completed backfill. Changed code, configuration,
table bindings, source identity, backfill revision or activation revision prevents
application. It atomically records the authenticated AWS operator and review
document alongside the head update. After an uncertain response, retry the same
file/digest. A superseded plan cannot reactivate reads.

7. Check Core queries and delivery, then recover/activate Fleet consumers and
   explicitly enable Fleet detection polling. Portable Fleet releases now default
   detection polling to disabled; existing installations retain their saved setting.

## Later upgrades of an activated feed

Prepare/apply a pause before planning the next release:

```bash
opencdr-deploy release detection-activation plan opencdr-dev \
  --action pause --reason 'Begin reviewed Core runtime upgrade' \
  --output pause.json --profile opencdr-dev --account-id 850695279560
opencdr-deploy release detection-activation apply pause.json \
  --sha256 PAUSE_PLAN_SHA256 --profile opencdr-dev --account-id 850695279560
```

Both release planning and execution require paused reads. Execution additionally
claims a durable deployment barrier before starting CloudFormation, so a concurrent
activation cannot slip between the check and deployment. If execution is interrupted,
the barrier stays in place. Retry the same reviewed change set; after a failed
CloudFormation update, prepare a new reviewed update once the stack has settled.
Activation requires that the exact claimed change set finished successfully.
Do not remove the barrier with a manual table edit.

After the update, repeat the post-deployment wait, new backfill and activation.
Receipt acknowledgement is transactionally fenced against a concurrent pause;
it cannot silently advance a checkpoint after the gate closes. The pause does
not rewind consumers, remove pending receipts or clear queues.

## Boundaries and measurements

- Core stacks with an existing **unguarded** feed are rejected for rolling upgrades.
  They need a separately reviewed migration; this candidate does not pretend the
  guard can stop older code that never checks it.
- Qualified Lambda aliases, qualified stream/queue mappings and qualified
  EventBridge targets are rejected. Normal Core triggers target unqualified
  functions. Preserve and inspect queues/DLQs; the procedure never purges them.
- Deploy serializes its activation/update operations. Operators must also serialize
  direct AWS administration with them. This is not a lock against arbitrary
  administrators changing AWS resources outside Deploy.
- Permissions: the operator needs STS identity; CloudFormation stack/template/resource
  reads; Lambda configuration/alias/mapping reads; EventBridge target reads;
  DynamoDB DescribeTable/GetItem and transactional UpdateItem/PutItem on the feed.
  These privileges belong to deployment operators, not SaaS delivery principals.
- Record writer settlement, backfill start/finish, read activation and first healthy
  collection separately. Compare both detection kinds, payload hashes, retained
  source counts, invalid/conflict findings, consumer checkpoints and collection lag.
- Rechecking the read gate adds DynamoDB metadata/head reads. Measure request counts,
  p50/p95 latency and cost with 100 accounts; local tests are not AWS cost evidence.
- Physical source/feed/S3 restore, retained-case recovery, populated browser checks,
  100-account benchmarks and public release acceptance remain separate roadmap gates.
