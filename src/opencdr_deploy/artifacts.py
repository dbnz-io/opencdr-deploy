"""Install reviewed CloudFormation releases without Node, Docker or a source checkout."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request

import boto3
from botocore.exceptions import ClientError

from .fleet_api import FleetManagerError, aws_client, open_request, verify_account

PRODUCTS = {"opencdr", "opencdr-fleet-manager"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_release(path, expected_digest):
    """A digest obtained from the reviewed release pins the entire dependency graph."""
    root = Path(path).resolve()
    raw = (root / "release.json").read_bytes()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_digest or "") or sha256(raw) != expected_digest:
        raise FleetManagerError("Release manifest digest mismatch")
    manifest = json.loads(raw)
    if (
        manifest.get("schema") != "opencdr.deployment/1"
        or manifest.get("product") not in PRODUCTS
        or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", manifest.get("version", ""))
        or manifest.get("management_contract_major")
        != {"opencdr": 2, "opencdr-fleet-manager": 1}.get(manifest.get("product"))
    ):
        raise FleetManagerError("Unsupported deployment release")
    files = manifest.get("files", {})
    if set(files) != {"template.json", "runtime.zip"}:
        raise FleetManagerError("Release must contain exactly template.json and runtime.zip")
    for name, expected in files.items():
        file = root / name
        if file.is_symlink() or sha256(file.read_bytes()) != expected:
            raise FleetManagerError(f"Release file digest mismatch: {name}")
    if not zipfile.is_zipfile(root / "runtime.zip"):
        raise FleetManagerError("Release runtime must be a Lambda ZIP archive")
    template = json.loads((root / "template.json").read_text())
    if "DetectionFeedTable" in template.get("Resources", {}):
        from .detection_activation import guarded, runtime_functions

        if not guarded(template) or any(
            p["Environment"]["Variables"].get("DETECTION_RUNTIME_ID") != files["runtime.zip"]
            for p in runtime_functions(template).values()
        ):
            raise FleetManagerError("Core feed release lacks a guard bound to its runtime digest")
    required = {
        "Stage",
        "ArtifactBucket",
        "ArtifactKey",
        "LegacyApiEnabled",
        "ManagementPrincipalArns",
        "PreserveRoleNames",
    }
    if not required <= set(template.get("Parameters", {})):
        raise FleetManagerError("Deployment template is missing installation parameters")
    from .ownership_contract import verify_template

    verify_template(template, (root / "runtime.zip").read_bytes(), manifest["product"])
    return manifest, template


def download_release(url, expected_digest, destination):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/release.json")
    ):
        raise FleetManagerError("Use an HTTPS release.json URL without credentials or query")
    destination = Path(destination)
    if destination.exists():
        raise FleetManagerError("Download destination must be new")
    destination.mkdir(parents=True)
    for name, limit in (
        ("release.json", 64_000),
        ("template.json", 1_000_000),
        ("runtime.zip", 250_000_000),
    ):
        request = Request(url.rsplit("/", 1)[0] + "/" + name)
        with open_request(request, timeout=60) as response:
            if response.code != 200:
                raise FleetManagerError("Release download failed")
            data = response.read(limit + 1)
        if len(data) > limit:
            raise FleetManagerError("Release file exceeds its size limit")
        (destination / name).write_bytes(data)
        if name == "release.json" and sha256(data) != expected_digest:
            raise FleetManagerError("Release manifest digest mismatch")
    return load_release(destination, expected_digest)


def prepare_change(args, session=None):
    manifest, template = load_release(args.directory, args.sha256)
    replay_days = getattr(args, "detection_replay_days", None)
    if replay_days is not None and (
        manifest["product"] != "opencdr"
        or "DetectionReplayDays" not in template["Parameters"]
        or type(replay_days) is not int
        or not 7 <= replay_days <= 30
    ):
        raise FleetManagerError(
            "Detection replay requires a compatible Core artifact and 7–30 days"
        )
    polling = {
        key: getattr(args, flag, None)
        for key, flag in (
            ("PollIntervalMinutes", "poll_interval_minutes"),
            ("FullRefreshMinutes", "full_refresh_minutes"),
        )
    }
    for key, value in polling.items():
        if value is not None and (
            manifest["product"] != "opencdr-fleet-manager"
            or key not in template["Parameters"]
            or not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= (60 if key == "PollIntervalMinutes" else 1440)
        ):
            raise FleetManagerError(
                "Polling options require a compatible Fleet artifact and valid intervals"
            )
    detection_options = {}
    for key, flag, low, high in (
        ("DetectionPollIntervalMinutes", "detection_poll_interval_minutes", 1, 60),
        ("DetectionRetentionDays", "detection_retention_days", 7, 365),
        ("DetectionPollConcurrency", "detection_poll_concurrency", 2, 1000),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            if (
                manifest["product"] != "opencdr-fleet-manager"
                or key not in template["Parameters"]
                or type(value) is not int
                or not low <= value <= high
            ):
                raise FleetManagerError(
                    f"{flag} requires a compatible Fleet artifact and {low}–{high}"
                )
            detection_options[key] = str(value)
    enabled = getattr(args, "detection_polling_enabled", None)
    if enabled is not None:
        if (
            manifest["product"] != "opencdr-fleet-manager"
            or "DetectionPollingEnabled" not in template["Parameters"]
            or type(enabled) is not bool
        ):
            raise FleetManagerError("Detection polling requires a compatible Fleet artifact")
        detection_options["DetectionPollingEnabled"] = str(enabled).lower()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,17}", args.stage):
        raise FleetManagerError("Stage must contain 1–18 lowercase letters, digits and hyphens")
    for arn in args.principal_arn or []:
        if not re.fullmatch(r"arn:aws(?:-us-gov|-cn)?:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+", arn):
            raise FleetManagerError("Management principals must be exact IAM role ARNs")
    session = session or boto3.Session(profile_name=args.profile, region_name=args.region)
    verify_account(session, args.account_id)
    cfn, s3 = aws_client(session, "cloudformation"), aws_client(session, "s3")
    stack = manifest["product"] + "-" + args.stage
    bucket = args.artifact_bucket
    # Bucket ownership is checked before uploading any code or template.
    s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=args.account_id)
    root = Path(args.directory)
    key = f"opencdr-releases/{manifest['product']}/{manifest['version']}/{args.sha256}"
    parameters = {
        "Stage": args.stage,
        "ArtifactBucket": bucket,
        "ArtifactKey": key + "/runtime.zip",
    }
    existing, source_named_roles = {}, False
    try:
        live = cfn.describe_stacks(StackName=stack)["Stacks"][0]
        from .storage_quarantine import require_unlocked

        require_unlocked(session, live)
        change_type = "UPDATE"
        # Keeping protected resources is a release gate, not an interactive prompt.
        live_template = cfn.get_template(StackName=stack)["TemplateBody"]
        if isinstance(live_template, str):
            live_template = json.loads(live_template)
        from .ownership_contract import require_compatible

        require_compatible(live_template, template)
        from .storage_bindings import apply_binding, current_binding, verify_physical

        binding = current_binding(live_template)
        if binding:
            if (binding["account_id"], binding["region"], binding["stage"]) != (
                args.account_id,
                args.region,
                args.stage,
            ):
                raise FleetManagerError("Recovered storage belongs to another deployment")
            verify_physical(session, binding)
            template = apply_binding(
                template,
                binding,
                parameters={
                    **{p["ParameterKey"]: p["ParameterValue"] for p in live.get("Parameters", [])},
                    **parameters,
                },
            )
            for logical in binding["resources"]:
                if (
                    template["Resources"][logical]["Properties"]
                    != live_template["Resources"][logical]["Properties"]
                ):
                    raise FleetManagerError(
                        "Recovered storage schema changes need a separate migration"
                    )
        if manifest["product"] == "opencdr":
            from .detection_activation import require_paused

            require_paused(session, stack, live_template)
        source_named_roles = any(
            resource.get("Type") == "AWS::IAM::Role"
            and "RoleName" in resource.get("Properties", {})
            for resource in live_template.get("Resources", {}).values()
        )
        for name, resource in live_template.get("Resources", {}).items():
            if resource.get("Type") == "AWS::Lambda::Version":
                # Published versions have content-derived logical IDs. Retiring
                # one from the stack retains it; a new runtime adds a new version.
                continue
            if resource.get("DeletionPolicy") == "Retain":
                new = template.get("Resources", {}).get(name)
                if (
                    not new
                    or new.get("DeletionPolicy") != "Retain"
                    or resource.get("UpdateReplacePolicy") == "Retain"
                    and new.get("UpdateReplacePolicy") != "Retain"
                ):
                    raise FleetManagerError(
                        f"Release removes protection from retained resource {name}"
                    )
        if live_template.get("Conditions", {}).get("Hardened") == {
            "Fn::Equals": ["hardened", "hardened"]
        } and template.get("Conditions", {}).get("Hardened") != {
            "Fn::Equals": ["hardened", "hardened"]
        }:
            raise FleetManagerError("Use a hardened release to preserve existing protection")
        existing = {p["ParameterKey"]: p["ParameterValue"] for p in live.get("Parameters", [])}
        if manifest["product"] == "opencdr-fleet-manager":
            from .fleet_recovery import IMPORT_DESCRIPTION, pinned_bucket_name

            if live_template.get("Description") == IMPORT_DESCRIPTION:
                raise FleetManagerError(
                    "First recovery runtime must use a reviewed 'fleet package' and "
                    "'fleet deploy' to preserve all retained properties"
                )
            pinned = pinned_bucket_name(live_template, existing)
            if pinned:
                if "DetectionEvidenceBucketName" not in template["Parameters"]:
                    raise FleetManagerError(
                        "Release cannot preserve the recovered evidence bucket; "
                        "use a compatible artifact"
                    )
                parameters["DetectionEvidenceBucketName"] = pinned
                if pinned_bucket_name(template, {**existing, **parameters}) != pinned:
                    raise FleetManagerError("Release changes the retained evidence bucket binding")
        # Existing optional infrastructure values survive upgrades.
        parameters = {
            **{k: v for k, v in existing.items() if k in template["Parameters"]},
            **parameters,
        }
    except ClientError as error:
        if error.response["Error"]["Code"] != "ValidationError" or "does not exist" not in str(
            error
        ):
            raise
        change_type = "CREATE"
    parameters["LegacyApiEnabled"] = (
        existing.get("LegacyApiEnabled", "false")
        if args.legacy_api is None
        else "true"
        if args.legacy_api
        else "false"
    )
    parameters["ManagementPrincipalArns"] = (
        existing.get("ManagementPrincipalArns", "")
        if args.principal_arn is None
        else ",".join(args.principal_arn)
    )
    parameters["PreserveRoleNames"] = existing.get(
        "PreserveRoleNames", "true" if source_named_roles else "false"
    )
    parameters.update({key: str(value) for key, value in polling.items() if value is not None})
    parameters.update(detection_options)
    if replay_days is not None:
        parameters["DetectionReplayDays"] = str(replay_days)
    if "DetectionReplayDays" in template["Parameters"]:
        retained_replay = str(
            parameters.get(
                "DetectionReplayDays",
                template["Parameters"]["DetectionReplayDays"].get("Default", 7),
            )
        )
        if not retained_replay.isdigit() or not 7 <= int(retained_replay) <= 30:
            raise FleetManagerError("Detection replay retention must remain between 7 and 30 days")
    if set(polling) <= template["Parameters"].keys():
        resolved = {
            key: int(parameters.get(key, template["Parameters"][key]["Default"])) for key in polling
        }
        if not 1 <= resolved["PollIntervalMinutes"] <= 60 or not (
            resolved["PollIntervalMinutes"] <= resolved["FullRefreshMinutes"] <= 1440
        ):
            raise FleetManagerError(
                "Full refresh must be at least the poll interval and at most 1440 minutes"
            )
    for name in ("runtime.zip", "template.json", "release.json"):
        s3.put_object(
            Bucket=bucket,
            Key=key + "/" + name,
            Body=(root / name).read_bytes(),
            ExpectedBucketOwner=args.account_id,
            ServerSideEncryption="AES256",
        )
    from .deployment_template import digest, materialize

    deployed_template = materialize(template, parameters)
    deployed_digest = digest(deployed_template)
    deployed_key = key + "/deployments/" + deployed_digest + ".json"
    s3.put_object(
        Bucket=bucket,
        Key=deployed_key,
        Body=json.dumps(deployed_template, sort_keys=True, separators=(",", ":")).encode(),
        ExpectedBucketOwner=args.account_id,
        ServerSideEncryption="AES256",
    )
    name = "opencdr-" + uuid.uuid4().hex
    suffix = "amazonaws.com.cn" if args.region.startswith("cn-") else "amazonaws.com"
    result = cfn.create_change_set(
        StackName=stack,
        ChangeSetName=name,
        ChangeSetType=change_type,
        TemplateURL=f"https://{bucket}.s3.{args.region}.{suffix}/{deployed_key}",
        Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()],
        Capabilities=["CAPABILITY_NAMED_IAM"],
        Description=f"Reviewed {manifest['product']} {manifest['version']} {args.sha256}",
    )
    cfn.get_waiter("change_set_create_complete").wait(
        ChangeSetName=result["Id"], WaiterConfig={"Delay": 5, "MaxAttempts": 60}
    )
    review = cfn.describe_change_set(ChangeSetName=result["Id"])
    # Changes can be paginated; preserve every resource change in the review.
    changes = list(review.get("Changes", []))
    while review.get("NextToken"):
        review = cfn.describe_change_set(ChangeSetName=result["Id"], NextToken=review["NextToken"])
        changes.extend(review.get("Changes", []))
    return {
        "change_set_arn": result["Id"],
        "stack": stack,
        "changes": changes,
        "release_sha256": args.sha256,
        "deployment_template_sha256": deployed_digest,
        "executed": False,
    }


def prepare_bootstrap(args, session=None):
    """Prepare the customer-owned artifact bucket through the same review flow."""
    if not re.fullmatch(r"\d{12}", args.account_id) or not re.fullmatch(
        r"[a-z]{2}(?:-[a-z]+)+-\d+", args.region
    ):
        raise FleetManagerError("Use a valid AWS account ID and region")
    session = session or boto3.Session(profile_name=args.profile, region_name=args.region)
    verify_account(session, args.account_id)
    cfn = aws_client(session, "cloudformation")
    bucket = f"opencdr-artifacts-{args.account_id}-{args.region}"
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Customer-owned OpenCDR release artifacts",
        "Resources": {
            "ArtifactBucket": {
                "Type": "AWS::S3::Bucket",
                "DeletionPolicy": "Retain",
                "UpdateReplacePolicy": "Retain",
                "Properties": {
                    "BucketName": bucket,
                    "BucketEncryption": {
                        "ServerSideEncryptionConfiguration": [
                            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                        ]
                    },
                    "VersioningConfiguration": {"Status": "Enabled"},
                    "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
                    "PublicAccessBlockConfiguration": {
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": True,
                    },
                },
            },
            "ArtifactBucketPolicy": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {
                    "Bucket": {"Ref": "ArtifactBucket"},
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Deny",
                                "Principal": "*",
                                "Action": "s3:*",
                                "Resource": [
                                    {"Fn::GetAtt": ["ArtifactBucket", "Arn"]},
                                    {"Fn::Sub": "${ArtifactBucket.Arn}/*"},
                                ],
                                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                            }
                        ],
                    },
                },
            },
        },
        "Outputs": {"ArtifactBucketName": {"Value": {"Ref": "ArtifactBucket"}}},
    }
    stack = "opencdr-artifacts-" + args.region
    result = cfn.create_change_set(
        StackName=stack,
        ChangeSetName="opencdr-bootstrap-" + uuid.uuid4().hex,
        ChangeSetType="CREATE",
        TemplateBody=json.dumps(template),
        Description="Create a private, encrypted, retained OpenCDR artifact bucket",
    )
    cfn.get_waiter("change_set_create_complete").wait(
        ChangeSetName=result["Id"], WaiterConfig={"Delay": 5, "MaxAttempts": 60}
    )
    review = cfn.describe_change_set(ChangeSetName=result["Id"])
    return {
        "change_set_arn": result["Id"],
        "stack": stack,
        "artifact_bucket": bucket,
        "changes": review.get("Changes", []),
        "executed": False,
    }


def configure_parser(commands):
    release = commands.add_parser("release", help="install prebuilt CloudFormation releases")
    actions = release.add_subparsers(dest="release_command", required=True)
    from .detection_activation import configure_parser as activation_parser
    from .storage_recovery import configure_parser as recovery_parser

    activation_parser(actions)
    recovery_parser(actions)
    bootstrap = actions.add_parser("bootstrap", help="prepare a private artifact bucket once")
    bootstrap.add_argument("--profile", required=True)
    bootstrap.add_argument("--account-id", required=True)
    bootstrap.add_argument("--region", default="us-east-1")
    export = actions.add_parser(
        "export", help="maintainer: export an audited compiled template and ZIP"
    )
    export.add_argument("template", type=Path)
    export.add_argument("runtime", type=Path)
    export.add_argument("directory", type=Path)
    export.add_argument("--product", choices=sorted(PRODUCTS), required=True)
    export.add_argument("--version", required=True)
    export.add_argument("--stage", required=True)
    export.add_argument("--region", required=True)
    export.add_argument("--account-id", required=True)
    connect = actions.add_parser("connection", help="write a secret-free Core CLI/MCP connection")
    connect.add_argument("file", type=Path)
    connect.add_argument("--url", required=True)
    connect.add_argument("--profile", required=True)
    connect.add_argument("--account-id", required=True)
    connect.add_argument("--region", default="us-east-1")
    connect.add_argument("--stage", required=True)
    for field in ("issuer", "tenant", "subject"):
        connect.add_argument("--identity-" + field)
    connect.add_argument(
        "--access",
        choices=[
            "observer",
            "delivery",
            "triage",
            "operator",
            "responder",
            "administrator",
            "integration",
        ],
        default="operator",
    )
    flags = actions.add_parser(
        "flags", help="inspect typed infrastructure parameters in a pinned release; no AWS calls"
    )
    flags.add_argument("directory", type=Path)
    flags.add_argument("--sha256", required=True)
    verify = actions.add_parser("verify")
    verify.add_argument("directory", type=Path)
    verify.add_argument("--sha256", required=True)
    download = actions.add_parser("download")
    download.add_argument("url")
    download.add_argument("directory", type=Path)
    download.add_argument("--sha256", required=True)
    plan = actions.add_parser(
        "plan", help="upload verified artifacts and prepare an AWS change set"
    )
    plan.add_argument("directory", type=Path)
    plan.add_argument("--sha256", required=True)
    plan.add_argument("--profile", required=True)
    plan.add_argument("--account-id", required=True)
    plan.add_argument("--region", default="us-east-1")
    plan.add_argument("--stage", required=True)
    plan.add_argument("--artifact-bucket", required=True)
    plan.add_argument("--principal-arn", action="append")
    plan.add_argument(
        "--poll-interval-minutes", type=int, help="Fleet health/configuration polling"
    )
    plan.add_argument("--full-refresh-minutes", type=int, help="Fleet full configuration refresh")
    plan.add_argument("--detection-poll-interval-minutes", type=int)
    plan.add_argument("--detection-retention-days", type=int)
    plan.add_argument("--detection-poll-concurrency", type=int)
    detection_polling = plan.add_mutually_exclusive_group()
    detection_polling.add_argument(
        "--enable-detection-polling", dest="detection_polling_enabled", action="store_true"
    )
    detection_polling.add_argument(
        "--pause-detection-polling", dest="detection_polling_enabled", action="store_false"
    )
    plan.set_defaults(detection_polling_enabled=None)
    plan.add_argument(
        "--detection-replay-days",
        type=int,
        help="Core durable detection replay retention (7–30 days)",
    )
    legacy = plan.add_mutually_exclusive_group()
    legacy.add_argument("--legacy-api", action="store_true", default=None)
    legacy.add_argument("--disable-legacy-api", dest="legacy_api", action="store_false")
    execute = actions.add_parser("execute", help="execute an exact reviewed change set")
    execute.add_argument("change_set_arn")
    execute.add_argument("--profile", required=True)
    execute.add_argument("--account-id", required=True)
    execute.add_argument("--region", default="us-east-1")


def flag_catalog(directory, manifest_sha256):
    manifest, template = load_release(directory, manifest_sha256)
    parameters = []
    for name, spec in sorted(template["Parameters"].items()):
        sensitive = str(spec.get("NoEcho", "false")).lower() == "true"
        allowed = spec.get("AllowedValues")
        kind = (
            "boolean"
            if isinstance(allowed, list) and set(map(str, allowed)) == {"true", "false"}
            else spec["Type"]
        )
        item = {
            "id": name,
            "type": kind,
            "sensitive": sensitive,
            "description": spec.get("Description", ""),
            "constraints": {
                k: v
                for k, v in spec.items()
                if k
                in {
                    "AllowedValues",
                    "AllowedPattern",
                    "MinValue",
                    "MaxValue",
                    "MinLength",
                    "MaxLength",
                }
            },
        }
        if not sensitive and "Default" in spec:
            item["default"] = spec["Default"]
        parameters.append(item)
    return {
        "schema_version": "1.0.0",
        "product": manifest["product"],
        "release_version": manifest["version"],
        "release_sha256": manifest_sha256,
        "application": "reviewed_deploy_plan",
        "authority": "deployment_iam",
        "parameters": parameters,
        "changes_aws": False,
        "workflow": [
            "release plan",
            "independent plan review",
            "release execute",
            "independent runtime and installation verification",
        ],
        "runtime_flags": (
            "Read /configuration/flags at the management owner; use reviewed API mutations."
        ),
    }


def run(args):
    if args.release_command == "flags":
        print(json.dumps(flag_catalog(args.directory, args.sha256), indent=2))
        return 0
    if args.release_command == "storage-recovery":
        from .storage_recovery import run as recovery_run

        return recovery_run(args)
    if args.release_command == "detection-activation":
        from .detection_activation import run as activation_run

        return activation_run(args)
    if args.release_command == "export":
        from .release_builder import export_release

        result = export_release(
            args.template,
            args.runtime,
            args.directory,
            product=args.product,
            version=args.version,
            stage=args.stage,
            region=args.region,
            account_id=args.account_id,
        )
    elif args.release_command == "connection":
        from .core_api import CoreAPI

        config = {
            "mode": "core",
            "auth_mode": "iam",
            "url": args.url,
            "aws_profile": args.profile,
            "access": args.access,
            "deployment": {
                "account_id": args.account_id,
                "home_region": args.region,
                "stage": args.stage,
            },
        }
        identity = {
            k: getattr(args, "identity_" + k, None) for k in ("issuer", "tenant", "subject")
        }
        if any(identity.values()):
            from .delegation import delegated_request

            try:
                delegated_request("GET", "/status", None, identity, config["deployment"])
            except ValueError as error:
                raise FleetManagerError(str(error)) from error
            config["identity"] = identity
        client = CoreAPI(config)
        report = client.verify()
        if "identity" in config:
            if report.get("capabilities", {}).get("trusted_identity") != "1.0.0":
                raise FleetManagerError("Core does not support trusted identity delegation")
            # Verify the subject grant too, using a bounded read with no side effect.
            code, _ = client.request("GET", "/case-access")
            if code != 200:
                raise FleetManagerError("The owner has not granted this delegated identity access")
        args.file.parent.mkdir(parents=True, exist_ok=True)
        with args.file.open("x") as destination:
            destination.write(json.dumps(config, indent=2) + "\n")
        args.file.chmod(0o600)
        result = {"connection": str(args.file), "verified": True}
    elif args.release_command == "verify":
        result = load_release(args.directory, args.sha256)[0]
    elif args.release_command == "download":
        result = download_release(args.url, args.sha256, args.directory)[0]
    elif args.release_command == "plan":
        result = prepare_change(args)
    elif args.release_command == "bootstrap":
        result = prepare_bootstrap(args)
    else:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        verify_account(session, args.account_id)
        if not args.change_set_arn.startswith(
            f"arn:aws:cloudformation:{args.region}:{args.account_id}:changeSet/opencdr-"
        ):
            raise FleetManagerError(
                "Use a reviewed change-set ARN in the selected account and region"
            )
        cfn = aws_client(session, "cloudformation")
        review = cfn.describe_change_set(ChangeSetName=args.change_set_arn)
        if review["Status"] != "CREATE_COMPLETE" or review["ExecutionStatus"] != "AVAILABLE":
            raise FleetManagerError("Change set is not available for execution")
        from .storage_quarantine import require_unlocked

        require_unlocked(session, cfn.describe_stacks(StackName=review["StackId"])["Stacks"][0])
        from .detection_activation import template_body
        from .storage_bindings import current_binding, verify_physical

        state = cfn.describe_stacks(StackName=review["StackId"])["Stacks"][0]
        from .ownership_contract import require_compatible, verify_change_runtime

        proposed_template = template_body(cfn, ChangeSetName=args.change_set_arn)
        if state["StackStatus"] != "REVIEW_IN_PROGRESS":
            require_compatible(template_body(cfn, StackName=review["StackId"]), proposed_template)
        verify_change_runtime(session, review, proposed_template)
        if state["StackStatus"] != "REVIEW_IN_PROGRESS":
            original = current_binding(template_body(cfn, StackName=review["StackId"]))
            proposed = current_binding(template_body(cfn, ChangeSetName=args.change_set_arn))
            if proposed != original:
                raise FleetManagerError("Storage binding changes require the recovery controller")
            if original:
                verify_physical(session, original)
        if review.get("StackName", "").startswith("opencdr-") and not review[
            "StackName"
        ].startswith("opencdr-fleet-manager-"):
            from .detection_activation import claim_deployment, guarded, template_body

            # A reviewed change set can sit pending while reads are reactivated.
            # Never rely on the planning-time check to authorize execution.
            live = cfn.describe_stacks(StackName=review["StackId"])["Stacks"][0]
            target = template_body(
                cfn, StackName=review["StackId"], ChangeSetName=args.change_set_arn
            )
            if "DetectionFeedTable" in target.get("Resources", {}) and not guarded(target):
                raise FleetManagerError(
                    "Reviewed change set would remove the Core read activation guard"
                )
            if live["StackStatus"] != "REVIEW_IN_PROGRESS":
                previous = template_body(cfn, StackName=review["StackId"])
                if "DetectionFeedTable" in previous.get(
                    "Resources", {}
                ) and "DetectionFeedTable" not in target.get("Resources", {}):
                    raise FleetManagerError(
                        "Reviewed change set would remove Core detection storage"
                    )
                claim_deployment(
                    session,
                    review["StackId"],
                    previous,
                    args.change_set_arn,
                )
        cfn.execute_change_set(ChangeSetName=args.change_set_arn)
        result = {
            "change_set_arn": args.change_set_arn,
            "stack_id": review["StackId"],
            "status": "started",
        }
    print(json.dumps(result, indent=2, default=str))
    return 0
