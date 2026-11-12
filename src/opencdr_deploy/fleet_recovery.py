"""Reviewed import of a retained Fleet storage set. Never copies or deletes data.

This reattaches existing resources, not a point-in-time restore. A saved plan
binds physical identities and configuration; partial outcomes resume that same
CloudFormation change set. Runtime polling remains quarantined after import.
"""

from __future__ import annotations

import hashlib
import json
import re

from botocore.exceptions import ClientError, WaiterError

from .fleet_api import FleetManagerError, aws_client, verify_account

TABLES = {
    "RegistryTable": "agents",
    "StatusTable": "status",
    "ConfigurationTable": "configuration",
    "RolloutTable": "rollouts",
    "DetectionTable": "detections",
}
VERSION = "opencdr.fleet-recovery/1"
IMPORT_DESCRIPTION = "Reattach retained OpenCDR Fleet storage; runtime workers remain paused"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(plan):
    return hashlib.sha256(
        canonical({k: v for k, v in plan.items() if k != "plan_digest"}).encode()
    ).hexdigest()


def _missing(error):
    return error.response["Error"].get("Code") == "ValidationError" and any(
        phrase in str(error).lower() for phrase in ("does not exist", "not exist", "not found")
    )


def existing_stack(session, stage):
    try:
        return aws_client(session, "cloudformation").describe_stacks(
            StackName=f"opencdr-fleet-manager-{stage}"
        )["Stacks"][0]
    except ClientError as error:
        if _missing(error):
            return None
        raise


def retained_tables(session, stage):
    result = {}
    dynamodb = aws_client(session, "dynamodb")
    for logical, suffix in TABLES.items():
        name = f"opencdr-fleet-manager-{stage}-{suffix}"
        try:
            result[logical] = dynamodb.describe_table(TableName=name)["Table"]
        except ClientError as error:
            if error.response["Error"].get("Code") != "ResourceNotFoundException":
                raise
    return result


def _tags(ddb, arn):
    tags, token = {}, None
    for _ in range(10):
        page = ddb.list_tags_of_resource(ResourceArn=arn, **({"NextToken": token} if token else {}))
        tags.update({row["Key"]: row["Value"] for row in page.get("Tags", [])})
        token = page.get("NextToken")
        if not token:
            return tags
    raise FleetManagerError("Retained resource tags exceeded the bounded inventory")


def _lineage(tags, logical, target):
    origin = tags.get("aws:cloudformation:stack-id", "")
    prefix = (
        f"arn:aws:cloudformation:{target['region']}:{target['account_id']}:"
        f"stack/{target['stack_name']}/"
    )
    if (
        not origin.startswith(prefix)
        or not origin[len(prefix) :]
        or tags.get("aws:cloudformation:logical-id") != logical
        or tags.get("aws:cloudformation:stack-name") != target["stack_name"]
    ):
        raise FleetManagerError(f"Cannot prove retained {logical} belongs to this Fleet deployment")
    return origin


def _resource(kind, properties):
    return {
        "Type": kind,
        "DeletionPolicy": "Retain",
        "UpdateReplacePolicy": "Retain",
        "Properties": properties,
    }


