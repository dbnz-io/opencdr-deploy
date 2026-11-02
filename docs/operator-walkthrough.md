# Update OpenCDR: SOC operator walkthrough

Use this guide to install a new OpenCDR release or change its archive, tracing or
resource-protection settings. You run the update from your deployment workstation.
The platform helps you prepare the file and instructions; it does not update AWS.

**If you downloaded instructions from the platform, use their commands.** They
already contain your account, profile, filename and installation name. The worked
example below uses these example values, not a recommendation to use this account
or release:

| Item | Example | Meaning |
| --- | --- | --- |
| AWS account | `111111111111` | Account containing the OpenCDR deployment |
| AWS profile | `company-dev` | Saved AWS login configuration on your computer |
| Installation name | `dev` | Name opencdr-deploy uses to remember this deployment locally |
| Region | `us-east-1` | AWS region containing the deployment |
| Stage | `dev` | Existing Core stage; the AWS stack is `opencdr-dev` |
| Release | `v1.2.1` | Example version; use the release specified for your change |

## Before you start

Have these ready:

- Your team's deployment workstation. It needs Git, AWS CLI v2, Node/npm,
  Python 3.12, Docker and jq. Start Docker before building. Your team must also
  supply any Serverless login required by the Core release.
- A company AWS profile allowed to deploy OpenCDR in the target account. A
  read-only SOC account is not sufficient. Ask the deployment owner for the right
  profile/SSO access if you do not have it.
- The OpenCDR version and feature choices for this change.

The tool downloads OpenCDR Core; you do not need a separate Core clone.
Do not paste AWS credentials or API keys into the platform or the configuration.

Commands below use **macOS/Linux or Windows WSL** terminal syntax. Native Windows
shell commands differ; use your team's supported deployment environment.

## First time on this computer: install the manager

Open Terminal and run these commands one at a time. Python 3.11 or newer must be
available for the manager; building Core separately requires Python 3.12.

```bash
python3 -m venv "$HOME/.opencdr-tools"
. "$HOME/.opencdr-tools/bin/activate"
python -m pip install --upgrade 'opencdr-deploy>=0.5.0'
opencdr-deploy --version
```

Expected: the last line prints `opencdr-deploy 0.5.0` or newer. In a new terminal,
activate it again before using the commands:

```bash
. "$HOME/.opencdr-tools/bin/activate"
```

If Python or another required program is missing, ask the workstation owner to
install it. Do not continue to deployment with failed prerequisites.

## Prepare the change in the platform

1. Run `aws configure list-profiles` in Terminal. Find the profile your cloud team
   assigned to the target account. For this example, that is `company-dev`.
2. Run `opencdr-deploy installations list`. If this account/region/stage is already
   listed, note its `name`. For this example, that is `dev`. An empty list is normal
   the first time; keep the suggested name in the platform instead.
3. Open **Deployment settings** in the platform. Select the deployment to update.
4. Enter the AWS profile name and the name in opencdr-deploy from the previous
   steps. The profile and installation name are different concepts; neither is
   an AWS access key.
5. Enter the release version supplied for your change, including the leading `v`.
   Use a published release tag, not `latest`, a branch, a URL or a commit SHA.
6. Review **all** feature choices. The switches describe the settings to deploy;
   they are not automatically filled from the running stack. To keep a feature,
   choose its current setting. Ask the deployment owner if that setting is unknown.
7. Select **Generate configuration and instructions**. Download both files to your
   **Downloads** folder. The configuration filename includes a unique suffix so a
   previous download is less likely to be mistaken for this change.

Generating these files does not change AWS. The same choices apply to all selected
deployments. If deployments need different settings, generate separate files.

## Apply the change from Terminal

The following example uses a file named `opencdr-change-1234abcd.toml` and an
installation named `dev`. Your downloaded instructions use your actual names.
Run one step at a time and check its expected result before continuing.

### 1. Open the download folder

```bash
cd "$HOME/Downloads"
ls -l opencdr-change-1234abcd.toml
```

Expected: that exact file is listed. If not, move the downloaded file into
Downloads or open the terminal in the folder where you saved it. Do not deploy
using a similarly named old file. If the browser renamed a duplicate download,
make the filename match the one in your downloaded instructions.

### 2. Sign in and check the AWS account

For company AWS SSO:

```bash
aws sso login --profile company-dev
aws sts get-caller-identity --profile company-dev --query Account --output text
```

Complete the browser sign-in. Expected: the identity command prints
`111111111111` for this example. **Stop if the account does not match the platform's
selected deployment.** If your organization uses a different authentication
method, sign in using that method and still run the identity check.

