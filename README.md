# OpenCDR Deploy

`opencdr-deploy` installs and manages multiple named
[OpenCDR Core](https://github.com/dbnz-io/opencdr) deployments from one
computer. It also installs and operates optional
[OpenCDR Fleet Managers](https://github.com/dbnz-io/opencdr-fleet-manager)
in security accounts. Each installation can use a different Core checkout/version, AWS
profile or account, region, stage, and feature set.

The tool is an orchestrator, not a second infrastructure engine. OpenCDR Core's
`serverless.yml` remains the infrastructure source of truth.

**Updating a deployment from the platform?** Start with the
[SOC operator walkthrough](https://github.com/dbnz-io/opencdr-deploy/blob/main/docs/operator-walkthrough.md). It gives one sequence
of commands, expected results and troubleshooting, without requiring a Core clone.

## Install

```bash
python3 -m venv "$HOME/.opencdr-tools"
. "$HOME/.opencdr-tools/bin/activate"
python -m pip install --upgrade 'opencdr-deploy>=0.7.0'
opencdr-deploy --help
opencdr-deploy --version
```

Python 3.11 or newer is required. The [prebuilt release workflow](docs/prebuilt-releases.md)
uses Python and AWS credentials for Core and Fleet installation. Source builds require the tools used by
that Core release: AWS CLI, Node/npm, Serverless Framework, Docker, `jq`, and
Python 3.12. `opencdr-deploy doctor` checks the selected installation.
In each new terminal, run `. "$HOME/.opencdr-tools/bin/activate"` to use the
installed tool. These shell commands apply to macOS/Linux or Windows WSL.

## Deploy and operate Fleet Manager

Scoped IAM management and prebuilt releases require OpenCDR Deploy 0.7.0 or later.
The managed source defaults
to Fleet Manager **v0.2.0**, which supports small-account Lambda quotas. Fleet
Manager runs in the security account; it polls registered Core agents and forwards
explicit API operations. It does not deploy Core stacks or replace Core as the
authority for detections, configuration and response execution.

Health/configuration cadence is configurable with `--poll-interval-minutes`
(1–60, default 1) and `--full-refresh-minutes` (at least the poll interval, at most
1440, default 60). Source packaging and compatible prebuilt `release plan`
support these options. The local P1 implementation adds separate durable detection
collection options described below; publication and live acceptance remain pending.

You can run these commands from any directory. **No fleet-manager clone is
required.** Git, Node/npm, Python 3.12+ and an authenticated AWS profile are needed
for packaging/deployment. The CLI obtains the versioned service source from
`https://github.com/dbnz-io/opencdr-fleet-manager.git`, creates isolated build tools,
installs hash-locked Python requirements and locked Node dependencies, and invokes
the service's pinned Serverless Framework. Serverless authentication still follows
your organization's setup. API-only operations need the AWS profile and saved URL;
they do not clone repositories or install Node/build tools.

### 1. Build and review the package

Replace the account ID with your security account. This checks the AWS identity
before packaging but does not create the fleet stack:

```bash
aws sso login --profile security-admin
opencdr-deploy fleet deploy security-prod \
  --profile security-admin \
  --security-account-id 222222222222 \
  --region us-east-1 \
  --stage prod \
  --package-only \
  --output /tmp/opencdr-fleet-reviewed
```

Expected: `Package ready ... No AWS stack was created or updated.` Review
`/tmp/opencdr-fleet-reviewed/cloudformation-template-update-stack.json`. The
directory includes the exact Lambda ZIP and a content/target manifest. Keep it in
place until deployment; it must not be moved or edited after review.

### 2. Deploy that exact artifact

```bash
opencdr-deploy fleet deploy security-prod --artifact /tmp/opencdr-fleet-reviewed
opencdr-deploy fleet status --fleet security-prod
```

Deployment saves the `FleetManagerApiUrl` automatically. `--artifact` verifies the
account, region, stage, version, feature options and artifact digest before
deploying without rebuilding. A successful package alone produces no stack
outputs. To build and deploy in one invocation, omit `--package-only` and `--artifact`.

Use `--dry-run` to inspect the managed-source/build/deploy commands without making
AWS calls, installing tools, or writing the registry. The standard source tag can
be changed with `--version vX.Y.Z`; previous checkouts remain available. Advanced
users can supply `--repository-url` and `--source-root`. Local Core installation
names and fleet-manager names are independent.

### 3. Connect an already deployed manager

Skip packaging/deployment when a manager already exists:

```bash
opencdr-deploy fleet connect security-prod \
  --url https://FLEET_API_ID.execute-api.us-east-1.amazonaws.com/prod \
  --profile security-admin \
  --security-account-id 222222222222 \
  --region us-east-1 --stage prod --access administrator
opencdr-deploy fleet list
opencdr-deploy fleet status --fleet security-prod
```

The CLI verifies the manager's product, fleet contract, account, region and stage
before saving it. Every later API operation repeats that identity check. IAM
requests use short-lived credentials from the saved AWS profile; no AWS access
keys or Core API keys are written to the local registry. Regional AWS `execute-api`
URLs are supported; custom manager domains and other AWS partitions are not yet supported.

`--fleet NAME` is optional when exactly one manager is saved; it is required when
there are several. A saved name cannot silently move to another account/region/stage.
When a manager is unavailable, operations fail instead of falling back to a direct
Core endpoint. Existing direct Core commands remain available for standalone use.

### 4. Register a Core agent

Copy the [agent example](https://github.com/dbnz-io/opencdr-deploy/blob/main/examples/fleet-agent.json) to `agent.json`. Set its actual
AWS account, region, Core stage and API URL. Start with `allow_writes: false`.
The reviewed file contains no credentials.

```bash
aws sso login --profile member-admin
opencdr-deploy fleet agents bootstrap --fleet security-prod \
  --file agent.json --core-profile member-admin
# Apply the emitted exact role ARNs to the Core trust policy first.
opencdr-deploy fleet agents register --fleet security-prod --file agent.json
opencdr-deploy fleet agents list --fleet security-prod
opencdr-deploy fleet agents show member-prod --fleet security-prod
```

New registrations default to `auth_mode: iam`. `bootstrap` verifies both AWS
accounts and emits the Fleet role ARNs that Core must trust. Apply those principals
through the reviewed Core deployment, then register without `--core-profile`.
IAM uses temporary credentials; no Core keys are created or copied. Select
`write_access: operator`, `responder` or `administrator` explicitly and start with
`allow_writes: false`. See [IAM and rollouts](https://github.com/dbnz-io/opencdr-fleet-manager/blob/main/docs/iam-and-rollouts.md).

For an explicit `auth_mode: legacy-api-key` migration registration, bootstrap still
prepares scoped keys in security-account SSM. Its monthly quota must cover polling;
10,000 requests cannot support one request per minute.

To import or rotate an existing scoped key through hidden terminal input:

```bash
opencdr-deploy fleet agents credentials member-prod --kind read --fleet security-prod
opencdr-deploy fleet agents refresh member-prod --fleet security-prod
```

### 5. Manage agents and their complete Core API

```bash
opencdr-deploy fleet agents configuration member-prod --kind list --fleet security-prod
opencdr-deploy fleet agents request member-prod GET '/rules?rule_kind=signal' --fleet security-prod
opencdr-deploy fleet agents request member-prod PUT /rules/example \
  --file rule-update.json --fleet security-prod
opencdr-deploy fleet agents request member-prod DELETE '/rules/example?rule_kind=signal&expected_rev=3' \
  --fleet security-prod
```

Forwarded paths and JSON bodies use the Core API contract, including each target's
`expected_rev`. Writes need the selected IAM management authority (or legacy write credentials) and `allow_writes: true`
on the agent registration. Update registration with a full reviewed file:

```bash
opencdr-deploy fleet agents update member-prod --file agent.json --expected-rev 1 --fleet security-prod
opencdr-deploy fleet agents disable member-prod --expected-rev 2 --fleet security-prod
opencdr-deploy fleet agents enable member-prod --expected-rev 3 --fleet security-prod
```

The CLI preserves revision conflicts, partial results, manager envelopes and
uncertain write outcomes. It returns a nonzero exit status for failed targets in
HTTP 207 results and does not retry writes. After an uncertain write, inspect the
agent's actual state before retrying. Disabling an agent retains registry/history
and blocks forwarding; it does not revoke its API keys.

The complete manager API, including explicit multi-agent reads/writes, is available
through `fleet request`. Agent listing follows every cursor; configuration and raw
API requests return pagination metadata for explicit continuation.

```bash
opencdr-deploy fleet request GET '/v1/agents?limit=20' --fleet security-prod
opencdr-deploy fleet request POST /v1/fleet/read --file fleet-read.json --fleet security-prod
```

### Change deployment flags or recover a failed first deployment

Repeat deployment with new flags to update the existing stack:

```bash
opencdr-deploy fleet deploy security-prod --security-profile hardened --poll-concurrency 2
opencdr-deploy fleet deploy security-prod --pause-polling
opencdr-deploy fleet deploy security-prod --enable-polling --snapshot-retention-hours 48
```

Defaults are standard security, polling enabled, 24-hour snapshots and a queue
concurrency cap of **2**. Polling uses shared Lambda capacity and makes no reserved
concurrency allocation. Larger fleets can raise `--poll-concurrency` after checking
the account quota and queue backlog. The cap does not guarantee capacity when other
workloads consume the account's shared pool. Hardened enables extra table recovery
controls; a downgrade requires `--allow-protection-downgrade` explicitly.

If an initial deployment fails, the stack can disappear while retained DynamoDB
tables remain. The CLI detects this before attempting another deploy. Compatible
source builds reattach those tables and their original detection evidence bucket
through a saved import-only plan:

```bash
opencdr-deploy fleet recover security-prod \
  --profile security-admin --security-account-id 222222222222 --region us-east-1 --stage prod \
  --out fleet-recovery.json
# Review the saved plan and copy its digest before execution:
opencdr-deploy fleet recover security-prod \
  --profile security-admin --security-account-id 222222222222 --region us-east-1 --stage prod \
  --execute --plan fleet-recovery.json --expected-plan-digest REVIEWED_64_CHARACTER_SHA256
```

Recovery binds table UUIDs, original stack lineage and bucket identity; it
preserves supported storage configuration and refuses non-import actions. Retry
unknown outcomes with the same saved plan and digest. Both schedules stay paused
for the first runtime update. Use the [complete recovery procedure](docs/fleet-storage-recovery.md)
for compatible packaging, first-update checks and acceptance measurements.
Reattaching storage does not repair a replaced worker principal or prove complete
history. Use `fleet worker-identity NAME` and the separate
[principal handoff procedure](../opencdr-internal/docs/detection-handoffs.md) in the
workspace for that review. These implementations still need live acceptance.

## Install the optional MCP client

OpenCDR's MCP management server is distributed independently and is not required
by Core. Install it into the same Python environment as `opencdr-deploy`:

```bash
opencdr-deploy mcp install
opencdr-deploy mcp status

# Upgrade to the latest published release, or select an exact version.
opencdr-deploy mcp install --upgrade
opencdr-deploy mcp install --version 0.1.0
```

This also works when `opencdr-deploy` was installed with `pipx`: the MCP package
is injected into the deployment manager's isolated environment, and the
`opencdr-mcp` executable is placed beside `opencdr-deploy`. Use `--dry-run` to
show the exact pip command without changing the environment.

MCP 0.3.0 supports IAM-capable Core releases with an API URL, expected deployment
identity and scoped AWS profile. Legacy API-key connections remain available for
migration. Installation does not read
credentials from the deployment registry or silently configure an MCP client.
See the [opencdr-mcp documentation](https://github.com/dbnz-io/opencdr-mcp) for
profiles and client registration.

For local development:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

## Install Core and register installations

The registry defaults to `~/.config/opencdr/deployments.json`, is mode `0600`,
and contains no credentials. Override its location with `--registry` or
`OPENCDR_DEPLOY_REGISTRY`.

A separate OpenCDR Core clone is not required. The manager can download an
exact release tag into its managed source directory and retain each installed
version:

```bash
opencdr-deploy installations add customer-production \
  --core-version v1.2.1 \
  --stage prod \
  --region eu-west-1 \
  --aws-profile customer-production \
  --expected-account-id 111111111111 \
  --security-profile hardened \
  --archive true \
  --xray true

opencdr-deploy installations versions customer-production
# Upgrade or roll back by selecting another published tag.
opencdr-deploy installations upgrade customer-production \
  --core-version v1.2.0 \
  --prepare

# Local checkouts remain supported for development and forks.
opencdr-deploy installations add customer-staging \
  --repository ~/src/opencdr-staging \
  --stage staging \
  --region us-east-1 \
  --aws-profile customer-staging \
  --archive false

opencdr-deploy installations list
opencdr-deploy installations show customer-production
```

Managed releases default to `https://github.com/dbnz-io/opencdr.git` and
`~/.local/share/opencdr-deploy/core`. Override those with
`--core-repository-url` and `--source-root`. Only version-shaped immutable tags
such as `v1.2.1` are accepted. Old versions are retained, so rollback means
running `installations upgrade` with the previous tag. Removing a registry
entry does not delete managed source or AWS resources.

AWS credentials remain in the normal AWS configuration or IAM Identity Center
credential chain. Serverless credentials remain in Serverless' own mechanism.
Potentially secret parameter names are rejected from the registry.

## Configure several accounts from TOML

Every installation, AWS profile/account, Core version, and feature flag can be
declared in one TOML file. Start from
[`examples/fleet.toml`](examples/fleet.toml):

```toml
version = 1

[defaults]
core_version = "v1.2.1"
region = "us-east-1"

[defaults.features]
archive = true
xray = true
security_profile = "hardened"

[[deployments]]
name = "customer-a-production"
stage = "prod"
aws_profile = "customer-a-production"
expected_account_id = "111111111111"

[[deployments]]
name = "customer-b-production"
stage = "prod"
aws_profile = "customer-b-production"
expected_account_id = "222222222222"

[deployments.features]
archive = false
```

Reconcile all entries, or select one:

```bash
# Validate and show intended source operations without writing state.
opencdr-deploy apply --file fleet.toml --dry-run

# Download Core versions and update the local registry.
opencdr-deploy apply --file fleet.toml

# Also install locked dependencies, package, and deploy each installation.
opencdr-deploy apply --file fleet.toml --prepare --deploy

opencdr-deploy apply --file fleet.toml \
  --select customer-a-production \
  --prepare --deploy
```

`[features]` provides typed values for `archive`, `xray`, and
`security_profile`. Any other non-secret Serverless option belongs under
`[parameters]`. Do not put credentials or API keys in the file; secret-shaped
parameter names are rejected.

`expected_account_id` is strongly recommended. Before any live AWS operation,
the manager resolves the selected profile with STS and stops if the account is
not the configured 12-digit ID. Multi-account apply is fail-fast by default;
`--continue-on-error` attempts the remaining entries. It is not a distributed
transaction: successful deployments are not rolled back if a later account
fails.

## Install dependencies and deploy

```bash
DEPLOY=(opencdr-deploy --deployment customer-production)

"${DEPLOY[@]}" doctor
"${DEPLOY[@]}" prepare
"${DEPLOY[@]}" package
"${DEPLOY[@]}" deploy
"${DEPLOY[@]}" status
```

`prepare` initializes the Core checkout's pinned submodules, runs `npm ci`,
creates a Python 3.12 virtual environment, and installs its locked, hashed
Python dependencies.

`deploy` verifies `expected_account_id` and packages first unless `--artifact`
is provided. Each package has a
manifest containing its Core Git revision, deployment configuration, creation
time, and SHA-256 digest. Deployment rejects modified artifacts, artifacts for
another installation, dirty source trees, and unacknowledged
`hardened -> standard` protection downgrades.

For development only, `--allow-dirty` permits packaging uncommitted source and
records that fact in the artifact manifest. `--dry-run` is non-mutating.

## Apply an update downloaded from the platform

Start with the [SOC operator walkthrough](https://github.com/dbnz-io/opencdr-deploy/blob/main/docs/operator-walkthrough.md). It covers
installation, AWS sign-in, checking the account, downloading Core, checking the
workstation, deploying and verifying the result. Each step says what to expect
and what to do if it fails.

The platform prepares a configuration file and matching instructions. You run
opencdr-deploy on your approved deployment workstation using your existing AWS
profile. There are no platform deployment roles, OIDC setup or platform-triggered
infrastructure updates. You do not need a separate Core clone.

`--dry-run` previews local configuration/source work. It does not check AWS
permissions or generate a CloudFormation change set. Running `apply` without
`--deploy` downloads/registers Core locally and does not deploy AWS resources.

For an existing local installation, keep its registered name. Version 0.5.0
also rejects account/region/stage name collisions and
duplicate registrations for the same known AWS deployment before source changes.

## Manage features

```bash
"${DEPLOY[@]}" features show

# Change desired state without deploying.
"${DEPLOY[@]}" features set archiveEnabled=false xrayEnabled=false

# Change, package, and deploy.
"${DEPLOY[@]}" features set archiveEnabled=true --deploy
```

The known security-sensitive flags receive strict validation. Future non-secret
Serverless parameters can be registered without upgrading this tool:

```bash
"${DEPLOY[@]}" features set futureFeature=enabled
```

## Operate Core

`opencdr-deploy` is the supported operator interface. It selects the correct
Core release, AWS profile, account, region, and stage before invoking a
version-compatible workflow:

```bash
"${DEPLOY[@]}" catalog load --dry-run
"${DEPLOY[@]}" test local --event 009
"${DEPLOY[@]}" test deployed --event 009
"${DEPLOY[@]}" coverage regions --regions eu-west-2
"${DEPLOY[@]}" coverage accounts --profiles member-a,member-b
"${DEPLOY[@]}" cost --granularity MONTHLY
"${DEPLOY[@]}" ingestion canary falco-production
```

Arguments supplied by the operator override injected stage/region values.
Child arguments are concealed in manager logs because operational commands can
contain secrets. The implementations remain in the selected Core release during
the compatibility period so an older deployment always runs matching logic.

`scripts list` and `scripts run` remain as deprecated compatibility escape
hatches. Direct execution of scripts from a Core checkout is also deprecated;
it will be removed only in a separately announced major Core release.

## Manage the complete REST API

API operators do not need a Core checkout. Use an IAM profile authorized for the
selected management path and an owner-readable connection file, for example
`customer-dev.json`:

```json
{
  "mode": "core",
  "auth_mode": "iam",
  "url": "https://example.execute-api.us-east-1.amazonaws.com/dev",
  "aws_profile": "customer-operator",
  "access": "operator",
  "deployment": {
    "account_id": "111111111111",
    "home_region": "us-east-1",
    "stage": "dev"
  }
}
```

```bash
chmod 600 customer-dev.json
opencdr-deploy api request GET /status --connection customer-dev.json
opencdr-deploy api request POST /rules/validate \
  --connection customer-dev.json --file candidate.json
opencdr-deploy api request GET /rules/runtime-status --connection customer-dev.json
```

The file must use the regional AWS HTTPS endpoint. IAM requires all three
`deployment` identity fields and verifies them against `/status` before an
operation. CLI and MCP 0.3.0 accept this same file and enforce its identity checks.
The `access` selector does not grant IAM permissions. Use one file per agent;
`--connection` cannot be combined with `--deployment`. Requests refuse redirects.

The local consumer-receipt implementation adds `access: delivery` for a dedicated
Core connection. It can register/inspect `/detections/consumers/{id}`, POST
`/read` with `expected_rev`, and POST `/ack` with `expected_rev` and `receipt_id`.
Use the same `api request ... --connection ... --file ...` commands. Persist a
page durably before acknowledging it; after an uncertain result inspect consumer
status. This role cannot configure Core or run responses. It is a source feature
pending release/live acceptance, not an additional installer or an AWS grant
created merely by changing the connection JSON.

For explicit legacy migration, a file containing `url`, `key` and optional
`deployment` identity fields uses that key without AWS credentials or SSM lookup.
Keep files containing keys out of Git. New deployments disable legacy access.

For registered AWS installations, the existing commands below retrieve the
selected installation's endpoint through its AWS profile. Infrastructure
operations still use that AWS path. The local P2/P3 candidate adds engine-owned
cases, detection handling and the content library through the same API command.
Existing SaaS records remain separate until migration. Approval policy and
durable job authorization remain pending.

The manager retrieves the API URL from `/opencdr-<stage>/api-url` in SSM.
IAM installations sign requests with their AWS credentials. Only legacy
installations retrieve `/opencdr-<stage>/api-key`; keys are never copied into
the registry.

```bash
# Discover routes supported by the deployed Core version.
"${DEPLOY[@]}" api request GET /help

# Query with safely encoded parameters.
"${DEPLOY[@]}" api request GET /signals/stats \
  --query date_from=2026-10-01 \
  --query date_to=2026-10-27

# Rule authoring and immutable releases.
"${DEPLOY[@]}" api request GET /rules/schema
"${DEPLOY[@]}" api request POST /rules/validate --file candidate.json
"${DEPLOY[@]}" api request POST /rules/evaluate --file evaluation.json
"${DEPLOY[@]}" api request POST /rule-releases/plan --file release.json
# After review, add the returned digest as expected_plan_digest in this manifest.
"${DEPLOY[@]}" api request POST /rule-releases/apply --file reviewed-release.json

# Any other current or future JSON route.
"${DEPLOY[@]}" api request GET /ir-roles
"${DEPLOY[@]}" api request PUT /ir-roles/123456789012 --file role.json
"${DEPLOY[@]}" api request POST /integrations/falco/jobs/OBJECT_ID/replay
```

Supported methods are `GET`, `POST`, `PUT`, and `DELETE`. Bodies can be read
from `--file`, stdin (`--file -`), or `--data`. Prefer a protected file or stdin
for secrets so they do not appear in process arguments. Absolute URLs are
rejected so a deployment's API key cannot be redirected to another host.
HTTP failures return a nonzero exit status.

For friendly API commands, use the top-level `manage` command:

```bash
"${DEPLOY[@]}" manage rules list --kind signal
"${DEPLOY[@]}" manage lists list
"${DEPLOY[@]}" manage settings get
"${DEPLOY[@]}" manage ir-actions list
```

`api core-cli` remains as a deprecated alias during the compatibility period.

## Security and responsibility boundary

The tool manages OpenCDR-owned resources and calls OpenCDR's management API. It
does not silently enable customer-account services or choose API exposure,
network policy, CloudTrail, GuardDuty, backups, recovery objectives, SCPs, or
other controls owned by the deployer under the shared-responsibility model.

See [SECURITY.md](SECURITY.md) for vulnerability reporting and credential
handling guidance.

## Releasing to PyPI

Releases use PyPI Trusted Publishing; no PyPI API token is stored in GitHub.

Maintainers must configure these once:

1. Create a GitHub environment named `pypi` and require reviewer approval if
   appropriate.
2. On PyPI, configure a trusted publisher for owner `dbnz-io`, repository
   `opencdr-deploy`, workflow `publish.yml`, environment `pypi`. A pending
   publisher can create the project on first release.
3. Update `project.version` and `opencdr_deploy.__version__` to the same value.
4. Merge the version change after CI passes, then publish a GitHub Release whose
   tag is exactly `v<version>` (for example `v0.1.0`).

The publish workflow rebuilds from the release commit, verifies the tag/version,
checks both distributions, uploads them as a workflow artifact, and publishes
through GitHub OIDC. PyPI project names are global; `opencdr-deploy` was
unregistered when this repository was initialized, but the pending publisher
should be configured before announcing the first release.

## License

Mozilla Public License 2.0. See [LICENSE](LICENSE).

### Independent detection collection options (local P1 implementation)

Compatible Fleet builds accept `--detection-poll-interval-minutes` (1–60),
`--detection-retention-days` (7–365), `--detection-poll-concurrency` (2–1000),
and `--enable-detection-polling` / `--pause-detection-polling` on `fleet package`,
`fleet deploy` and portable `release plan`. These are separate from health and
configuration polling. Omitted values preserve existing configuration.
IAM bootstrap includes the detection worker's exact observer role; apply its
reviewed Core trust update before expecting collection to succeed.

`fleet agents show AGENT` includes collection status. The specific route is
`fleet request GET /v1/agents/AGENT/detections/status`. Indexed central queries and
explicit retained-source backfill/reconciliation are implemented locally; backfill
is never automatic. `fleet recover` now reattaches the paired retained detection
table and evidence bucket using a reviewed plan, with polling paused. See the
[recovery guide](docs/fleet-storage-recovery.md) for supported configurations and
remaining consumer-identity/restore gates. Current changes are not yet published.


Reviewed [feed storage and consumer recovery](../opencdr-internal/docs/detection-feed-recovery.md)
is implemented locally through Deploy API commands: inspect physical binding,
review/apply generation rotation, complete retained-source backfill, then queue
Fleet replay with polling paused. This does not initiate AWS PITR or claim live
recovery acceptance.

Detection handling and triage access: see [operator notes](docs/detection-handling.md).

## Operational cases (P2 candidate)

The local candidate adds shared operational cases: Core owns standalone cases,
Fleet owns multi-account cases. Read `capabilities.operational_cases == "1.0.0"`
before using `/cases` (Core) or `/v1/cases` (Fleet). Create, edit, notes, verified
attachments, retained evidence, close/reopen and immutable history use IAM,
revision checks and stable request IDs. Cases track SIEM work; they do not run
investigations or change detection handling. Fleet requires explicit principal
account grants, even for administrators, and never fans out case reads to Core.

Use the [owner deployment/test and KPI guide](../opencdr-internal/docs/operational-cases.md) for grants, Deploy commands,
limits, failure scenarios and remaining live/retention/migration gates. This
candidate is locally tested; it has not been deployed or publicly promoted.

### Dedicated connector connections (P2 candidate)

Core IAM connection JSON and saved Fleet connections now accept
`access: integration`. This signs only the dedicated entry-point URL; the actual
AWS caller policy must restrict the connector role to that entry point. The
backend permits status discovery and scoped case external-status reports only.
Use an administrator connection to manage `/case-integrations/{id}` trust
bindings. In Fleet mode prefix the path with `/v1` and operate on Fleet's cases.

`api request` / `fleet request` also support active-case lookup and selected Core
response-outcome reads without another deployment tool. Use `--file` for report
JSON, retain exact request IDs/bodies for retry, and keep vendor credentials out
of requests and connection files. This does not install a native SIEM adapter.
The coordinated Core manual `docs/case-connectors-and-outcomes.md` contains the
role/account/origin binding, upgrade constraints and manual acceptance sequence.

## Content library (P3 local candidate)

Deploy's existing signed API commands are sufficient for the new library; no
additional operator tool is needed. Use `api request` with `/library/*` against
standalone Core, or `fleet request` with `/v1/library/*` against the customer Fleet.
Check `content_library` and `library_release_snapshots` capabilities first.
Operator access creates/edits drafts and freezes snapshots; observer/triage read.
Stable request IDs and current head revisions are required for safe retries.
Saving/freezing never activates, validates or approves runtime content.

See the coordinated [library deployment/test manual and KPIs](../opencdr-internal/docs/content-library.md)
for payloads, retained-storage limitations and manual acceptance. These changes
are local source, pending packaging/live acceptance and public release.

See [owner automation and subscription initialization](docs/owner-automation.md) for portable runners, exact-receipt retry and reviewed new-consumer cutover.