def _table_snapshot(session, table, logical, target):
    ddb = aws_client(session, "dynamodb")
    expected = f"opencdr-fleet-manager-{target['stage']}-{TABLES[logical]}"
    arn = f"arn:aws:dynamodb:{target['region']}:{target['account_id']}:table/{expected}"
    if (
        table.get("TableName") != expected
        or table.get("TableArn") != arn
        or not table.get("TableId")
    ):
        raise FleetManagerError(f"Retained {logical} physical identity is invalid")
    if table.get("TableStatus") != "ACTIVE" or any(
        i.get("IndexStatus") != "ACTIVE" for i in table.get("GlobalSecondaryIndexes", [])
    ):
        raise FleetManagerError(f"Retained table {expected} and its indexes must be ACTIVE")
    if table.get("BillingModeSummary", {}).get("BillingMode") != "PAY_PER_REQUEST":
        raise FleetManagerError("Recovery supports the Fleet's on-demand tables only")
    if (
        table.get("Replicas")
        or table.get("StreamSpecification", {}).get("StreamEnabled")
        or table.get("TableClassSummary", {}).get("TableClass", "STANDARD") != "STANDARD"
        or any(v != -1 for v in table.get("OnDemandThroughput", {}).values())
        or any(
            i.get("OnDemandThroughput") and any(v != -1 for v in i["OnDemandThroughput"].values())
            for i in table.get("GlobalSecondaryIndexes", [])
        )
    ):
        raise FleetManagerError(
            "Custom table replication, streams, class or throughput needs a separate recovery plan"
        )
    props = {k: table[k] for k in ("TableName", "AttributeDefinitions", "KeySchema")}
    props.update(BillingMode="PAY_PER_REQUEST", SSESpecification={"SSEEnabled": False})
    encryption = table.get("SSEDescription", {})
    if encryption.get("SSEType") == "KMS":
        key = encryption.get("KMSMasterKeyArn")
        metadata = aws_client(session, "kms").describe_key(KeyId=key)["KeyMetadata"]
        if (
            encryption.get("Status") != "ENABLED"
            or metadata.get("KeyManager") != "AWS"
            or metadata.get("Arn") != key
        ):
            raise FleetManagerError(
                "Custom or transitioning table KMS encryption needs a separate reviewed recovery"
            )
        props["SSESpecification"] = {"SSEEnabled": True}
    elif encryption:
        raise FleetManagerError("Unsupported table encryption state; keep storage unchanged")
    if table.get("DeletionProtectionEnabled"):
        props["DeletionProtectionEnabled"] = True
    for kind in ("GlobalSecondaryIndexes", "LocalSecondaryIndexes"):
        if table.get(kind):
            props[kind] = [
                {k: index[k] for k in ("IndexName", "KeySchema", "Projection")}
                for index in table[kind]
            ]
    backup = ddb.describe_continuous_backups(TableName=expected)["ContinuousBackupsDescription"]
    status = backup.get("PointInTimeRecoveryDescription", {}).get(
        "PointInTimeRecoveryStatus", "DISABLED"
    )
    if status not in {"ENABLED", "DISABLED"}:
        raise FleetManagerError("Wait for point-in-time recovery configuration to stabilize")
    if backup.get("PointInTimeRecoveryDescription", {}).get("RecoveryPeriodInDays", 35) != 35:
        raise FleetManagerError(
            "Custom point-in-time recovery period needs a separate recovery plan"
        )
    props["PointInTimeRecoverySpecification"] = {"PointInTimeRecoveryEnabled": status == "ENABLED"}
    ttl = ddb.describe_time_to_live(TableName=expected)["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus") not in {"ENABLED", "DISABLED"}:
        raise FleetManagerError("Wait for table TTL configuration to stabilize")
    if ttl["TimeToLiveStatus"] == "ENABLED":
        props["TimeToLiveSpecification"] = {"Enabled": True, "AttributeName": ttl["AttributeName"]}
    tags = _tags(ddb, arn)
    user_tags = [
        {"Key": k, "Value": v} for k, v in sorted(tags.items()) if not k.lower().startswith("aws:")
    ]
    if user_tags:
        props["Tags"] = user_tags
    return (
        _resource("AWS::DynamoDB::Table", props),
        {
            "table_id": table["TableId"],
            "table_arn": arn,
            "encryption_key_arn": encryption.get("KMSMasterKeyArn"),
        },
        tags,
    )


