"""Reviewed fencing for physical operational-storage recovery.

Only declared operational writers are stopped. Core raw ingestion continues to
buffer work in its existing queues; snapshots never rewind queues, outbox state,
raw-ingestion jobs or external effects. No function here resumes any worker.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from botocore.exceptions import ClientError

from .artifacts import load_release
from .deployment_template import materialize
from .detection_activation import DRAIN_SECONDS, require_paused, resource_ids, template_body
from .fleet_api import FleetManagerError, aws_client, verify_account
from .storage_snapshot import canonical, sha256

SCHEMA = "opencdr.storage-quarantine/1"


def configuration_digest(config):
    # Reserved concurrency changes RevisionId, including when code/env did not
    # change. Pin the complete functional configuration instead of that token.
    volatile = {
        "ResponseMetadata",
        "RevisionId",
        "LastModified",
        "State",
        "StateReason",
        "StateReasonCode",
        "LastUpdateStatus",
        "LastUpdateStatusReason",
        "LastUpdateStatusReasonCode",
    }
    return sha256({k: v for k, v in config.items() if k not in volatile})


CORE_TABLES = ("SignalsTableV2", "DetectionFeedTable", "CaseTable", "LibraryTable")
FLEET_TABLES = (
    "RegistryTable",
    "StatusTable",
    "ConfigurationTable",
    "RolloutTable",
    "DetectionTable",
    "CaseTable",
    "LibraryTable",
)
CORE_CONTINUING = {
    "ProcessorLambdaFunction",
    "S3IngestionLambdaFunction",
    "AlarmNotifierLambdaFunction",
    "ArchiverLambdaFunction",
    "NotifierLambdaFunction",
}
CORE_FUNCTIONS = CORE_CONTINUING | {
    "SignalWriterLambdaFunction",
    "AlerterLambdaFunction",
    "PublisherLambdaFunction",
    "ResponderLambdaFunction",
    "RollbackHandlerLambdaFunction",
    "ApiLambdaFunction",
    *(
        "Management" + name + "LambdaFunction"
        for name in [
            "Delivery",
            "Integration",
            "Triage",
            "Observer",
            "Operator",
            "Responder",
            "Administrator",
        ]
    ),
}
FLEET_FUNCTIONS = {
    "ApiLambdaFunction",
    "DetectionScheduleLambdaFunction",
    "DetectionPollLambdaFunction",
    "ScheduleLambdaFunction",
    "PollLambdaFunction",
    *(
        "Management" + name + "LambdaFunction"
        for name in ["Integration", "Triage", "Observer", "Operator", "Responder", "Administrator"]
    ),
    *("Rollout" + name + "LambdaFunction" for name in ["Operator", "Responder", "Administrator"]),
}


def _cloud_time(response):
    value = response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("date")
    if not value:
        raise FleetManagerError("AWS response has no service clock for the quarantine drain")
    return parsedate_to_datetime(value).astimezone(UTC).timestamp()


def _body(response):
    try:
        raw = response["Body"].read(2_000_001)
    finally:
        response["Body"].close()
    if len(raw) > 2_000_000:
        raise FleetManagerError("Recovery state exceeds its byte limit")
    return json.loads(raw)


def _get(s3, bucket, key, account):
    try:
        response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
        return _body(response), response["ETag"]
    except ClientError as error:
        if error.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
            raise
        return None, None


def require_unlocked(session, stack):
    """Ordinary deployments/activation cannot race a physical recovery."""
    parameters = {r["ParameterKey"]: r["ParameterValue"] for r in stack.get("Parameters", [])}
    bucket = parameters.get("ArtifactBucket")
    if not bucket:
        return  # Legacy installations cannot enter this recovery protocol.
    stack_id = stack["StackId"]
    account = stack_id.split(":")[4]
    lock, _ = _get(
        aws_client(session, "s3"),
        bucket,
        "opencdr-recovery/locks/" + sha256(stack_id) + ".json",
        account,
    )
    if lock is not None and set(lock) != {"released_plan_sha256"}:
        raise FleetManagerError(
            "A storage recovery owns this stack; finish or resume its source first"
        )


def inventory(
    session, *, stack_name, directory, release_sha256, bucket, account_id, region, reason
):
    if not isinstance(reason, str) or not 10 <= len(reason) <= 1000:
        raise FleetManagerError("Recovery requires a concrete reason of 10–1000 characters")
    verify_account(session, account_id)
    if region.startswith(("cn-", "us-gov-")):
        raise FleetManagerError("This recovery controller currently supports commercial AWS only")
    manifest, source = load_release(directory, release_sha256)
    cfn, ddb, lambdas, s3 = [
        aws_client(session, k) for k in ["cloudformation", "dynamodb", "lambda", "s3"]
    ]
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    require_unlocked(session, stack)
    if stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        raise FleetManagerError("Wait for a stable source deployment before recovery planning")
    params = {r["ParameterKey"]: r["ParameterValue"] for r in stack["Parameters"]}
    stage = params["Stage"]
    mode = "core" if manifest["product"] == "opencdr" else "fleet"
    if stack_name != manifest["product"] + "-" + stage:
        raise FleetManagerError("Recovery target does not match the pinned installation")
    live = template_body(cfn, StackName=stack_name)
    from .storage_bindings import current_binding, verify_physical

    binding = current_binding(live)
    if binding:
        verify_physical(session, binding)
    if sha256(live) != sha256(materialize(source, params, binding=binding)):
        raise FleetManagerError("Source stack does not match the pinned release and parameters")
    if mode == "core":
        require_paused(session, stack_name, live)
    elif params.get("DetectionPollingEnabled") != "false":
        raise FleetManagerError("Pause Fleet detection polling through Deploy before recovery")
    ids = resource_ids(cfn, stack_name, live)
    expected = CORE_FUNCTIONS if mode == "core" else FLEET_FUNCTIONS
    actual = {
        name for name, row in live["Resources"].items() if row["Type"] == "AWS::Lambda::Function"
    }
    if actual != expected:
        raise FleetManagerError("Unknown writer topology requires a new reviewed recovery contract")
    functions = {}
    for name in sorted(actual):
        config = lambdas.get_function_configuration(FunctionName=ids[name])
        if (
            config.get("State") != "Active"
            or config.get("LastUpdateStatus") != "Successful"
            or base64.b64decode(config["CodeSha256"]).hex() != manifest["files"]["runtime.zip"]
        ):
            raise FleetManagerError("Every runtime must match the pinned source before quarantine")
        frozen = mode == "fleet" or name not in CORE_CONTINUING
        functions[name] = {
            "name": ids[name],
            "arn": config["FunctionArn"],
            "revision_id": config["RevisionId"],
            "configuration_sha256": configuration_digest(config),
            "code_sha256": config["CodeSha256"],
            "timeout": config["Timeout"],
            "freeze": frozen,
            "concurrency": lambdas.get_function_concurrency(FunctionName=ids[name]).get(
                "ReservedConcurrentExecutions"
            ),
        }
    selected = {row["arn"] for row in functions.values() if row["freeze"]}
    mappings = {}
    known_mappings = {
        physical
        for logical, physical in ids.items()
        if live["Resources"].get(logical, {}).get("Type") == "AWS::Lambda::EventSourceMapping"
    }
    for function in functions.values():
        if not function["freeze"]:
            continue
        marker = None
        for _ in range(10):
            page = lambdas.list_event_source_mappings(
                FunctionName=function["name"], **({"Marker": marker} if marker else {})
            )
            for mapping in page["EventSourceMappings"]:
                if mapping["UUID"] not in known_mappings or mapping["State"] not in {
                    "Enabled",
                    "Disabled",
                }:
                    raise FleetManagerError(
                        "Unmanaged or transitioning event mapping blocks recovery"
                    )
                mappings[mapping["UUID"]] = {
                    k: mapping[k] for k in ["UUID", "FunctionArn", "EventSourceArn", "State"]
                }
            marker = page.get("NextMarker")
            if not marker:
                break
        else:
            raise FleetManagerError("Event mapping inventory exceeded its bound")
    events = aws_client(session, "events")
    rules = {}
    for logical, resource in live["Resources"].items():
        if resource["Type"] != "AWS::Events::Rule" or logical not in ids:
            continue
        name = ids[logical]
        targets = events.list_targets_by_rule(Rule=name)
        if targets.get("NextToken"):
            raise FleetManagerError("Rule target inventory exceeded its bound")
        if any(row["Arn"] in selected for row in targets["Targets"]):
            value = events.describe_rule(Name=name)
            if not value.get("ScheduleExpression") or value.get("EventPattern"):
                raise FleetManagerError(
                    "Business event ingress cannot be dropped during quarantine"
                )
            rules[name] = {
                "state": value["State"],
                "schedule": value["ScheduleExpression"],
                "targets_sha256": sha256(targets["Targets"]),
            }
    tables = {}
    for logical in CORE_TABLES if mode == "core" else FLEET_TABLES:
        value = ddb.describe_table(TableName=ids[logical])["Table"]
        if value.get("TableStatus") != "ACTIVE" or not value.get("TableId"):
            raise FleetManagerError(
                "Source operational tables must have stable physical identities"
            )
        tables[logical] = {
            "name": ids[logical],
            "id": value["TableId"],
            "spec": live["Resources"][logical]["Properties"],
        }
        if binding:
            previous = binding["resources"][logical]
            tables[logical]["original_name"] = previous.get(
                "original_name", previous["source_name"]
            )
    buckets = {}
    for logical in (
        ["CaseBucket", "LibraryBucket", "IngestionBucket"]
        if mode == "core"
        else ["DetectionBucket", "CaseBucket", "LibraryBucket"]
    ):
        name = binding["resources"][logical]["name"] if binding else ids[logical]
        s3.head_bucket(Bucket=name, ExpectedBucketOwner=account_id)
        buckets[logical] = {
            "name": name,
            "prefix": "detection-feed/" if logical == "IngestionBucket" else "",
            "usage": "detection_payload" if logical == "IngestionBucket" else "storage",
        }
        if binding:
            previous = binding["resources"][logical]
            buckets[logical]["original_name"] = previous.get(
                "original_name", previous["source_name"]
            )
    s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account_id)
    if bucket in {r["name"] for r in buckets.values()}:
        raise FleetManagerError("Recovery artifacts require a separate private bucket")
    return {
        "schema": SCHEMA,
        "mode": mode,
        "account_id": account_id,
        "region": region,
        "stage": stage,
        "stack_name": stack_name,
        "stack_id": stack["StackId"],
        "template_sha256": sha256(live),
        "release_sha256": release_sha256,
        "runtime_sha256": manifest["files"]["runtime.zip"],
        "artifact_bucket": bucket,
        "reason": reason,
        "created_at": datetime.now(UTC).isoformat(),
        "functions": functions,
        "mappings": mappings,
        "rules": rules,
        "tables": tables,
        "buckets": buckets,
        "drain_seconds": DRAIN_SECONDS,
        "continuing_functions": sorted(
            actual - set(functions_name for functions_name, fn in functions.items() if fn["freeze"])
        ),
        "scope": (
            "retained operational facts/cases/library; queues/outbox/raw ingestion are not rewound"
        ),
    }


def validate_plan(plan, expected_digest):
    if (
        not re.fullmatch(r"[a-f0-9]{64}", expected_digest or "")
        or sha256(plan) != expected_digest
        or plan.get("schema") != SCHEMA
        or plan.get("drain_seconds") != DRAIN_SECONDS
    ):
        raise FleetManagerError("Recovery plan digest or schema mismatch")
    mode = plan.get("mode")
    expected_functions = CORE_FUNCTIONS if mode == "core" else FLEET_FUNCTIONS
    expected_tables = CORE_TABLES if mode == "core" else FLEET_TABLES
    if (
        mode not in {"core", "fleet"}
        or set(plan.get("functions", {})) != expected_functions
        or set(plan.get("tables", {})) != set(expected_tables)
        or not re.fullmatch(r"\d{12}", plan.get("account_id", ""))
        or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", plan.get("region", ""))
        or plan["region"].startswith(("cn-", "us-gov-"))
        or not plan.get("stack_id", "").startswith(
            f"arn:aws:cloudformation:{plan['region']}:{plan['account_id']}:stack/"
            f"{plan.get('stack_name', '')}/"
        )
    ):
        raise FleetManagerError("Recovery plan has an unsupported owner or writer topology")
    for logical, row in plan["functions"].items():
        if not re.fullmatch(r"[a-f0-9]{64}", row.get("configuration_sha256", "")):
            raise FleetManagerError("Recovery plan must pin every functional configuration")
        if row["freeze"] != (mode == "fleet" or logical not in CORE_CONTINUING):
            raise FleetManagerError("Recovery plan changes the declared writer fence")
        if type(row.get("timeout")) is not int or not 1 <= row["timeout"] <= 900:
            raise FleetManagerError("Recovery cannot drain this function execution model")
    return expected_digest


def verify_source(session, plan, *, frozen=False):
    verify_account(session, plan["account_id"])
    cfn = aws_client(session, "cloudformation")
    lambdas = aws_client(session, "lambda")
    ddb = aws_client(session, "dynamodb")
    stack = cfn.describe_stacks(StackName=plan["stack_name"])["Stacks"][0]
    if (
        stack["StackId"] != plan["stack_id"]
        or stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
        or sha256(template_body(cfn, StackName=plan["stack_name"])) != plan["template_sha256"]
    ):
        raise FleetManagerError("Recovery source deployment changed after review")
    if plan["mode"] == "core":
        require_paused(
            session, plan["stack_name"], template_body(cfn, StackName=plan["stack_name"])
        )
    for row in plan["tables"].values():
        live = ddb.describe_table(TableName=row["name"])["Table"]
        if live.get("TableId") != row["id"] or live.get("TableStatus") != "ACTIVE":
            raise FleetManagerError("Recovery source physical table changed")
    for row in plan["functions"].values():
        live = lambdas.get_function_configuration(FunctionName=row["name"])
        if (
            live.get("FunctionArn") != row["arn"]
            or configuration_digest(live) != row["configuration_sha256"]
            or live.get("CodeSha256") != row["code_sha256"]
            or live.get("LastUpdateStatus") != "Successful"
        ):
            raise FleetManagerError("A runtime changed during quarantine")
        concurrency = lambdas.get_function_concurrency(FunctionName=row["name"]).get(
            "ReservedConcurrentExecutions"
        )
        allowed = (
            {0}
            if frozen and row["freeze"]
            else {row["concurrency"], 0}
            if row["freeze"]
            else {row["concurrency"]}
        )
        if concurrency not in allowed:
            raise FleetManagerError("Runtime concurrency changed outside recovery")
    if frozen:
        for row in plan["mappings"].values():
            live = lambdas.get_event_source_mapping(UUID=row["UUID"])
            if (
                any(live[k] != row[k] for k in ["FunctionArn", "EventSourceArn"])
                or live["State"] != "Disabled"
            ):
                raise FleetManagerError("A source mapping is not safely quarantined")
        events = aws_client(session, "events")
        for name, expected in plan["rules"].items():
            live = events.describe_rule(Name=name)
            targets = events.list_targets_by_rule(Rule=name)
            if (
                live["State"] != "DISABLED"
                or live.get("ScheduleExpression") != expected["schedule"]
                or targets.get("NextToken")
                or sha256(targets["Targets"]) != expected["targets_sha256"]
            ):
                raise FleetManagerError("A scheduled writer changed during quarantine")
    return _cloud_time(
        lambdas.get_function_concurrency(
            FunctionName=next(iter(plan["functions"].values()))["name"]
        )
    )


def freeze(session, plan, expected_digest):
    digest = validate_plan(plan, expected_digest)
    verify_source(session, plan)
    s3 = aws_client(session, "s3")
    bucket = plan["artifact_bucket"]
    account = plan["account_id"]
    prefix = "opencdr-recovery/" + digest
    lock = "opencdr-recovery/locks/" + sha256(plan["stack_id"]) + ".json"
    try:
        s3.put_object(
            Bucket=bucket,
            Key=lock,
            Body=canonical({"plan_sha256": digest}),
            ExpectedBucketOwner=account,
            IfNoneMatch="*",
            ServerSideEncryption="AES256",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
            raise
        existing, lock_etag = _get(s3, bucket, lock, account)
        if existing and set(existing) == {"released_plan_sha256"}:
            if existing["released_plan_sha256"] == digest:
                raise FleetManagerError("A resumed recovery cannot be frozen again") from None
            s3.put_object(
                Bucket=bucket,
                Key=lock,
                Body=canonical({"plan_sha256": digest}),
                ExpectedBucketOwner=account,
                IfMatch=lock_etag,
                ServerSideEncryption="AES256",
            )
        elif existing != {"plan_sha256": digest}:
            raise FleetManagerError("Another recovery owns this deployment") from None
    existing, _ = _get(s3, bucket, prefix + "/plan.json", account)
    if existing is not None and existing != plan:
        raise FleetManagerError("Stored recovery plan differs")
    if existing is None:
        s3.put_object(
            Bucket=bucket,
            Key=prefix + "/plan.json",
            Body=canonical(plan),
            ExpectedBucketOwner=account,
            IfNoneMatch="*",
            ServerSideEncryption="AES256",
        )
    state, etag = _get(s3, bucket, prefix + "/quarantine.json", account)
    if state and state.get("state") == "frozen":
        now = verify_source(session, plan, frozen=True)
        return {**state, "drained": now >= state["drain_deadline"]}
    if state:
        raise FleetManagerError("Recovery is resuming or completed; it cannot be frozen again")
    events = aws_client(session, "events")
    lambdas = aws_client(session, "lambda")
    for name in plan["rules"]:
        events.disable_rule(Name=name)
    pending = []
    for row in plan["mappings"].values():
        current = lambdas.get_event_source_mapping(UUID=row["UUID"])
        if any(current[k] != row[k] for k in ["FunctionArn", "EventSourceArn"]):
            raise FleetManagerError("Event mapping identity changed")
        if current["State"] == "Enabled":
            lambdas.update_event_source_mapping(UUID=row["UUID"], Enabled=False)
            pending.append(row["UUID"])
        elif current["State"] == "Disabling":
            pending.append(row["UUID"])
        elif current["State"] != "Disabled":
            raise FleetManagerError("Unexpected event mapping transition")
    if pending:
        return {"state": "disabling_mappings", "pending": pending, "plan_sha256": digest}
    for row in plan["functions"].values():
        if row["freeze"]:
            lambdas.put_function_concurrency(
                FunctionName=row["name"], ReservedConcurrentExecutions=0
            )
    now = verify_source(session, plan, frozen=True)
    state = {
        "state": "frozen",
        "plan_sha256": digest,
        "frozen_at": now,
        "drain_deadline": now + DRAIN_SECONDS,
    }
    s3.put_object(
        Bucket=bucket,
        Key=prefix + "/quarantine.json",
        Body=canonical(state),
        ExpectedBucketOwner=account,
        ServerSideEncryption="AES256",
        **({"IfMatch": etag} if etag else {"IfNoneMatch": "*"}),
    )
    return {**state, "drained": False}


def require_drained(session, plan, expected_digest):
    digest = validate_plan(plan, expected_digest)
    now = verify_source(session, plan, frozen=True)
    s3 = aws_client(session, "s3")
    bucket = plan["artifact_bucket"]
    account = plan["account_id"]
    lock, _ = _get(
        s3, bucket, "opencdr-recovery/locks/" + sha256(plan["stack_id"]) + ".json", account
    )
    state, _ = _get(s3, bucket, "opencdr-recovery/" + digest + "/quarantine.json", account)
    if (
        lock != {"plan_sha256": digest}
        or not state
        or state.get("plan_sha256") != digest
        or state.get("state") != "frozen"
        or now < state["drain_deadline"]
    ):
        raise FleetManagerError("Recovery requires a verified, fully drained quarantine")
    return state


def resume_source(session, plan, expected_digest):
    """Abort before cutover, restoring only the inventoried source controls.

    No restored resource is deleted. A different deployment or physical source
    prevents this operation, so it cannot rewind a completed cutover. Repeating
    after an uncertain response reconciles the exact controls and durable state.
    Detection read activation and Fleet detection polling remain paused.
    """
    digest = validate_plan(plan, expected_digest)
    verify_source(session, plan)
    s3 = aws_client(session, "s3")
    bucket, account = plan["artifact_bucket"], plan["account_id"]
    lock_key = "opencdr-recovery/locks/" + sha256(plan["stack_id"]) + ".json"
    state_key = "opencdr-recovery/" + digest + "/quarantine.json"
    lock, lock_etag = _get(s3, bucket, lock_key, account)
    state, etag = _get(s3, bucket, state_key, account)
    if state and state.get("state") == "source_resumed":
        if lock == {"plan_sha256": digest}:
            s3.put_object(
                Bucket=bucket,
                Key=lock_key,
                Body=canonical({"released_plan_sha256": digest}),
                IfMatch=lock_etag,
                ExpectedBucketOwner=account,
                ServerSideEncryption="AES256",
            )
        return state
    if lock != {"plan_sha256": digest}:
        raise FleetManagerError("This recovery does not own the source quarantine")
    if state and state.get("state") not in {"frozen", "resuming_source"}:
        raise FleetManagerError("This recovery can no longer resume its old storage")
    if not state or state["state"] != "resuming_source":
        state = {"state": "resuming_source", "plan_sha256": digest}
        result = s3.put_object(
            Bucket=bucket,
            Key=state_key,
            Body=canonical(state),
            ExpectedBucketOwner=account,
            ServerSideEncryption="AES256",
            **({"IfMatch": etag} if etag else {"IfNoneMatch": "*"}),
        )
        etag = result["ETag"]
    lambdas, events = aws_client(session, "lambda"), aws_client(session, "events")
    for row in plan["functions"].values():
        if not row["freeze"]:
            continue
        if row["concurrency"] is None:
            lambdas.delete_function_concurrency(FunctionName=row["name"])
        else:
            lambdas.put_function_concurrency(
                FunctionName=row["name"],
                ReservedConcurrentExecutions=row["concurrency"],
            )
    pending = []
    for row in plan["mappings"].values():
        live = lambdas.get_event_source_mapping(UUID=row["UUID"])
        if any(live[k] != row[k] for k in ["FunctionArn", "EventSourceArn"]):
            raise FleetManagerError("Source mapping identity changed; resume stopped")
        if live["State"] == row["State"]:
            continue
        if live["State"] in {"Enabling", "Disabling"}:
            pending.append(row["UUID"])
            continue
        if live["State"] not in {"Enabled", "Disabled"}:
            raise FleetManagerError("Source mapping is in an unsupported state")
        lambdas.update_event_source_mapping(UUID=row["UUID"], Enabled=row["State"] == "Enabled")
        pending.append(row["UUID"])
    if pending:
        return {**state, "pending": pending}
    for name, expected in plan["rules"].items():
        live = events.describe_rule(Name=name)
        targets = events.list_targets_by_rule(Rule=name)
        if (
            live.get("ScheduleExpression") != expected["schedule"]
            or targets.get("NextToken")
            or sha256(targets["Targets"]) != expected["targets_sha256"]
        ):
            raise FleetManagerError("Source schedule changed; resume stopped")
        if expected["state"] == "ENABLED":
            events.enable_rule(Name=name)
        else:
            events.disable_rule(Name=name)
    verify_source(session, plan)
    for row in plan["functions"].values():
        if (
            lambdas.get_function_concurrency(FunctionName=row["name"]).get(
                "ReservedConcurrentExecutions"
            )
            != row["concurrency"]
        ):
            raise FleetManagerError("Source concurrency has not returned to its reviewed value")
    state = {
        "state": "source_resumed",
        "plan_sha256": digest,
        "reads_reactivated": False,
        "restored_resources_deleted": False,
    }
    s3.put_object(
        Bucket=bucket,
        Key=state_key,
        Body=canonical(state),
        ExpectedBucketOwner=account,
        ServerSideEncryption="AES256",
        IfMatch=etag,
    )
    s3.put_object(
        Bucket=bucket,
        Key=lock_key,
        Body=canonical({"released_plan_sha256": digest}),
        ExpectedBucketOwner=account,
        ServerSideEncryption="AES256",
        IfMatch=lock_etag,
    )
    return state
