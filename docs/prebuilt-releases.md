# Prebuilt releases

The `release` commands deploy a reviewed CloudFormation template and runtime ZIP
using Python and AWS credentials. Customers do not need Node, Docker, Serverless
Framework or a Core source checkout for this path.

A release directory contains `release.json`, `template.json` and `runtime.zip`.
Obtain the manifest SHA-256 from the reviewed release channel. The installer
checks it and both artifact hashes before contacting AWS. These hashes provide
integrity against the pinned release; they are not an independent publisher
signature. HTTPS downloads do not follow redirects. Download GitHub release assets
through an authenticated release channel first when it requires redirects.

If the account has no artifact bucket, prepare one using the same CLI. Review and
execute the returned change set, then wait for the bootstrap stack to reach
`CREATE_COMPLETE`. This is a one-time operation per account and region; reuse its
bucket for later releases. It blocks public access, enforces TLS and bucket owner
ownership, enables encryption and versioning, and retains the bucket on deletion.

```sh
opencdr-deploy release bootstrap --profile customer-admin \
  --account-id 123456789012 --region us-east-1
opencdr-deploy release execute REVIEWED_BOOTSTRAP_CHANGE_SET_ARN \
  --profile customer-admin --account-id 123456789012 --region us-east-1
```

The bucket name is `opencdr-artifacts-ACCOUNT_ID-REGION`. An existing customer-owned
bucket can be used instead. Bootstrap creates a new stack; it does not adopt or
modify an existing bucket. AWS authentication must already be configured.

```sh
opencdr-deploy release verify ./release --sha256 MANIFEST_SHA256
opencdr-deploy release plan ./release --sha256 MANIFEST_SHA256 \
  --profile customer-admin --account-id 123456789012 --region us-east-1 \
  --stage prod --artifact-bucket CUSTOMER_OWNED_ARTIFACT_BUCKET
opencdr-deploy release execute REVIEWED_CHANGE_SET_ARN \
  --profile customer-admin --account-id 123456789012 --region us-east-1
```

`plan` verifies the AWS account and bucket owner, uploads immutable artifact paths,
preserves existing optional template parameters, refuses to remove retention
protection, and returns the complete CloudFormation change set. `execute` accepts
only an available change set in the selected account and region. Inspect stack
completion in CloudFormation before connecting. Pass exact trusted Fleet role ARNs
with repeated `--principal-arn` options. New installations disable legacy access.
Upgrades preserve the existing `LegacyApiEnabled` and `ManagementPrincipalArns`
parameters unless explicitly overridden. Pass `--legacy-api` when first migrating
a source-compiled stack that has no such parameters; use `--disable-legacy-api`
after all clients have migrated.

Compatible Fleet artifacts also accept `release plan --poll-interval-minutes 5
--full-refresh-minutes 60`. These configure health/configuration observation, not
detection delivery. The CLI validates bounds and preserves omitted settings on
upgrades. It rejects these options for Core and older Fleet artifacts that do not
advertise the corresponding template parameters.

Compatible Core artifacts with the durable detection feed accept
`release plan --detection-replay-days 14` (7–30 days; default 7 for new
installations). Omitted values preserve an existing stack's setting. This controls
feed replay, independently of searchable detection or case retention. It does
not backfill historical detections or configure Fleet's detection cadence. The
CLI rejects the option for Fleet and older Core artifacts without the parameter.

Keep using prebuilt releases for stacks created from these artifacts. Moving a
new artifact-created stack back to a source deployment can rename generated IAM
roles and requires a separate migration review.

Create one secret-free connection file usable by both CLI and MCP:

```sh
opencdr-deploy release connection ./customer.json --url CORE_STAGE_URL \
  --profile customer-operator --account-id 123456789012 \
  --region us-east-1 --stage prod --access operator
opencdr-deploy api request GET /status --connection ./customer.json
OPENCDR_CONFIG_FILE=./customer.json opencdr-mcp
```

For source-based development, `installations add` defaults to IAM and requires the
expected account ID. Existing saved installations retain legacy behavior until
explicitly replaced with IAM configuration. Content operations use the management
API; the deprecated shell loader now delegates to that same API.

Maintainers compile and audit their templates and Linux runtime ZIPs in CI, then
run `release export TEMPLATE ZIP OUTPUT --product opencdr --version VERSION
--account-id BUILD_ACCOUNT --region BUILD_REGION --stage BUILD_STAGE`.
The exporter preserves CloudFormation logical IDs, parameterizes deployment
identity, and rejects templates containing multiple distinct runtime archives.
Feature and security profiles remain properties of the reviewed template; publish
and test each supported profile as a separate artifact. A release is ready only
after installing its exported artifact in a clean stack and upgrading a retained
stack, including a runtime code change. Validate supported account and regional
configurations before advertising them. Source compilation and local unit tests
alone do not satisfy that gate.

The candidate build workflow requires the `release-build` GitHub environment,
`OPENCDR_RELEASE_BUILD_ROLE_ARN` variable and Serverless license secret. It uploads
review artifacts without publishing a public release. The IAM-capable Core must
first pass the existing internal-to-public promotion process; older public Core
tags do not become compatible merely by upgrading this CLI.
## Detection read activation

Core releases containing the durable feed require the
[reviewed activation procedure](detection-activation.md) after installation or
upgrade. Detection processing continues while new indexed reads and receipt
progress remain gated. Before upgrading an activated feed, pause its reads with
Deploy. Release execution rechecks this boundary and claims a deployment barrier.
Portable Fleet releases default detection polling to disabled; enable it explicitly
after Core activation and reviewed consumer recovery.

