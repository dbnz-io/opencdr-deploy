"""Reviewed Core read activation. Never disables writers, triggers or queues.

The operator's AWS identity is the authority. Plans are observations, not an
authorization token: AWS state and retained-source backfill are checked again
on apply. Infrastructure administrators must serialize deployment/recovery with
activation; this is not a lock against out-of-band AWS administration.
"""

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from .fleet_api import FleetManagerError, aws_client, verify_account

SCHEMA = "opencdr.detection-activation/1"
DRAIN_SECONDS = 960  # Standard event functions only; managed/durable execution is excluded.
HEAD = {"pk": {"S": "detections"}, "sequence": {"N": "0"}}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def decode(row):
    return {k: TypeDeserializer().deserialize(v) for k, v in row.items()}


def encode(row):
    return {k: TypeSerializer().serialize(v) for k, v in row.items()}


def timestamp(value):
    value = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    if value.tzinfo is None:
        raise FleetManagerError("AWS deployment timestamp lacks a timezone")
    return value.timestamp()


def template_body(cfn, **args):
    body = cfn.get_template(**args)["TemplateBody"]
    return json.loads(body) if isinstance(body, str) else body


def resource_ids(cfn, stack, template=None):
    items, token = {}, None
    while True:
        page = cfn.list_stack_resources(StackName=stack, **({"NextToken": token} if token else {}))
        items.update(
            {
                r["LogicalResourceId"]: r["PhysicalResourceId"]
                for r in page["StackResourceSummaries"]
                if r.get("PhysicalResourceId")
            }
        )
        token = page.get("NextToken")
        if not token:
            if template is None:
                return items
            from .storage_bindings import bound_ids

            return bound_ids(items, template)


def runtime_functions(template):
    return {
        key: row["Properties"]
        for key, row in template.get("Resources", {}).items()
        if row["Type"] == "AWS::Lambda::Function"
        and row["Properties"].get("Handler", "").startswith("src.")
    }


def guarded(template):
    functions = runtime_functions(template)
    return bool(functions) and all(
        p.get("Environment", {}).get("Variables", {}).get("DETECTION_ACTIVATION_REQUIRED") == "true"
        for p in functions.values()
    )


def require_paused(session, stack, template):
    """Reused at both release planning and execution; no old guard, no rolling update."""
    if any(
        p.get("CapacityProviderConfig") or p.get("DurableConfig")
        for p in runtime_functions(template).values()
    ):
        raise FleetManagerError(
            "Managed/durable Lambda execution needs a separate draining protocol"
        )
    if "DetectionFeedTable" not in template.get("Resources", {}):
        return
    if not guarded(template):
        raise FleetManagerError(
            "Existing feed has unguarded readers; a coordinated legacy migration is required"
        )
    ids = resource_ids(aws_client(session, "cloudformation"), stack, template)
    row = decode(
        aws_client(session, "dynamodb")
        .get_item(
            TableName=ids["DetectionFeedTable"],
            Key=HEAD,
            ConsistentRead=True,
        )
        .get("Item", {})
    )
    if row.get("activation"):
        raise FleetManagerError(
            "Pause detection reads with a reviewed detection-activation plan before upgrading Core"
        )