def _bucket_snapshot(session, name, target):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", name):
        raise FleetManagerError("A valid retained evidence bucket name is required")
    s3 = aws_client(session, "s3")
    args = {"Bucket": name, "ExpectedBucketOwner": target["account_id"]}
    s3.head_bucket(**args)
    region = s3.get_bucket_location(**args).get("LocationConstraint") or "us-east-1"
    if region != target["region"]:
        raise FleetManagerError("Retained evidence bucket is in a different region")
    encryption = s3.get_bucket_encryption(**args)["ServerSideEncryptionConfiguration"]["Rules"]
    if (
        len(encryption) != 1
        or encryption[0].get("ApplyServerSideEncryptionByDefault") != {"SSEAlgorithm": "AES256"}
        or encryption[0].get("BucketKeyEnabled", False)
    ):
        raise FleetManagerError("Recovery supports the service-managed AES256 evidence bucket only")
    block = s3.get_public_access_block(**args)["PublicAccessBlockConfiguration"]
    if block != dict.fromkeys(
        ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"), True
    ):
        raise FleetManagerError("All four evidence bucket public-access blocks must be enabled")
    ownership = s3.get_bucket_ownership_controls(**args)["OwnershipControls"]
    if ownership != {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}:
        raise FleetManagerError(
            "Evidence bucket ownership controls differ from the service contract"
        )
    if s3.get_bucket_versioning(**args).get("Status"):
        raise FleetManagerError(
            "Versioned evidence recovery requires a separate object-version plan"
        )
    try:
        s3.get_bucket_replication(**args)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ReplicationConfigurationNotFoundError":
            raise
    else:
        raise FleetManagerError("Replicated evidence recovery needs a separate reviewed plan")
    rules = s3.get_bucket_lifecycle_configuration(**args)["Rules"]
    if len(rules) != 1:
        raise FleetManagerError("Unexpected evidence lifecycle rules; refusing to change retention")
    rule = rules[0]
    expected_keys = {"ID", "Status", "Prefix", "Expiration", "AbortIncompleteMultipartUpload"}
    prefix = rule.get("Prefix")
    if "Filter" in rule and rule["Filter"] == {"Prefix": "detections/"}:
        prefix = "detections/"
        expected_keys = expected_keys - {"Prefix"} | {"Filter"}
    days = rule.get("Expiration", {}).get("Days")
    if (
        set(rule) != expected_keys
        or rule.get("ID") != "DetectionEvidenceRetention"
        or rule.get("Status") != "Enabled"
        or prefix != "detections/"
        or type(days) is not int
        or not 7 <= days <= 365
        or rule.get("Expiration") != {"Days": days}
        or rule.get("AbortIncompleteMultipartUpload") != {"DaysAfterInitiation": 1}
    ):
        raise FleetManagerError("Evidence lifecycle differs from the supported Fleet configuration")
    tags = {t["Key"]: t["Value"] for t in s3.get_bucket_tagging(**args)["TagSet"]}
    props = {
        "BucketName": name,
        "BucketEncryption": {
            "ServerSideEncryptionConfiguration": [
                {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
            ]
        },
        "PublicAccessBlockConfiguration": block,
        "OwnershipControls": ownership,
        "LifecycleConfiguration": {
            "Rules": [
                {
                    "Id": "DetectionEvidenceRetention",
                    "Status": "Enabled",
                    "Prefix": "detections/",
                    "ExpirationInDays": days,
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                }
            ]
        },
    }
    user_tags = [
        {"Key": k, "Value": v} for k, v in sorted(tags.items()) if not k.lower().startswith("aws:")
    ]
    if user_tags:
        props["Tags"] = user_tags
    # Record policy drift, even though BucketPolicy is a separate runtime resource.
    try:
        policy = json.loads(s3.get_bucket_policy(**args)["Policy"])
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchBucketPolicy":
            raise
        policy = None
    if policy is not None:
        expected_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:*",
                    "Resource": [f"arn:aws:s3:::{name}", f"arn:aws:s3:::{name}/*"],
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                }
            ],
        }
        if policy != expected_policy:
            raise FleetManagerError("Custom evidence bucket policy needs a separate recovery plan")
    return (
        _resource("AWS::S3::Bucket", props),
        {
            "bucket_name": name,
            "policy_sha256": hashlib.sha256(canonical(policy).encode()).hexdigest(),
        },
        tags,
        days,
    )