For more than one AWS profile, repeat the login/account check for each profile.
The downloaded instructions provide the commands and expected account IDs.

### 3. Preview the requested configuration

```bash
opencdr-deploy apply --file opencdr-change-1234abcd.toml --dry-run
```

Expected: no `FAIL` or `ERROR`. Review the release, account, region, stage and
feature choices in the downloaded file.

This preview changes neither the local registry nor AWS resources. It shows the
local configuration/source work the manager would perform. **It is not a live AWS
permission check or a CloudFormation resource-change preview.** Source/release
availability can still fail when the actual download runs.

### 4. Download Core and register it locally

```bash
opencdr-deploy apply --file opencdr-change-1234abcd.toml
```

Expected: `PASS` for each installation and an empty `failed` list. This downloads
the selected Core release and saves local installation settings. **It does not
deploy AWS resources.**

If you already registered this AWS deployment under another local name, use that
name in the platform and download a corrected file. Version 0.5.0 adds checks
against redirecting an existing name or registering the same known deployment
twice. If you have an older manager, upgrade it before continuing.

### 5. Check your workstation

```bash
opencdr-deploy --deployment dev doctor
```

Expected: all required checks say `PASS`, including AWS account identity. A
missing program or failed login must be resolved first. This check does not
certify every permission needed for deployment. For multiple installations, run
the command for each name; the downloaded instructions list them.

### 6. Deploy — this step changes AWS

```bash
opencdr-deploy apply --file opencdr-change-1234abcd.toml --prepare --deploy
```

This installs the selected release's dependencies, builds its package, and applies
the settings in AWS. Archival, tracing and retained resources can add AWS costs.
Keep the terminal open and allow the build/deployment to finish.

Expected: each selected installation reports `PASS`, `failed` is empty and the
command exits successfully. The tool checks the expected AWS account before
deployment. If a later account fails, earlier successful updates remain applied;
the batch is not automatically rolled back.

### 7. Verify the result

```bash
opencdr-deploy --deployment dev status
```

Check:

- `account`, `stage` and `region` match the deployment you selected.
- `stack_status` is `UPDATE_COMPLETE` or `CREATE_COMPLETE`.
- `deployed_archive_enabled`, `deployed_xray_enabled` and
  `deployed_security_profile` match the requested choices.
- Version 0.5.0 also reports `deployed_core_version` where available, and shows
  missing outputs as `null`. `null` means **not reported**, not a successful match.
  Earlier versions can assume feature defaults when stack outputs are missing;
  ask the deployment owner to verify an older release's outputs before closing
  the change.

For multiple deployments, check each installation. Keep the file and final
status output with your change record. This confirms infrastructure deployment;
testing detections and notification delivery is a separate operational check.

## If something fails

| Symptom | What to do |
| --- | --- |
| `opencdr-deploy: command not found` | Activate the Python environment using the command above. |
| Profile not found | Run `aws configure list-profiles`; ask your cloud team for the correct company profile/SSO setup. |
| SSO/token expired | Repeat SSO login and check the printed account ID. |
| Wrong AWS account | Stop. Correct the profile in the platform and generate a new file. |
| Missing Git, Node, Python 3.12 or jq | Ask the workstation owner to install the required program, then rerun `doctor`. |
| Docker unavailable | Start Docker, then retry the build after resolving the error. |
| AccessDenied | Give the deployment owner the failed AWS operation/error. Do not switch accounts or invent administrator permissions. |
| Version/tag not found | Confirm the published release tag with the deployment owner. Do not substitute `latest` or a branch. |
| Installation name conflict | Use the name already listed for that account/region/stage, then download a corrected file. |
| Protection downgrade blocked | Have the deployment owner review the requested change. Do not add a bypass flag to make it pass. |
| Deployment failed or stack is rolling back | Stop and have the deployment owner inspect CloudFormation before retrying. A retry is not a rollback. |

After a multi-account failure, check each account's status. Once the cause is
resolved, retry only the failed installation, for example:

```bash
opencdr-deploy apply --file opencdr-change-1234abcd.toml --select dev --prepare --deploy
```

Use your actual failed installation name. Do not rerun successful accounts
unnecessarily. Do not include credentials or API keys in error reports.

## For the cloud/platform team

Provide a ready workstation and an AWS profile with permissions for the intended
OpenCDR stack/resources, deployment-artifact publishing and required scoped
`iam:PassRole`. The exact permission policy follows the selected Core template
and any existing CloudFormation service role. A SOC read-only permission set is
not a deployment permission set. This guide does not provision account controls,
change API exposure, create credentials or grant the hosted platform AWS access.

AWS reference: [company SSO login and account checks](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html).
