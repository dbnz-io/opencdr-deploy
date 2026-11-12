"""Scoped CloudTrail data-event metering, planned through CloudFormation/Deploy.

Observes exact customer-owned resources without changing engine functions, queues,
permissions or recovery guards. Logging is stopped with a separately reviewed plan.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import boto3

from .detection_activation import resource_ids, template_body
from .fleet_api import FleetManagerError, aws_client, verify_account
from .storage_quarantine import require_unlocked


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def configure(commands):
    p = commands.add_parser("usage-telemetry", help="plan scoped request metering; no engine edits")
    p.add_argument("action", choices=["plan", "stop-plan", "restart-plan"])
    p.add_argument("--profile", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--stack", required=True, help="exact existing engine stack")
    p.add_argument("--run-id", required=True, help="8-24 lowercase hex characters")
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--original-plan", type=Path)
    p.add_argument("--original-sha256")


def scope(session, stack, account, region):
    cf = aws_client(session, "cloudformation")
    current = cf.describe_stacks(StackName=stack)["Stacks"][0]
    if current["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        raise FleetManagerError("Metering requires a settled engine stack")
    require_unlocked(session, current)
    body = template_body(cf, StackName=stack)
    ids = resource_ids(cf, stack, body)
    rows = body["Resources"]
    tables, buckets, queues = set(), set(), set()
    for logical, resource in rows.items():
        physical = ids.get(logical)
        if not isinstance(physical, str):
            continue
        kind = resource["Type"]
        if kind == "AWS::DynamoDB::Table":
            tables.add(f"arn:aws:dynamodb:{region}:{account}:table/{physical}")
        elif kind == "AWS::S3::Bucket":
            buckets.add("arn:aws:s3:::" + physical + "/")
        elif kind == "AWS::SQS::Queue":
            parsed = urlsplit(physical)
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != account:
                raise FleetManagerError("Queue identity differs from selected account")
            queues.add(f"arn:aws:sqs:{region}:{account}:{parts[1]}")
    # Restored operational resources are parameter-bound; resolve their exact identities.
    from .storage_bindings import current_binding

    binding = current_binding(body)
    if binding:
        for item in binding["resources"].values():
            kind = item["type"]
            if kind == "AWS::DynamoDB::Table":
                tables.add(f"arn:aws:dynamodb:{region}:{account}:table/{item['name']}")
            elif kind == "AWS::S3::Bucket":
                buckets.add("arn:aws:s3:::" + item["name"] + "/")
    if not tables or not buckets or not queues:
        raise FleetManagerError("Complete engine table/bucket/queue inventory is required")
    return {
        "stack_id": current["StackId"],
        "change_set_id": current.get("ChangeSetId"),
        "template_sha256": digest(body),
        "tables": sorted(tables),
        "bucket_prefixes": sorted(buckets),
        "queues": sorted(queues),
    }


def template(account, region, run_id, selected, *, logging=True):
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"[a-f0-9]{8,24}", run_id):
        raise FleetManagerError("Exact account and 8-24 character hexadecimal run ID required")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", region):
        raise FleetManagerError("Invalid AWS region")
    if region.startswith("us-gov-") or not region.startswith(
        ("us-", "eu-", "ap-", "ca-", "sa-", "me-", "af-", "il-", "mx-")
    ):
        raise FleetManagerError("Only the commercial AWS partition is supported")
    selectors = []
    for title, kind, key, op in [
        ("EngineTables", "AWS::DynamoDB::Table", "tables", "Equals"),
        ("EngineObjects", "AWS::S3::Object", "bucket_prefixes", "StartsWith"),
        ("EngineQueues", "AWS::SQS::Queue", "queues", "Equals"),
    ]:
        values = selected[key]
        if not isinstance(values, list) or not values or len(values) > 100:
            raise FleetManagerError("Bounded nonempty resource selection required")
        for value in values:
            pattern = (
                r"arn:aws:s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/"
                if key == "bucket_prefixes"
                else rf"arn:aws:{'sqs' if key == 'queues' else 'dynamodb'}:{region}:{account}:"
                + (r"[A-Za-z0-9_-]+(?:\.fifo)?" if key == "queues" else r"table/[A-Za-z0-9_.-]+")
            )
            if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
                raise FleetManagerError("Resource escapes selected account/region/type")
        selectors.append(
            {
                "Name": title,
                "FieldSelectors": [
                    {"Field": "eventCategory", "Equals": ["Data"]},
                    {"Field": "resources.type", "Equals": [kind]},
                    {"Field": "resources.ARN", op: sorted(set(values))},
                ],
            }
        )
    name = "opencdr-usage-" + run_id
    bucket = f"opencdr-usage-{account}-{region}-{run_id}"
    arn = f"arn:aws:cloudtrail:{region}:{account}:trail/{name}"
    source = {"StringEquals": {"aws:SourceArn": arn}}
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "OpenCDR scoped engine request measurement; retained audit logs",
        "Metadata": {"OpenCDRUsage": {"schema": "1", "source": selected}},
        "Resources": {
            "AuditBucket": {
                "Type": "AWS::S3::Bucket",
                "DeletionPolicy": "Retain",
                "UpdateReplacePolicy": "Retain",
                "Properties": {
                    "BucketName": bucket,
                    "PublicAccessBlockConfiguration": {
                        k: True
                        for k in [
                            "BlockPublicAcls",
                            "BlockPublicPolicy",
                            "IgnorePublicAcls",
                            "RestrictPublicBuckets",
                        ]
                    },
                    "BucketEncryption": {
                        "ServerSideEncryptionConfiguration": [
                            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                        ]
                    },
                    "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
                    "VersioningConfiguration": {"Status": "Enabled"},
                    "LifecycleConfiguration": {
                        "Rules": [
                            {
                                "Id": "BoundedAuditRetention",
                                "Status": "Enabled",
                                "ExpirationInDays": 30,
                                "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                            }
                        ]
                    },
                },
            },
            "AuditBucketPolicy": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {
                    "Bucket": {"Ref": "AuditBucket"},
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Deny",
                                "Principal": "*",
                                "Action": "s3:*",
                                "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
                                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                            },
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "cloudtrail.amazonaws.com"},
                                "Action": "s3:GetBucketAcl",
                                "Resource": f"arn:aws:s3:::{bucket}",
                                "Condition": source,
                            },
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "cloudtrail.amazonaws.com"},
                                "Action": "s3:PutObject",
                                "Resource": f"arn:aws:s3:::{bucket}/AWSLogs/{account}/*",
                                "Condition": {
                                    "StringEquals": {
                                        "aws:SourceArn": arn,
                                        "s3:x-amz-acl": "bucket-owner-full-control",
                                    }
                                },
                            },
                        ],
                    },
                },
            },
            "RequestTrail": {
                "Type": "AWS::CloudTrail::Trail",
                "DependsOn": "AuditBucketPolicy",
                "Properties": {
                    "TrailName": name,
                    "S3BucketName": {"Ref": "AuditBucket"},
                    "IsLogging": logging,
                    "IsMultiRegionTrail": False,
                    "IncludeGlobalServiceEvents": False,
                    "EnableLogFileValidation": True,
                    "AdvancedEventSelectors": selectors,
                },
            },
        },
        "Outputs": {"AuditBucket": {"Value": {"Ref": "AuditBucket"}}, "TrailArn": {"Value": arn}},
    }


def run(args):
    if args.output.exists():
        raise FleetManagerError("Evidence output exists; inspect before retrying")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    verify_account(session, args.account_id)
    selected = None
    document = None
    name = "opencdr-usage-" + args.run_id
    cf = aws_client(session, "cloudformation")
    kind = "CREATE"
    predecessor = None
    if args.action in {"stop-plan", "restart-plan"}:
        if not args.original_plan or not args.original_sha256:
            raise FleetManagerError(
                "Telemetry transitions require the exact original plan and SHA256"
            )
        raw = args.original_plan.read_bytes()
        if hashlib.sha256(raw).hexdigest() != args.original_sha256:
            raise FleetManagerError("Original telemetry plan SHA256 mismatch")
        original = json.loads(raw)
        selected = original["source"]
        expected_stack = (
            f"arn:aws:cloudformation:{args.region}:{args.account_id}:stack/{args.stack}/"
        )
        if not selected.get("stack_id", "").startswith(expected_stack):
            raise FleetManagerError("Source stack identity differs from reviewed plan")
        if (
            args.action == "restart-plan"
            and scope(session, args.stack, args.account_id, args.region) != selected
        ):
            raise FleetManagerError("Engine inventory changed; prepare a new measurement scope")
        document = template(args.account_id, args.region, args.run_id, selected)
        if any(
            original.get(k) != v
            for k, v in {
                "account_id": args.account_id,
                "region": args.region,
                "run_id": args.run_id,
                "source": selected,
                "template": document,
            }.items()
        ):
            raise FleetManagerError("Source identity or reviewed telemetry plan changed")
        before = cf.describe_stacks(StackName=name)["Stacks"][0]
        if before["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
            raise FleetManagerError("Telemetry stack must be settled")
        predecessor = before.get("ChangeSetId")
        if not predecessor:
            raise FleetManagerError("Telemetry predecessor change-set identity required")
        document["Resources"]["RequestTrail"]["Properties"]["IsLogging"] = (
            args.action == "stop-plan"
        )
        live = template_body(cf, StackName=name)
        if digest(live) != digest(document):
            raise FleetManagerError("Telemetry stack differs from reviewed template")
        document["Resources"]["RequestTrail"]["Properties"]["IsLogging"] = (
            args.action == "restart-plan"
        )
        kind = "UPDATE"
    elif args.original_plan or args.original_sha256:
        raise FleetManagerError("Original plan only applies to telemetry transitions")
    if document is None:
        selected = scope(session, args.stack, args.account_id, args.region)
        document = template(args.account_id, args.region, args.run_id, selected)
    request_id = digest(
        {
            "template": document,
            "kind": kind,
            "executor": "opencdr-release-v1",
            "predecessor": predecessor,
        }
    )
    intent = {
        "account_id": args.account_id,
        "region": args.region,
        "stack_name": name,
        "request_id": request_id,
        "change_set_name": "opencdr-meter-" + request_id[:32],
        "template_sha256": digest(document),
        "kind": kind,
        "predecessor": predecessor,
    }
    intent_path = args.output.with_suffix(".intent.json")
    if intent_path.exists():
        if json.loads(intent_path.read_text()) != intent:
            raise FleetManagerError("Existing metering intent differs; reconcile before retry")
    else:
        with intent_path.open("x") as stream:
            stream.write(json.dumps(intent, indent=2) + "\n")
    result = cf.create_change_set(
        StackName=name,
        ChangeSetName="opencdr-meter-" + request_id[:32],
        ChangeSetType=kind,
        ClientToken=request_id,
        TemplateBody=json.dumps(document),
        Description="Scoped OpenCDR data-event metering; no engine runtime changes",
        Tags=[{"Key": "opencdr-purpose", "Value": "engine-acceptance-metering"}],
    )
    value = {
        "created_at": datetime.now(UTC).isoformat(),
        "account_id": args.account_id,
        "region": args.region,
        "run_id": args.run_id,
        "source": selected,
        "template": document,
        "template_sha256": digest(document),
        "change_set_arn": result["Id"],
        "stack_id": result["StackId"],
        "applied": False,
        "action": args.action,
        "execution": "Independently review, then use opencdr-deploy release execute",
    }
    with args.output.open("x") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
    print(
        json.dumps(
            {
                "plan": str(args.output),
                "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                "change_set_arn": result["Id"],
                "applied": False,
            }
        )
    )
    return 0