def claim_deployment(session, stack, template, change_set):
    """Atomically exclude activation before starting a CloudFormation update.

    A failed update keeps reads paused. A new reviewed change set can supersede
    that barrier only once CloudFormation is no longer executing an update.
    """
    require_paused(session, stack, template)
    if "DetectionFeedTable" not in template.get("Resources", {}):
        return
    cfn, ddb = aws_client(session, "cloudformation"), aws_client(session, "dynamodb")
    state = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    if state["StackStatus"] not in {
        "CREATE_COMPLETE",
        "UPDATE_COMPLETE",
        "UPDATE_ROLLBACK_COMPLETE",
    }:
        raise FleetManagerError("A Core deployment is already in progress or needs recovery")
    table = resource_ids(cfn, stack, template)["DetectionFeedTable"]
    head = decode(ddb.get_item(TableName=table, Key=HEAD, ConsistentRead=True).get("Item", {}))
    if not head:
        raise FleetManagerError("Initialize the feed with a backfill before upgrading it")
    if head.get("activation"):
        raise FleetManagerError("Detection activation raced deployment; no update started")
    pending = head.get("deployment_pending")
    if pending == change_set:
        # An uncertain execution response must reuse the durable claim rather
        # than invalidating it with another activation revision.
        return
    if pending and pending != state.get("ChangeSetId"):
        raise FleetManagerError(
            "Another change set owns the deployment barrier; resume that exact change set"
        )
    values = {":pending": change_set, ":next": int(head.get("activation_revision", 0)) + 1}
    condition = "attribute_not_exists(activation) AND "
    if "activation_revision" in head:
        condition += "activation_revision = :old"
        values[":old"] = head["activation_revision"]
    else:
        condition += "attribute_not_exists(activation_revision)"
    if pending:
        condition += " AND deployment_pending = :previous"
        values[":previous"] = pending
    else:
        condition += " AND attribute_not_exists(deployment_pending)"
    try:
        ddb.update_item(
            TableName=table,
            Key=HEAD,
            UpdateExpression="SET deployment_pending = :pending, activation_revision = :next",
            ConditionExpression=condition,
            ExpressionAttributeValues=encode(values),
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise FleetManagerError(
                "Detection activation raced deployment; no update started"
            ) from None
        raise


def observe(session, stack, action, backfill_id=None, release=None):
    cfn, ddb, lambdas = (
        aws_client(session, name) for name in ("cloudformation", "dynamodb", "lambda")
    )
    live = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    from .storage_quarantine import require_unlocked

    require_unlocked(session, live)
    if live["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        raise FleetManagerError("Core stack must be in CREATE_COMPLETE or UPDATE_COMPLETE")
    template = template_body(cfn, StackName=stack)
    if not guarded(template):
        raise FleetManagerError("Core does not have the required read activation guard")
    if release:
        from .deployment_template import materialize

        manifest, expected = release
        parameters = {p["ParameterKey"]: p["ParameterValue"] for p in live.get("Parameters", [])}
        from .storage_bindings import current_binding, verify_physical

        binding = current_binding(template)
        if binding:
            verify_physical(session, binding)
        installed = materialize(expected, parameters, binding=binding)
        if manifest["product"] != "opencdr" or digest(template) not in {
            digest(expected),
            digest(installed),
        }:
            raise FleetManagerError("Deployed template does not match the reviewed Core release")
    ids = resource_ids(cfn, stack, template)
    tables = {}
    for logical in ("DetectionFeedTable", "SignalsTableV2"):
        table = ddb.describe_table(TableName=ids[logical])["Table"]
        if table["TableStatus"] != "ACTIVE" or not table.get("TableId"):
            raise FleetManagerError("Detection storage is not active or lacks physical identity")
        tables[logical] = {"name": ids[logical], "id": table["TableId"]}
        if logical == "DetectionFeedTable" and not any(
            i["IndexName"] == "by-day" and i["IndexStatus"] == "ACTIVE"
            for i in table.get("GlobalSecondaryIndexes", [])
        ):
            raise FleetManagerError("Detection query index is not active")
    head = decode(
        ddb.get_item(TableName=ids["DetectionFeedTable"], Key=HEAD, ConsistentRead=True).get(
            "Item", {}
        )
    )
    if not head or head.get("table_id") != tables["DetectionFeedTable"]["id"]:
        raise FleetManagerError(
            "Initialize/backfill or explicitly recover the physical feed binding first"
        )
    if head.get("deployment_pending") and head["deployment_pending"] != live.get("ChangeSetId"):
        raise FleetManagerError(
            "A reviewed deployment has not completed; activation remains blocked"
        )
    functions, latest = {}, timestamp(live.get("LastUpdatedTime", live["CreationTime"]))
    runtime = None
    for logical, properties in runtime_functions(template).items():
        config = lambdas.get_function_configuration(FunctionName=ids[logical])
        if (
            config.get("CapacityProviderConfig")
            or config.get("DurableConfig")
            or config.get("Timeout", 901) > 900
        ):
            raise FleetManagerError(
                "Managed/durable Lambda execution needs a separate draining protocol"
            )
        env = config.get("Environment", {}).get("Variables", {})
        if config.get("State") != "Active" or config.get("LastUpdateStatus") != "Successful":
            raise FleetManagerError("Lambda deployment has not settled")
        if (
            env.get("DETECTION_ACTIVATION_REQUIRED") != "true"
            or config["Handler"] != properties["Handler"]
        ):
            raise FleetManagerError("Lambda activation guard or handler drifted")
        if (
            env.get("DETECTION_FEED_TABLE_NAME") != ids["DetectionFeedTable"]
            or env.get("SIGNALS_TABLE_NAME") != ids["SignalsTableV2"]
        ):
            raise FleetManagerError("Lambda storage binding drifted")
        sha = base64.b64decode(config["CodeSha256"], validate=True).hex()
        if len(sha) != 64 or env.get("DETECTION_RUNTIME_ID") != sha or runtime not in (None, sha):
            raise FleetManagerError("Runtime identity differs across Core functions")
        if release and sha != release[0]["files"]["runtime.zip"]:
            raise FleetManagerError("Lambda code differs from the reviewed runtime")
        runtime = sha
        # Qualified versions/aliases need their own draining protocol. Refuse to
        # silently treat them as the updated unqualified Core pipeline.
        if lambdas.list_aliases(FunctionName=ids[logical]).get("Aliases"):
            raise FleetManagerError("Qualified Core aliases require a separate migration")
        for page in lambdas.get_paginator("list_event_source_mappings").paginate(
            FunctionName=ids[logical]
        ):
            if any(m["FunctionArn"] != config["FunctionArn"] for m in page["EventSourceMappings"]):
                raise FleetManagerError("A source mapping targets a qualified Core function")
        latest = max(latest, timestamp(config["LastModified"]))
        functions[logical] = {
            k: config[k]
            for k in ("FunctionArn", "RevisionId", "CodeSha256", "LastModified", "Timeout")
        }
    # Check actual EventBridge targets, including rules created before the upgrade.
    arns = {f["FunctionArn"] for f in functions.values()}
    events = aws_client(session, "events")
    for logical, resource in template["Resources"].items():
        if resource["Type"] == "AWS::Events::Rule":
            token = None
            while True:
                page = events.list_targets_by_rule(
                    Rule=ids[logical], **({"NextToken": token} if token else {})
                )
                for target in page["Targets"]:
                    arn = target["Arn"]
                    if any(arn.startswith(base + ":") for base in arns):
                        raise FleetManagerError(
                            "EventBridge still targets a qualified Core function"
                        )
                token = page.get("NextToken")
                if not token:
                    break
    snapshot = {
        "stack_id": live["StackId"],
        "template_sha256": digest(template),
        "parameters": sorted(live.get("Parameters", []), key=lambda p: p["ParameterKey"]),
        "stack_updated_at": datetime.fromtimestamp(latest, UTC).isoformat(),
        "tables": tables,
        "functions": functions,
        "epoch": head["epoch"],
        "runtime_sha256": runtime,
        "deployment_pending": head.get("deployment_pending"),
    }
    if action == "activate":
        if not release or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", backfill_id or ""):
            raise FleetManagerError("Activation requires a pinned release and backfill ID")
        job = decode(
            ddb.get_item(
                TableName=ids["DetectionFeedTable"],
                Key=encode({"pk": "backfill#" + backfill_id, "sequence": 0}),
                ConsistentRead=True,
            ).get("Item", {})
        )
        if (
            job.get("state") != "completed"
            or not job.get("retained_source_scan_complete")
            or job.get("epoch") != head["epoch"]
            or job.get("feed_table_id") != tables["DetectionFeedTable"]["id"]
            or job.get("source_table_id") != tables["SignalsTableV2"]["id"]
        ):
            raise FleetManagerError(
                "Backfill must be completed against the current source and feed generation"
            )
        if timestamp(job["created_at"]) < latest + DRAIN_SECONDS:
            raise FleetManagerError(
                "Create a fresh backfill at least 16 minutes after the last deployment change"
            )
        snapshot["backfill"] = {
            "id": backfill_id,
            "plan_digest": job["plan_digest"],
            "rev": int(job["rev"]),
            "counts": {k: int(v) for k, v in job["counts"].items()},
            "created_at": job["created_at"],
        }
    return snapshot, head


def plan(args, session):
    from .artifacts import load_release

    if not re.fullmatch(r"opencdr-[a-z][a-z0-9-]{0,17}", args.stack):
        raise FleetManagerError("Select an exact Core stack name")
    if not 1 <= len(args.reason) <= 512:
        raise FleetManagerError("A reason of 1–512 characters is required")
    release = load_release(args.directory, args.release_sha256) if args.directory else None
    snapshot, head = observe(session, args.stack, args.action, args.backfill_id, release)
    document = {
        "schema": SCHEMA,
        "action": args.action,
        "stack": args.stack,
        "account_id": args.account_id,
        "region": args.region,
        "reason": args.reason,
        "created_at": datetime.now(UTC).isoformat(),
        "snapshot": snapshot,
        "expected_revision": int(head.get("activation_revision", 0)),
        "release_directory": str(Path(args.directory).resolve()) if args.directory else None,
        "release_sha256": args.release_sha256,
        "history_complete": False,
    }
    with args.output.open("x") as file:
        file.write(json.dumps(document, indent=2) + "\n")
    args.output.chmod(0o600)
    return {
        "plan": str(args.output),
        "sha256": digest(document),
        "review": document,
        "applied": False,
    }


def apply(args, session):
    from .artifacts import load_release

    document = json.loads(args.plan.read_text())
    if document.get("schema") != SCHEMA or digest(document) != args.sha256:
        raise FleetManagerError("Activation plan digest/schema mismatch")
    if (document["account_id"], document["region"]) != (args.account_id, args.region):
        raise FleetManagerError("Activation plan belongs to another account or region")
    if document["action"] not in {"pause", "activate"}:
        raise FleetManagerError("Unknown activation action")
    release = (
        load_release(document["release_directory"], document["release_sha256"])
        if document["release_directory"]
        else None
    )
    snapshot, head = observe(
        session,
        document["stack"],
        document["action"],
        document["snapshot"].get("backfill", {}).get("id"),
        release,
    )
    ddb = aws_client(session, "dynamodb")
    table = snapshot["tables"]["DetectionFeedTable"]["name"]
    audit_key = {"pk": "activation#" + args.sha256, "sequence": 0}
    prior = ddb.get_item(TableName=table, Key=encode(audit_key), ConsistentRead=True).get("Item")
    revision = document["expected_revision"] + 1
    expected = dict(document["snapshot"])
    if prior and document["action"] == "activate":
        # Successful activation consumes the deployment barrier. It is the only
        # snapshot difference allowed on a retry of the same durable audit row.
        expected["deployment_pending"] = None
    if snapshot != expected:
        raise FleetManagerError(
            "Deployment, storage or backfill changed; create a fresh activation plan"
        )
    if prior:
        if int(head.get("activation_revision", 0)) != revision or (
            document["action"] == "activate"
            and head.get("activation", {}).get("plan_digest") != args.sha256
        ):
            raise FleetManagerError("Activation plan was already applied and superseded")
        return {
            "applied": True,
            "duplicate": True,
            "action": document["action"],
            "revision": revision,
        }
    actor = aws_client(session, "sts").get_caller_identity()["Arn"]
    at = datetime.now(UTC).isoformat()
    values = {
        ":old": document["expected_revision"],
        ":new": revision,
        ":epoch": snapshot["epoch"],
        ":table": snapshot["tables"]["DetectionFeedTable"]["id"],
    }
    condition = "epoch = :epoch AND table_id = :table AND " + (
        "activation_revision = :old"
        if "activation_revision" in head
        else "attribute_not_exists(activation_revision)"
    )
    if snapshot["deployment_pending"]:
        condition += " AND deployment_pending = :pending"
        values[":pending"] = snapshot["deployment_pending"]
    else:
        condition += " AND attribute_not_exists(deployment_pending)"
    if "activation_revision" not in head:
        values.pop(":old")
        if document["expected_revision"] != 0:
            raise FleetManagerError("Activation revision changed")
    update = "SET activation_revision = :new REMOVE activation"
    if document["action"] == "activate":
        values[":activation"] = {
            "runtime_sha256": snapshot["runtime_sha256"],
            "epoch": snapshot["epoch"],
            "table_id": snapshot["tables"]["DetectionFeedTable"]["id"],
            "plan_digest": args.sha256,
            "approved_at": at,
            "approved_by": actor,
        }
        update = (
            "SET activation_revision = :new, activation = :activation REMOVE deployment_pending"
        )
    try:
        ddb.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": table,
                        "Key": HEAD,
                        "UpdateExpression": update,
                        "ConditionExpression": condition,
                        "ExpressionAttributeValues": encode(values),
                    }
                },
                {
                    "Put": {
                        "TableName": table,
                        "Item": encode(
                            {
                                **audit_key,
                                "plan": document,
                                "applied_at": at,
                                "applied_by": actor,
                                "revision": revision,
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
            ]
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "TransactionCanceledException":
            raise FleetManagerError(
                "Activation state changed; inspect and retry the exact plan"
            ) from None
        raise
    return {
        "applied": True,
        "action": document["action"],
        "revision": revision,
        "history_complete": False,
    }


def configure_parser(actions):
    command = actions.add_parser(
        "detection-activation", help="plan/apply a Core read pause or activation"
    )
    modes = command.add_subparsers(dest="activation_command", required=True)
    prepare = modes.add_parser("plan")
    prepare.add_argument("stack")
    prepare.add_argument("--action", choices=["pause", "activate"], required=True)
    prepare.add_argument("--directory", type=Path)
    prepare.add_argument("--release-sha256")
    prepare.add_argument("--backfill-id")
    prepare.add_argument("--reason", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    execute = modes.add_parser("apply")
    execute.add_argument("plan", type=Path)
    execute.add_argument("--sha256", required=True)
    for parser in (prepare, execute):
        parser.add_argument("--profile", required=True)
        parser.add_argument("--account-id", required=True)
        parser.add_argument("--region", default="us-east-1")


def run(args):
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    verify_account(session, args.account_id)
    result = plan(args, session) if args.activation_command == "plan" else apply(args, session)
    print(json.dumps(result, indent=2))
    return 0
