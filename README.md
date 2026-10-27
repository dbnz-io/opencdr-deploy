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

For local development:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

## Register installations

The registry defaults to `~/.config/opencdr/deployments.json`, is mode `0600`,
and contains no credentials. Override its location with `--registry` or
`OPENCDR_DEPLOY_REGISTRY`.

```bash
opencdr-deploy installations add customer-production \
  --repository /opt/opencdr/releases/1.4.0 \
  --stage prod \
  --region eu-west-1 \
  --aws-profile customer-production \
  --security-profile hardened \
  --archive true \
  --xray true

opencdr-deploy installations add customer-staging \
  --repository ~/src/opencdr-staging \
  --stage staging \
  --region us-east-1 \
  --aws-profile customer-staging \
  --archive false

opencdr-deploy installations list
opencdr-deploy installations show customer-production
```

AWS credentials remain in the normal AWS configuration or IAM Identity Center
credential chain. Serverless credentials remain in Serverless' own mechanism.
Potentially secret parameter names are rejected from the registry.

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

`deploy` packages first unless `--artifact` is provided. Each package has a
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

## Run Core operational scripts

The selected Core checkout remains responsible for version-coupled operational
scripts. The manager discovers all `.py` and `.sh` entry points under its
`scripts/` directory, supplies the correct AWS profile/region, and injects the
stage/region options for known scripts.

```bash
"${DEPLOY[@]}" scripts list
"${DEPLOY[@]}" scripts run load_rules.sh -- --dry-run
"${DEPLOY[@]}" scripts run test_deployed.sh -- --event 009
"${DEPLOY[@]}" scripts run setup_region_forwarding.sh -- --regions eu-west-2
"${DEPLOY[@]}" scripts run cost_report.sh -- --granularity MONTHLY
```

Explicit script arguments override injected values. Child arguments are
concealed in manager logs because operational commands can contain secrets.

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

For the selected Core version's friendly API commands:

```bash
"${DEPLOY[@]}" api core-cli -- rules list --kind signal
"${DEPLOY[@]}" api core-cli -- settings get
```

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
