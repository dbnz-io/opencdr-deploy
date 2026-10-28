# OpenCDR Deploy

`opencdr-deploy` installs and manages multiple named
[OpenCDR Core](https://github.com/dbnz-io/opencdr) deployments from one
computer. Each installation can use a different Core checkout/version, AWS
profile or account, region, stage, and feature set.

The tool is an orchestrator, not a second infrastructure engine. OpenCDR Core's
`serverless.yml` remains the infrastructure source of truth.

## Install

```bash
python3 -m pip install opencdr-deploy
opencdr-deploy --help
opencdr-deploy --version
```

Python 3.11 or newer is required. Deploying Core also requires the tools used by
that Core release: AWS CLI, Node/npm, Serverless Framework, Docker, `jq`, and
Python 3.12. `opencdr-deploy doctor` checks the selected installation.

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

The MCP package can manage any OpenCDR deployment, but it still needs that
deployment's API URL and a suitably scoped API key. Installation does not read
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

The manager retrieves the selected installation's API URL and full-scope key
from `/opencdr-<stage>/api-url` and `/opencdr-<stage>/api-key` in SSM at request
time. It never copies them into the registry.

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
"${DEPLOY[@]}" api request POST /rule-releases/apply --file release.json

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