def _inventory(session, stage, account_id, region, evidence_bucket=None):
    target = {
        "account_id": account_id,
        "region": region,
        "stage": stage,
        "stack_name": f"opencdr-fleet-manager-{stage}",
    }
    tables = retained_tables(session, stage)
    if not tables:
        raise FleetManagerError("No retained Fleet tables found; use 'fleet deploy' normally")
    resources, identities, origins, imports = {}, {}, set(), []
    for logical, table in sorted(tables.items()):
        resource, identity, tags = _table_snapshot(session, table, logical, target)
        origins.add(_lineage(tags, logical, target))
        resources[logical], identities[logical] = resource, identity
        imports.append(
            {
                "ResourceType": "AWS::DynamoDB::Table",
                "LogicalResourceId": logical,
                "ResourceIdentifier": {"TableName": table["TableName"]},
            }
        )
    if len(origins) != 1:
        raise FleetManagerError("Retained tables belong to different original stacks")
    origin = next(iter(origins))
    # A known physical bucket name is never sufficient proof of pairing.
    # Use retained CloudFormation provenance tags, plus account/region checks.
    if "DetectionTable" in tables and not evidence_bucket:
        try:
            evidence_bucket = aws_client(session, "cloudformation").describe_stack_resource(
                StackName=origin, LogicalResourceId="DetectionBucket"
            )["StackResourceDetail"]["PhysicalResourceId"]
        except ClientError as error:
            raise FleetManagerError(
                "Cannot discover paired evidence bucket; supply --evidence-bucket "
                "from the original deployment record"
            ) from error
    days = None
    if evidence_bucket:
        if "DetectionTable" not in tables:
            raise FleetManagerError("Detection bucket and table must be recovered together")
        resource, identity, tags, days = _bucket_snapshot(session, evidence_bucket, target)
        if _lineage(tags, "DetectionBucket", target) != origin:
            raise FleetManagerError(
                "Evidence bucket and detection table belong to different original stacks"
            )
        resources["DetectionBucket"], identities["DetectionBucket"] = resource, identity
        imports.append(
            {
                "ResourceType": "AWS::S3::Bucket",
                "LogicalResourceId": "DetectionBucket",
                "ResourceIdentifier": {"BucketName": evidence_bucket},
            }
        )
    return {
        "schema_version": VERSION,
        "target": target,
        "source_stack_id": origin,
        "template": {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Description": IMPORT_DESCRIPTION,
            "Resources": resources,
        },
        "resources_to_import": imports,
        "physical_identities": identities,
        "detection_retention_days": days,
        "deletes_data": False,
        "data_completeness_verified": False,
        "runtime_polling_enabled": False,
        "consumer_identity_reconciled": False,
    }


def recovery_plan(session, stage, *, account_id, region, evidence_bucket=None):
    if existing_stack(session, stage):
        raise FleetManagerError(
            "A Fleet stack already exists; resume its saved recovery plan or inspect it first"
        )
    plan = _inventory(session, stage, account_id, region, evidence_bucket)
    plan["plan_digest"] = digest(plan)
    return plan


