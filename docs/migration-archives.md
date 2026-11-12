# Portable migration archives — candidate format 1

`migration-archive` preserves explicit source records, references, identity mappings,
unresolved records, unknown provenance and historical actors. It performs local file
operations only. A SQLite stage is inert: even a retained `sending` job or enabled policy
cannot execute. This is part of P7, not a completed live ownership migration.

```sh
opencdr-deploy migration-archive export source.json ./archive
opencdr-deploy migration-archive verify ./archive --sha256 MANIFEST_SHA256
opencdr-deploy migration-archive stage ./archive --sha256 MANIFEST_SHA256 \
  --database ./migration-stage.sqlite
```

The manifest digest is returned by export. Pin it through the reviewed migration record.
Each content-addressed chunk contains at most100 records. Export writes an identity intent
first and publishes each complete file atomically without overwriting existing files.
Rerunning export requires identical source bytes after canonical ordering. A changed
source needs a new archive directory; never replace the previous archive's evidence.
Staging verifies every chunk and reference before touching the database, then commits
records and checkpoints together per chunk. An interrupted chunk rolls back; resume
revalidates previously staged bytes and rejects changes or a different manifest.

The input is a JSON object with exactly these fields:

- `source`: explicit owner/snapshot identity supplied by a source adapter.
- `identity_map`: source-to-target identities; target collisions are rejected.
- `records`: objects containing `family`, `id`, `payload` and `references`.
  Families are `case`, `library`, `history`, `policy`, `job` and `evidence`.
  References contain exact `family` and `id` pairs in this archive.
- `unresolved`: retained source records/issues that cannot yet be linked. Do not
  invent an actor, rule version or foreign identity to make validation pass.

Payloads are preserved as source objects. Identity maps are retained, not blindly
applied to every string in a historical record. Duplicate identities, dangling
references, corrupt hashes, unsafe chunk paths and symlink inputs are rejected.
Current bounds are100000 records and64MiB per input file. This format is not a
large-binary attachment transfer protocol or a publisher signature.

`source_consistency: not_attested_by_archive` is deliberate. A source adapter must prove
its writer/worker fence and final-delta consistency independently. The current local
archive does not attest a live snapshot, import records into an engine table, transfer
an execution claim or activate an owner. Those P7 gates remain open.

The engine candidate adds atomic ownership checks to case/Library transactions. Frozen,
retired and malformed owner states reject writes; a race with a freeze or revision
change aborts the transaction. A controller must still drain already-claimed workers
and verify external effects before activating another owner. Do not write or delete
ownership metadata manually, and do not use this fence as a substitute for the existing
Deploy recovery/activation guards. These new checks have local test evidence only and
are not yet deployed to the verification stacks.


## Native library capture

The candidate installed CLI can read an existing Core or Fleet library, resolve its
current physical storage bindings, and export immutable artifacts with native reference
checks. It does not write to AWS:

```sh
opencdr-deploy migration-archive capture-library ./library-archive \
  --profile "$PROFILE" --account-id "$ACCOUNT" --region "$REGION" --stack "$STACK"
```

The adapter checks the account, settled/unlocked stack and physical table identity;
reads the table twice around referenced S3 object reads; validates version dependencies,
release roots, test evidence, compilations, legacy release mappings and artifact hashes;
and preserves raw DynamoDB attributes, historical actors and unknown provenance.
Unsupported partitions remain explicit unresolved records. Owner metadata is an observation,
not imported authority. Only referenced library objects are included; orphan S3 inventory
is outside this adapter's scope. A changed table/deployment aborts publication.

Two matching reads of active storage are not a writer fence or a point-in-time snapshot.
The archive therefore retains `writer_fence_attested: false` and cannot authorize cutover.
Native case adapters, guarded family transfer and reverse recovery passed the
isolated P7 engine rehearsal on2026-11-11; see [ownership transfer](ownership-transfer.md). No command here activates rules, policies, jobs or consumers.

Worker outcome persistence distinguishes freezing from retirement. New effect claims
are rejected while frozen. A matching permanent effect claim may record a terminal result
under the same ownership revision, allowing the controller to drain work. Retirement
rejects those writes too; the controller must reconcile every claim before retiring an owner.
These boundaries were checked in the isolated engine rehearsal; public release
verification and application migration remain separate gates.


## Native cases and import previews

`capture-cases` accepts the same account/profile/region/stack arguments as
`capture-library`. It verifies complete case audit revisions, active-detection uniqueness,
retained attachment evidence, case relationships and indicator attachment references.
It also retains policy, grant, connector and execution history as inert archive records.
Unknown partitions are explicit unresolved records; ownership metadata is an observation,
never imported authority. Only current case attachment objects are captured.

Create a JSON map from source scope to destination scope, for example
`{"850695279560:us-east-1:p0-verify":"core-p0-verify"}` for a reviewed fixture.
This map is data only; it does not register or authorize a Fleet member. Library previews
use an empty object because the authoring library is owner-wide.

```sh
opencdr-deploy migration-archive import-preview ./case-archive \
  --sha256 "$ARCHIVE_SHA256" --agent-map ./agent-map.json --output ./preview.json
```

The preview revalidates the native graph, maps case scope/attachment/active-link identities,
retains historical audit text and actors verbatim, and reports exact source/target row
hashes and object hashes. Source grants, connectors, policies and jobs remain in the
retained archive; the preview never makes them active destination records. Reversing the
map on a new destination archive preserves new case revisions and audit entries. It does
not roll back data by restoring the original snapshot.

This is still a local preview: no AWS writes, worker drain, destination verification or
activation occurs. An empty-target requirement, reviewed physical identities, runtime
fence compatibility, final-delta reconciliation, retained recovery window and upgrade
protection must be enforced by the live controller before a handoff can be accepted.
SaaS imports, old application URLs and hosted worker migration remain P10.8.


The new guarded [native ownership transfer](ownership-transfer.md) candidate adds
reviewed physical/runtime pins, existing Deploy lease exclusion, final-delta capture,
inert conditional imports, ordered activation and reverse recovery. Installed/live
native rehearsal and portable consumer switching passed for the isolated engine
scope; this does not establish public release or application migration acceptance.