def validate_plan(plan, expected_digest):
    try:
        if (
            plan["schema_version"] != VERSION
            or not re.fullmatch(r"[a-f0-9]{64}", expected_digest or "")
            or plan["plan_digest"] != expected_digest
            or digest(plan) != expected_digest
            or plan["deletes_data"] is not False
            or plan["runtime_polling_enabled"] is not False
        ):
            raise ValueError
        target = plan["target"]
        if (
            not re.fullmatch(r"\d{12}", target["account_id"])
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", target["stage"])
            or not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", target["region"])
            or target["stack_name"] != "opencdr-fleet-manager-" + target["stage"]
        ):
            raise ValueError
        resources = plan["template"]["Resources"]
        if not resources or set(resources) - {*TABLES, "DetectionBucket"}:
            raise ValueError
        if ("DetectionTable" in resources) != ("DetectionBucket" in resources):
            raise ValueError
        if any(
            r.get("DeletionPolicy") != "Retain" or r.get("UpdateReplacePolicy") != "Retain"
            for r in resources.values()
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise FleetManagerError(
            "Recovery plan or reviewed digest is invalid; create a new plan"
        ) from None


def _change_set(cf, stack, name):
    try:
        return cf.describe_change_set(StackName=stack, ChangeSetName=name)
    except ClientError as error:
        if error.response["Error"].get("Code") == "ChangeSetNotFound" or _missing(error):
            return None
        raise


def _verify_changes(cf, change, plan):
    if change.get("Description") != "OpenCDR recovery " + plan["plan_digest"]:
        raise FleetManagerError("Recovery change-set provenance differs from the reviewed plan")
    template = cf.get_template(StackName=change["StackId"], ChangeSetName=change["ChangeSetId"])[
        "TemplateBody"
    ]
    if isinstance(template, str):
        template = json.loads(template)
    if template != plan["template"]:
        raise FleetManagerError("Recovery change-set template differs from the reviewed plan")
    rows = list(change.get("Changes", []))
    token = change.get("NextToken")
    for _ in range(10):
        if not token:
            break
        page = cf.describe_change_set(
            StackName=change["StackId"], ChangeSetName=change["ChangeSetId"], NextToken=token
        )
        rows.extend(page.get("Changes", []))
        token = page.get("NextToken")
    if token:
        raise FleetManagerError("Recovery change-set inventory exceeded its bound")
    expected = {(x["LogicalResourceId"], x["ResourceType"]) for x in plan["resources_to_import"]}
    seen = []
    physical = {
        x["LogicalResourceId"]: next(iter(x["ResourceIdentifier"].values()))
        for x in plan["resources_to_import"]
    }
    for row in rows:
        resource = row.get("ResourceChange", {})
        if resource.get("Action") != "Import":
            raise FleetManagerError(
                "Recovery change set contains a non-import action; not executing"
            )
        if resource.get("PhysicalResourceId") and resource["PhysicalResourceId"] != physical.get(
            resource.get("LogicalResourceId")
        ):
            raise FleetManagerError("Recovery change set selects an unreviewed physical resource")
        seen.append((resource.get("LogicalResourceId"), resource.get("ResourceType")))
    if len(seen) != len(expected) or set(seen) != expected:
        raise FleetManagerError(
            "Recovery change set does not import exactly the reviewed storage set"
        )


def _verify_imported(cf, session, plan, stack_id):
    resources = cf.describe_stack_resources(StackName=stack_id)["StackResources"]
    expected = {
        (x["LogicalResourceId"], x["ResourceType"], next(iter(x["ResourceIdentifier"].values())))
        for x in plan["resources_to_import"]
    }
    actual = {
        (x["LogicalResourceId"], x["ResourceType"], x["PhysicalResourceId"]) for x in resources
    }
    if actual != expected:
        raise FleetManagerError("Imported storage identities do not match the reviewed plan")
    # Recheck physical IDs and configuration; AWS lineage tags can change at import.
    target = plan["target"]
    tables = retained_tables(session, target["stage"])
    for logical, resource in plan["template"]["Resources"].items():
        if logical == "DetectionBucket":
            live, identity, _, _ = _bucket_snapshot(
                session, resource["Properties"]["BucketName"], target
            )
        else:
            live, identity, _ = _table_snapshot(session, tables[logical], logical, target)
        if live != resource or identity != plan["physical_identities"][logical]:
            raise FleetManagerError(
                "Imported storage configuration changed; keep polling paused and inspect drift"
            )


def apply_recovery(session, plan, *, expected_digest):
    validate_plan(plan, expected_digest)
    target = plan["target"]
    if session.region_name != target["region"]:
        raise FleetManagerError("Recovery session region differs from the reviewed plan")
    verify_account(session, target["account_id"])
    cf = aws_client(session, "cloudformation")
    name = "opencdr-recover-" + expected_digest[:32]
    change = _change_set(cf, target["stack_name"], name)
    if change is None:
        if existing_stack(session, target["stage"]):
            raise FleetManagerError("Existing stack is not this reviewed recovery operation")
        live = _inventory(
            session,
            target["stage"],
            target["account_id"],
            target["region"],
            plan["physical_identities"].get("DetectionBucket", {}).get("bucket_name"),
        )
        if digest(live) != expected_digest:
            raise FleetManagerError("Retained storage changed since review; create a new plan")
        cf.create_change_set(
            StackName=target["stack_name"],
            ChangeSetName=name,
            ChangeSetType="IMPORT",
            TemplateBody=canonical(plan["template"]),
            ResourcesToImport=plan["resources_to_import"],
            ClientToken=expected_digest,
            Description="OpenCDR recovery " + expected_digest,
        )
        change = _change_set(cf, target["stack_name"], name)
    if not change:
        raise FleetManagerError(
            "Recovery change set is not yet visible; resume the same saved plan"
        )
    if change.get("Status") == "CREATE_IN_PROGRESS":
        try:
            cf.get_waiter("change_set_create_complete").wait(
                StackName=change["StackId"],
                ChangeSetName=name,
                WaiterConfig={"Delay": 3, "MaxAttempts": 15},
            )
        except WaiterError:
            latest = _change_set(cf, target["stack_name"], name)
            if latest and latest.get("Status") == "CREATE_IN_PROGRESS":
                return {
                    "status": "CHANGE_SET_PENDING",
                    "change_set_id": change["ChangeSetId"],
                    "plan_digest": expected_digest,
                }
        change = _change_set(cf, target["stack_name"], name)
    if not change or change.get("Status") != "CREATE_COMPLETE":
        raise FleetManagerError(
            "Recovery change set failed; inspect it without deleting retained storage"
        )
    _verify_changes(cf, change, plan)
    execution = change.get("ExecutionStatus")
    if execution == "AVAILABLE":
        # Close the review-to-execute drift window as far as the AWS APIs permit.
        live = _inventory(
            session,
            target["stage"],
            target["account_id"],
            target["region"],
            plan["physical_identities"].get("DetectionBucket", {}).get("bucket_name"),
        )
        if digest(live) != expected_digest:
            raise FleetManagerError(
                "Retained storage changed before import execution; not executing"
            )
        cf.execute_change_set(
            ChangeSetName=change["ChangeSetId"], ClientRequestToken=expected_digest
        )
    elif execution not in {"EXECUTE_IN_PROGRESS", "EXECUTE_COMPLETE"}:
        raise FleetManagerError("Recovery change set is not executable or resumable")
    try:
        cf.get_waiter("stack_import_complete").wait(
            StackName=change["StackId"], WaiterConfig={"Delay": 3, "MaxAttempts": 15}
        )
    except WaiterError:
        state = cf.describe_stacks(StackName=change["StackId"])["Stacks"][0]["StackStatus"]
        if state == "IMPORT_IN_PROGRESS":
            return {
                "status": state,
                "change_set_id": change["ChangeSetId"],
                "plan_digest": expected_digest,
            }
        raise FleetManagerError(
            "Recovery import did not complete; inspect stack events and resume only after review"
        ) from None
    _verify_imported(cf, session, plan, change["StackId"])
    return {
        "stack_id": change["StackId"],
        "status": "IMPORT_COMPLETE",
        "plan_digest": expected_digest,
        "polling_enabled": False,
        "data_completeness_verified": False,
        "consumer_identity_reconciled": False,
    }


def verify_recovery_template(template, config):
    """Reject the first runtime package if it would replace/reconfigure storage."""
    omitted = object()

    def resolve(value):
        if isinstance(value, list):
            return [resolve(v) for v in value]
        if not isinstance(value, dict):
            return value
        if set(value) == {"Ref"}:
            refs = {
                "AWS::NoValue": omitted,
                "AWS::AccountId": config["security_account_id"],
                "AWS::Region": config["region"],
            }
            if value["Ref"] not in refs:
                raise FleetManagerError(
                    "Recovery storage template contains an unresolved reference"
                )
            return refs[value["Ref"]]
        if set(value) == {"Fn::If"}:
            condition, yes, no = value["Fn::If"]
            return resolve(yes if resolve(template["Conditions"][condition]) else no)
        if set(value) == {"Fn::Equals"}:
            left, right = resolve(value["Fn::Equals"])
            return left == right
        if set(value) == {"Fn::Not"}:
            return not resolve(value["Fn::Not"][0])
        result = {k: resolve(v) for k, v in value.items()}
        return {k: v for k, v in result.items() if v is not omitted}

    def normalized(value):
        if isinstance(value, list):
            return sorted((normalized(v) for v in value), key=canonical)
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == "DeletionProtectionEnabled" and item is False:
                    continue
                if key == "Tags":
                    # Serverless applies these through provider/stack tags too.
                    # Customer tags may drive ABAC and must not be dropped.
                    inherited = [
                        {"Key": "Project", "Value": "opencdr-fleet-manager"},
                        {"Key": "Stage", "Value": config.get("stage")},
                    ]
                    item = [tag for tag in item if tag not in inherited]
                    if not item:
                        continue
                result[key] = normalized(item)
            return result
        return value

    try:
        for logical, expected in config["recovery_storage"].items():
            actual = resolve(template["Resources"][logical])
            if normalized(actual) != normalized(expected):
                raise FleetManagerError(
                    f"Runtime package changes retained {logical}; "
                    "preserve its reviewed properties before deploying"
                )
        schedules = ["ScheduleEventsRuleSchedule1"]
        if "DetectionTable" in config["recovery_storage"]:
            schedules.append("DetectionScheduleEventsRuleSchedule1")
        for logical in schedules:
            if resolve(template["Resources"][logical]["Properties"]["State"]) != "DISABLED":
                raise FleetManagerError(
                    "First recovery deployment must keep both schedules disabled"
                )
    except (KeyError, TypeError, ValueError, IndexError):
        raise FleetManagerError(
            "Runtime package cannot prove preservation of the retained storage set"
        ) from None


def pinned_bucket_name(template, parameters=None):
    """Resolve only the static bucket-name binding emitted by supported installers."""
    values = {k: v.get("Default") for k, v in template.get("Parameters", {}).items()}
    values.update(parameters or {})

    def resolve(value):
        if isinstance(value, (str, bool)) or value is None:
            return value
        if isinstance(value, dict) and len(value) == 1:
            if "Ref" in value:
                if value["Ref"] == "AWS::NoValue":
                    return None
                if value["Ref"] in values:
                    return values[value["Ref"]]
            if "Fn::If" in value:
                condition, yes, no = value["Fn::If"]
                decision = resolve(template["Conditions"][condition])
                if type(decision) is bool:
                    return resolve(yes if decision else no)
            if "Fn::Not" in value:
                decision = resolve(value["Fn::Not"][0])
                if type(decision) is bool:
                    return not decision
            if "Fn::Equals" in value:
                a, b = value["Fn::Equals"]
                return resolve(a) == resolve(b)
        raise FleetManagerError(
            "Cannot resolve the retained evidence bucket binding; use a compatible source package"
        )

    try:
        name = (
            template.get("Resources", {})
            .get("DetectionBucket", {})
            .get("Properties", {})
            .get("BucketName")
        )
        resolved = resolve(name)
        if resolved is not None and (
            not isinstance(resolved, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", resolved)
        ):
            raise ValueError
        return resolved
    except (KeyError, TypeError, ValueError, IndexError):
        raise FleetManagerError("Invalid retained evidence bucket binding") from None
