"""Reviewed binding and resume of a coherently restored storage set.

Binding is a separate CloudFormation change from resuming workers. Failed updates
keep rollback disabled and the recovery lease held. Neither phase activates Core
reads, rewinds queues, deletes originals or asserts historical completeness.
"""

from __future__ import annotations

import base64
import copy
import hashlib

from . import storage_quarantine as q
from . import storage_recovery as recovery
from .artifacts import load_release
from .deployment_template import materialize
from .detection_activation import HEAD, decode, encode, resource_ids, template_body
from .fleet_api import FleetManagerError, aws_client, verify_account
from .storage_bindings import apply_binding, verify_physical
from .storage_snapshot import canonical, compare_fingerprints, sha256, table_fingerprint

SCHEMA = "opencdr.storage-cutover/1"
QUARANTINE_KEY = "OpenCDRStorageQuarantine"


def target_template(source, parameters, plan, receipt, *, phase):
    binding = receipt["binding"]
    target = apply_binding(source, binding, parameters=parameters)
    if phase in {"bind", "repair"}:
        for logical, row in target["Resources"].items():
            properties = row.get("Properties", {})
            if row["Type"] == "AWS::Lambda::Function" and plan["functions"][logical]["freeze"]:
                properties["ReservedConcurrentExecutions"] = 0
            elif row["Type"] == "AWS::Lambda::EventSourceMapping":
                properties["Enabled"] = False
                # Lambda validates SQS maximum concurrency even for disabled
                # mappings. An empty configuration is compatible with the zero
                # reserved-concurrency fence; resume restores the source limit.
                if "ScalingConfig" in properties:
                    properties["ScalingConfig"] = {}
            elif row["Type"] == "AWS::Events::Rule" and "ScheduleExpression" in properties:
                properties["State"] = "DISABLED"
        target.setdefault("Metadata", {})[QUARANTINE_KEY] = {"plan_sha256": sha256(plan)}
    elif phase != "resume":
        raise FleetManagerError("Unknown recovery cutover phase")
    return materialize(target, parameters)


def _lease(session, plan):
    s3 = aws_client(session, "s3")
    key = "opencdr-recovery/locks/" + sha256(plan["stack_id"]) + ".json"
    lock, etag = q._get(s3, plan["artifact_bucket"], key, plan["account_id"])
    if lock != {"plan_sha256": sha256(plan)}:
        raise FleetManagerError("This recovery no longer owns the deployment")
    return s3, key, etag


def _state(session, plan):
    s3, _, _ = _lease(session, plan)
    key = "opencdr-recovery/" + sha256(plan) + "/quarantine.json"
    value, etag = q._get(s3, plan["artifact_bucket"], key, plan["account_id"])
    if not value:
        raise FleetManagerError("Recovery has no durable quarantine state")
    return s3, key, value, etag


def _transition(session, plan, phase, change_set):
    s3, key, previous, etag = _state(session, plan)
    current = {**previous, "state": phase, "change_set_arn": change_set}
    if current != previous:
        s3.put_object(
            Bucket=plan["artifact_bucket"],
            Key=key,
            Body=canonical(current),
            ExpectedBucketOwner=plan["account_id"],
            ServerSideEncryption="AES256",
            IfMatch=etag,
        )
    return current


def verify_receipt(session, plan, receipt, digest):
    if (
        receipt.get("schema") != recovery.RECEIPT_SCHEMA
        or sha256(receipt) != digest
        or receipt.get("plan_sha256") != sha256(plan)
        or receipt["binding"]["snapshot_sha256"] != receipt["snapshot_sha256"]
        or (
            receipt["binding"]["account_id"],
            receipt["binding"]["region"],
            receipt["binding"]["stage"],
        )
        != (plan["account_id"], plan["region"], plan["stage"])
        or set(receipt["binding"]["resources"]) != set(plan["tables"]) | set(plan["buckets"])
    ):
        raise FleetManagerError("Restore receipt does not cover this reviewed deployment")
    verify_physical(session, receipt["binding"])
    ddb = aws_client(session, "dynamodb")
    s3 = aws_client(session, "s3")
    stored, _ = q._get(
        s3,
        plan["artifact_bucket"],
        "opencdr-recovery/" + sha256(plan) + "/restore.json",
        plan["account_id"],
    )
    capture, _ = q._get(
        s3,
        plan["artifact_bucket"],
        "opencdr-recovery/" + sha256(plan) + "/snapshot.json",
        plan["account_id"],
    )
    if stored != receipt or not capture or sha256(capture) != receipt["snapshot_sha256"]:
        raise FleetManagerError("Restore receipt differs from the sealed recovery records")
    for logical, source in plan["tables"].items():
        compare_fingerprints(
            capture["tables"][logical]["fingerprint"], receipt["fingerprints"][logical]
        )
        bound = receipt["binding"]["resources"][logical]
        if (bound["source_name"], bound["source_id"]) != (source["name"], source["id"]):
            raise FleetManagerError("Restored table has a different source")
        compare_fingerprints(
            receipt["fingerprints"][logical], table_fingerprint(ddb, bound["name"], bound["id"])
        )
    for logical, source in plan["buckets"].items():
        bound = receipt["binding"]["resources"][logical]
        if bound["source_name"] != source["name"]:
            raise FleetManagerError("Restored bucket has a different source")
        # Verify exact immutable object versions, not metadata alone.
        for row in receipt["object_versions"][logical]:
            raw, response = recovery.snapshot._get_bytes(
                s3, bound["name"], row["key"], plan["account_id"]
            )
            if (
                hashlib.sha256(raw).hexdigest() != row["sha256"]
                or response.get("VersionId") != row["restored_version_id"]
            ):
                raise FleetManagerError("Restored evidence changed before cutover")


def _verify_target(session, plan, template, *, frozen):
    verify_account(session, plan["account_id"])
    cfn, lambdas = aws_client(session, "cloudformation"), aws_client(session, "lambda")
    live = cfn.describe_stacks(StackName=plan["stack_id"])["Stacks"][0]
    if live["StackStatus"] != "UPDATE_COMPLETE" or sha256(
        template_body(cfn, StackName=plan["stack_id"])
    ) != sha256(template):
        raise FleetManagerError("Reviewed recovery deployment has not completed")
    ids = resource_ids(cfn, plan["stack_id"])
    for logical, source in plan["functions"].items():
        config = lambdas.get_function_configuration(FunctionName=ids[logical])
        if (
            config["FunctionArn"] != source["arn"]
            or config.get("State") != "Active"
            or config.get("LastUpdateStatus") != "Successful"
            or base64.b64decode(config["CodeSha256"]).hex() != plan["runtime_sha256"]
        ):
            raise FleetManagerError("Recovered runtime identity changed")
        concurrency = lambdas.get_function_concurrency(FunctionName=ids[logical]).get(
            "ReservedConcurrentExecutions"
        )
        if concurrency != (0 if frozen and source["freeze"] else source["concurrency"]):
            raise FleetManagerError("Recovered worker concurrency differs from review")
        drift = cfn.detect_stack_resource_drift(
            StackName=plan["stack_id"], LogicalResourceId=logical
        )["StackResourceDrift"]
        if drift["StackResourceDriftStatus"] != "IN_SYNC":
            raise FleetManagerError("Recovered function configuration differs from its template")
    events = aws_client(session, "events")
    for logical, resource in template["Resources"].items():
        if resource["Type"] == "AWS::Lambda::EventSourceMapping":
            mapping = lambdas.get_event_source_mapping(UUID=ids[logical])
            desired = False if frozen else resource["Properties"].get("Enabled", True)
            if type(desired) is not bool or mapping["State"] != (
                "Enabled" if desired else "Disabled"
            ):
                raise FleetManagerError("Recovered event mapping is not in the reviewed state")
            source = resource["Properties"]["EventSourceArn"]
            if isinstance(source, str) and mapping["EventSourceArn"] != source:
                raise FleetManagerError("Recovered stream binding differs from its template")
        elif resource["Type"] == "AWS::IAM::Role":
            drift = cfn.detect_stack_resource_drift(
                StackName=plan["stack_id"], LogicalResourceId=logical
            )["StackResourceDrift"]
            if drift["StackResourceDriftStatus"] != "IN_SYNC":
                raise FleetManagerError("Recovered role permissions differ from their template")
    for name, original in plan["rules"].items():
        rule = events.describe_rule(Name=name)
        if rule["State"] != ("DISABLED" if frozen else original["state"]):
            raise FleetManagerError("Recovered schedule is not in the reviewed state")
    return live


def _repair_delta(before, target, binding=None):
    """Only repair frozen mapping scaling or split a failed stream replacement."""
    repaired = copy.deepcopy(before)
    changed = []
    for logical, resource in repaired["Resources"].items():
        properties = resource.get("Properties", {})
        if resource["Type"] == "AWS::Lambda::EventSourceMapping" and properties.get(
            "ScalingConfig"
        ):
            if properties.get("Enabled") is not False:
                raise FleetManagerError("Repair requires every affected mapping to stay disabled")
            properties["ScalingConfig"] = {}
            changed.append(logical)
    compared = copy.deepcopy(target)
    for logical, old in list(repaired["Resources"].items()):
        if old["Type"] != "AWS::Lambda::EventSourceMapping" or logical not in compared["Resources"]:
            continue
        kept = compared["Resources"][logical]
        if kept == old:
            continue
        for resource_name, row in (binding or {}).get("resources", {}).items():
            if old["Properties"].get("EventSourceArn") != row["stream_arn"]:
                continue
            expected_kept = copy.deepcopy(old)
            expected_kept["Properties"]["EventSourceArn"] = {
                "Fn::GetAtt": [resource_name, "StreamArn"]
            }
            twins = [
                name
                for name, value in compared["Resources"].items()
                if name not in repaired["Resources"]
                and name.startswith("RecoveredMapping")
                and value == old
            ]
            if (
                kept == expected_kept
                and len(twins) == 1
                and old["Properties"].get("Enabled") is False
                and old.get("DeletionPolicy") == old.get("UpdateReplacePolicy") == "Retain"
            ):
                repaired["Resources"][logical] = copy.deepcopy(kept)
                repaired["Resources"][twins[0]] = copy.deepcopy(old)
                changed.append(logical)
                break
    # The portable materializer fingerprints the entire template into the API
    # deployment logical ID. Only its name may change here, never its definition.
    deployments = []
    for document in (repaired, compared):
        definitions = []
        names = {}
        for name, resource in list(document["Resources"].items()):
            if resource["Type"] == "AWS::ApiGateway::Deployment":
                definitions.append(sha256(resource))
                names[name] = "ReviewedApiDeployment" + sha256(resource)
                del document["Resources"][name]

        def normalize_references(value, names=names):
            if isinstance(value, dict):
                return {key: normalize_references(item) for key, item in value.items()}
            if isinstance(value, list):
                return [normalize_references(item) for item in value]
            if isinstance(value, str):
                if value in names:
                    return names[value]
                for previous, current in names.items():
                    value = value.replace("${" + previous + "}", "${" + current + "}")
                    value = value.replace("${" + previous + ".", "${" + current + ".")
            return value

        normalized = normalize_references(document)
        document.clear()
        document.update(normalized)
        deployments.append(sorted(definitions))
    if not changed or repaired != compared or deployments[0] != deployments[1]:
        raise FleetManagerError(
            "Repair may only correct frozen mapping scaling or stream replacement"
        )
    return set(changed)


def _verify_failed_binding(session, plan, target, failed_change, binding=None):
    """Review a failed bind without weakening its existing execution fence.

    CFN cannot detect drift in UPDATE_FAILED. Require the narrow template delta,
    exact failed resources, pinned code, zero concurrency and unchanged disabled
    sources instead. Normal post-bind drift verification still precedes resume.
    """
    if plan["mode"] not in {"core", "fleet"}:
        raise FleetManagerError("Unsupported recovery repair owner")
    _, _, state, _ = _state(session, plan)
    if state["state"] != "binding_started":
        raise FleetManagerError("Repair requires a failed frozen binding")
    verify_account(session, plan["account_id"])
    cfn, lm = aws_client(session, "cloudformation"), aws_client(session, "lambda")
    live = cfn.describe_stacks(StackName=plan["stack_id"])["Stacks"][0]
    before = template_body(cfn, StackName=plan["stack_id"])
    if (
        live["StackStatus"] != "UPDATE_FAILED"
        or live.get("ChangeSetId") != failed_change
        or template_body(cfn, ChangeSetName=failed_change) != before
    ):
        raise FleetManagerError("Failed binding changed before its repair")
    allowed = _repair_delta(before, target, binding)
    resources = []
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=plan["stack_id"]):
        resources.extend(page["StackResourceSummaries"])
    # Preserve-on-failure can roll back only the failed resources while leaving
    # the stack UPDATE_FAILED and all successfully updated resources in place.
    failed_states = {"UPDATE_FAILED", "UPDATE_ROLLBACK_COMPLETE"}
    failed = {r["LogicalResourceId"] for r in resources if r["ResourceStatus"] in failed_states}
    if (
        not failed
        or not failed <= allowed
        or any(
            r["ResourceStatus"] not in {"UPDATE_COMPLETE", "CREATE_COMPLETE"} | failed_states
            for r in resources
        )
    ):
        raise FleetManagerError("Failed resources exceed the supported mapping repair")
    observed = {"functions": {}, "mappings": {}, "rules": {}}
    for logical, source in plan["functions"].items():
        config = lm.get_function_configuration(FunctionName=source["name"])
        concurrency = lm.get_function_concurrency(FunctionName=source["name"])
        if (
            config["FunctionArn"] != source["arn"]
            or config.get("State") != "Active"
            or config.get("LastUpdateStatus") != "Successful"
            or base64.b64decode(config["CodeSha256"]).hex() != plan["runtime_sha256"]
            or concurrency.get("ReservedConcurrentExecutions")
            != (0 if source["freeze"] else source["concurrency"])
            or q._cloud_time(concurrency) < state["frozen_at"] + plan["drain_seconds"]
        ):
            raise FleetManagerError("Failed binding lost its pinned runtime or execution fence")
        observed["functions"][logical] = q.configuration_digest(config)
    for source in plan["mappings"].values():
        mapping = lm.get_event_source_mapping(UUID=source["UUID"])
        if mapping["State"] != "Disabled" or any(
            mapping[key] != source[key] for key in ["FunctionArn", "EventSourceArn"]
        ):
            raise FleetManagerError("Failed binding source mapping escaped its fence")
        observed["mappings"][source["UUID"]] = {
            key: mapping.get(key)
            for key in [
                "FunctionArn",
                "EventSourceArn",
                "State",
                "ScalingConfig",
                "BatchSize",
                "MaximumBatchingWindowInSeconds",
                "FunctionResponseTypes",
            ]
        }
    events = aws_client(session, "events")
    for name, source in plan["rules"].items():
        rule = events.describe_rule(Name=name)
        targets = events.list_targets_by_rule(Rule=name)
        if (
            rule["State"] != "DISABLED"
            or rule.get("ScheduleExpression") != source["schedule"]
            or targets.get("NextToken")
            or sha256(targets["Targets"]) != source["targets_sha256"]
        ):
            raise FleetManagerError("Failed binding schedule escaped its fence")
        observed["rules"][name] = source["targets_sha256"]
    return sha256(observed)


def prepare(session, plan, receipt, receipt_digest, directory, *, phase):
    q.validate_plan(plan, sha256(plan))
    _lease(session, plan)
    manifest, source = load_release(directory, plan["release_sha256"])
    if manifest["files"]["runtime.zip"] != plan["runtime_sha256"]:
        raise FleetManagerError("Cutover must keep the reviewed source runtime")
    cfn = aws_client(session, "cloudformation")
    live = cfn.describe_stacks(StackName=plan["stack_id"])["Stacks"][0]
    parameters = {p["ParameterKey"]: p["ParameterValue"] for p in live["Parameters"]}
    verify_receipt(session, plan, receipt, receipt_digest)
    repair = {}
    if phase == "bind":
        q.require_drained(session, plan, sha256(plan))
        stored, _ = q._get(
            aws_client(session, "s3"),
            plan["artifact_bucket"],
            "opencdr-recovery/" + sha256(plan) + "/snapshot.json",
            plan["account_id"],
        )
        if not stored or sha256(stored) != receipt["snapshot_sha256"]:
            raise FleetManagerError("Restored snapshot is not the sealed capture")
        recovery.verify_capture(session, plan, stored)
    elif phase == "repair":
        _, _, state, _ = _state(session, plan)
        repair = {"repair_from": state["change_set_arn"]}
        repair["repair_observation_sha256"] = _verify_failed_binding(
            session,
            plan,
            target_template(source, parameters, plan, receipt, phase="bind"),
            repair["repair_from"],
            receipt["binding"],
        )
    elif phase == "resume":
        _, _, state, _ = _state(session, plan)
        if state["state"] != "bound_frozen":
            raise FleetManagerError("Finish the frozen binding before preparing resume")
        _verify_target(
            session,
            plan,
            target_template(source, parameters, plan, receipt, phase="bind"),
            frozen=True,
        )
    else:
        raise FleetManagerError("Unknown cutover phase")
    target = target_template(source, parameters, plan, receipt, phase=phase)
    before = template_body(cfn, StackName=plan["stack_id"])
    for logical, row in before["Resources"].items():
        if (
            row["Type"] in {"AWS::DynamoDB::Table", "AWS::S3::Bucket", "AWS::SQS::Queue"}
            and target["Resources"].get(logical, {}).get("Properties") != row["Properties"]
        ):
            raise FleetManagerError("Recovery cannot mutate original storage or queues")
    s3 = aws_client(session, "s3")
    prefix = "opencdr-recovery/" + sha256(plan) + "/" + phase
    # Keep invalid/superseded preparations immutable without preventing a fixed
    # candidate from being reviewed under the same frozen recovery plan.
    template_key = prefix + "/templates/" + sha256(target) + ".json"
    recovery._persist(s3, plan, template_key, target)
    request = {
        "schema": SCHEMA,
        "phase": phase,
        "plan_sha256": sha256(plan),
        "receipt_sha256": receipt_digest,
        "stack_id": plan["stack_id"],
        "previous_template_sha256": sha256(before),
        "template_sha256": sha256(target),
        "parameters": parameters,
        "release_sha256": plan["release_sha256"],
        **repair,
    }
    name = "opencdr-recovery-" + phase + "-" + sha256(request)[:32]
    review_key = prefix + "/changes/" + sha256(request) + ".json"
    prior, _ = q._get(s3, plan["artifact_bucket"], review_key, plan["account_id"])
    if prior:
        if {k: prior[k] for k in request} != request:
            raise FleetManagerError("Stored recovery change differs from the current review")
        return prior
    # A deterministic name reconciles an uncertain successful CreateChangeSet.
    from botocore.exceptions import ClientError

    try:
        created = cfn.describe_change_set(StackName=plan["stack_id"], ChangeSetName=name)
        change_arn = created["ChangeSetId"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "ChangeSetNotFound":
            raise
        created = cfn.create_change_set(
            StackName=plan["stack_id"],
            ChangeSetName=name,
            ChangeSetType="UPDATE",
            TemplateURL=f"https://{plan['artifact_bucket']}.s3.{plan['region']}.amazonaws.com/{template_key}",
            Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()],
            Capabilities=["CAPABILITY_NAMED_IAM"],
            Description="Reviewed storage " + phase + " " + sha256(request),
        )
        change_arn = created["Id"]
    cfn.get_waiter("change_set_create_complete").wait(
        ChangeSetName=change_arn, WaiterConfig={"Delay": 5, "MaxAttempts": 60}
    )
    review = cfn.describe_change_set(ChangeSetName=change_arn)
    if review["Status"] != "CREATE_COMPLETE" or review["ExecutionStatus"] != "AVAILABLE":
        raise FleetManagerError("Recovery change is not available for review")
    changes = list(review.get("Changes", []))
    while review.get("NextToken"):
        review = cfn.describe_change_set(ChangeSetName=change_arn, NextToken=review["NextToken"])
        changes.extend(review.get("Changes", []))
    if sha256(template_body(cfn, ChangeSetName=change_arn)) != request["template_sha256"]:
        raise FleetManagerError("CloudFormation changed the reviewed recovery template")
    for entry in changes:
        row = entry["ResourceChange"]
        if (
            row["Action"] == "Remove"
            and row.get("PolicyAction") != "Retain"
            and row["ResourceType"] != "AWS::ApiGateway::Deployment"
        ):
            raise FleetManagerError("Recovery would delete an unretained resource")
        if row.get("Replacement") == "True":
            raise FleetManagerError("Recovery cannot replace resources while rollback is disabled")
    request.update(change_set_arn=change_arn, changes=changes, executed=False)
    return recovery._persist(s3, plan, review_key, request)


def execute(session, plan, receipt, change, expected_digest, directory):
    if (
        sha256(change) != expected_digest
        or change.get("schema") != SCHEMA
        or change["plan_sha256"] != sha256(plan)
        or change["receipt_sha256"] != sha256(receipt)
    ):
        raise FleetManagerError("Reviewed recovery change digest mismatch")
    _lease(session, plan)
    manifest, source = load_release(directory, plan["release_sha256"])
    target = target_template(source, change["parameters"], plan, receipt, phase=change["phase"])
    cfn = aws_client(session, "cloudformation")
    review = cfn.describe_change_set(ChangeSetName=change["change_set_arn"])
    if review["StackId"] != plan["stack_id"] or sha256(
        template_body(cfn, ChangeSetName=change["change_set_arn"])
    ) != sha256(target):
        raise FleetManagerError("Recovery change no longer matches the review")
    if review["ExecutionStatus"] in {"EXECUTE_IN_PROGRESS", "EXECUTE_COMPLETE"}:
        live = cfn.describe_stacks(StackName=plan["stack_id"])["Stacks"][0]
        if live.get("ChangeSetId") != change["change_set_arn"]:
            raise FleetManagerError("Another deployment superseded this recovery")
        return {
            "state": "started",
            "change_set_arn": change["change_set_arn"],
            "phase": change["phase"],
        }
    if review["ExecutionStatus"] != "AVAILABLE" or review["Status"] != "CREATE_COMPLETE":
        raise FleetManagerError("Recovery change is unavailable")
    verify_receipt(session, plan, receipt, change["receipt_sha256"])
    if change["phase"] == "repair":
        observed = _verify_failed_binding(
            session, plan, target, change["repair_from"], receipt["binding"]
        )
        if observed != change["repair_observation_sha256"]:
            raise FleetManagerError("Failed binding observations changed after repair review")
        phase = "binding_started"
    elif change["phase"] == "bind":
        q.verify_source(session, plan, frozen=True)
        _, _, state, _ = _state(session, plan)
        if state["state"] not in {"frozen", "binding_started"}:
            raise FleetManagerError("Recovery is no longer frozen for binding")
        # require_drained also proves the sealed source has not been resumed.
        if state["state"] == "frozen":
            q.require_drained(session, plan, sha256(plan))
        stored, _ = q._get(
            aws_client(session, "s3"),
            plan["artifact_bucket"],
            "opencdr-recovery/" + sha256(plan) + "/snapshot.json",
            plan["account_id"],
        )
        # Verify the complete source immediately before execution, including
        # claims. Retrying binding_started keeps the same drain timestamp.
        if not stored or sha256(stored) != receipt["snapshot_sha256"]:
            raise FleetManagerError("Sealed snapshot differs from the restored receipt")
        for logical, table in stored["tables"].items():
            src = plan["tables"][logical]
            compare_fingerprints(
                table["fingerprint"],
                table_fingerprint(aws_client(session, "dynamodb"), src["name"], src["id"]),
            )
        for bucket in stored["buckets"].values():
            recovery.snapshot.verify_source_objects(
                aws_client(session, "s3"), bucket, plan["account_id"]
            )
        phase = "binding_started"
    else:
        _, _, state, _ = _state(session, plan)
        if state["state"] not in {"bound_frozen", "resume_started"}:
            raise FleetManagerError("Recovery is no longer frozen for resume")
        _verify_target(
            session,
            plan,
            target_template(source, change["parameters"], plan, receipt, phase="bind"),
            frozen=True,
        )
        phase = "resume_started"
    if sha256(template_body(cfn, StackName=plan["stack_id"])) != change["previous_template_sha256"]:
        raise FleetManagerError("Deployment changed after recovery review")
    _transition(session, plan, phase, change["change_set_arn"])
    # Rollback could remove the explicit concurrency fence from a partially
    # updated function. Keep failed updates frozen for reviewed repair instead.
    cfn.execute_change_set(ChangeSetName=change["change_set_arn"], DisableRollback=True)
    return {
        "state": "started",
        "phase": change["phase"],
        "change_set_arn": change["change_set_arn"],
    }


def complete(session, plan, receipt, change, expected_digest, directory):
    if (
        sha256(change) != expected_digest
        or change.get("schema") != SCHEMA
        or change["plan_sha256"] != sha256(plan)
        or change["receipt_sha256"] != sha256(receipt)
    ):
        raise FleetManagerError("Reviewed recovery change digest mismatch")
    # Reconcile a lost completion/lease-release response before rerunning work.
    s3 = aws_client(session, "s3")
    recorded, _ = q._get(
        s3,
        plan["artifact_bucket"],
        "opencdr-recovery/" + sha256(plan) + "/completed.json",
        plan["account_id"],
    )
    if recorded:
        if recorded["change_set_arn"] != change["change_set_arn"] or recorded[
            "receipt_sha256"
        ] != sha256(receipt):
            raise FleetManagerError("Recovery completed with another reviewed change")
        lock_key = "opencdr-recovery/locks/" + sha256(plan["stack_id"]) + ".json"
        lock, etag = q._get(s3, plan["artifact_bucket"], lock_key, plan["account_id"])
        if lock == {"plan_sha256": sha256(plan)}:
            _transition(session, plan, "bound_resumed", change["change_set_arn"])
            s3.put_object(
                Bucket=plan["artifact_bucket"],
                Key=lock_key,
                Body=canonical({"released_plan_sha256": sha256(plan)}),
                ExpectedBucketOwner=plan["account_id"],
                ServerSideEncryption="AES256",
                IfMatch=etag,
            )
        return recorded
    _lease(session, plan)
    _, source = load_release(directory, plan["release_sha256"])
    phase = change["phase"]
    target = target_template(source, change["parameters"], plan, receipt, phase=phase)
    live = _verify_target(session, plan, target, frozen=phase in {"bind", "repair"})
    if live.get("ChangeSetId") != change["change_set_arn"]:
        raise FleetManagerError("Another deployment superseded recovery")
    verify_physical(session, receipt["binding"])
    if phase in {"bind", "repair"}:
        verify_receipt(session, plan, receipt, change["receipt_sha256"])
        _transition(session, plan, "bound_frozen", change["change_set_arn"])
        return {
            "state": "bound_frozen",
            "original_resources_retained": True,
            "reads_activated": False,
        }
    # Any replaced old stream mappings were retained, not destroyed. Resume
    # their exact source/function pair too; delivery/outbox claims were not rewound.
    lm = aws_client(session, "lambda")
    pending = []
    for row in plan["mappings"].values():
        value = lm.get_event_source_mapping(UUID=row["UUID"])
        if any(value[k] != row[k] for k in ["FunctionArn", "EventSourceArn"]):
            raise FleetManagerError("Retained source mapping identity changed")
        if value["State"] == row["State"]:
            continue
        if value["State"] in {"Enabling", "Disabling"}:
            pending.append(row["UUID"])
            continue
        if value["State"] not in {"Enabled", "Disabled"}:
            raise FleetManagerError("Retained mapping is unavailable")
        lm.update_event_source_mapping(UUID=row["UUID"], Enabled=row["State"] == "Enabled")
        pending.append(row["UUID"])
    if pending:
        return {"state": "resuming_retained_streams", "pending": pending}
    ddb = aws_client(session, "dynamodb")
    if plan["mode"] == "core":
        table = receipt["binding"]["resources"]["DetectionFeedTable"]["name"]
        head = decode(ddb.get_item(TableName=table, Key=HEAD, ConsistentRead=True)["Item"])
        if head.get("activation"):
            raise FleetManagerError("Recovered Core was prematurely activated")
        if head.get("deployment_pending") != change["change_set_arn"]:
            values = {
                ":next": change["change_set_arn"],
                ":revision": int(head.get("activation_revision", 0)) + 1,
            }
            conditions = ["attribute_not_exists(activation)"]
            for key in [
                "epoch",
                "table_id",
                "last_sequence",
                "activation_revision",
                "deployment_pending",
            ]:
                if key in head:
                    conditions.append(key + " = :" + key)
                    values[":" + key] = head[key]
                else:
                    conditions.append("attribute_not_exists(" + key + ")")
            ddb.update_item(
                TableName=table,
                Key=HEAD,
                UpdateExpression="SET deployment_pending = :next, activation_revision = :revision",
                ConditionExpression=" AND ".join(conditions),
                ExpressionAttributeValues=encode(values),
            )
    for logical, source_table in plan["tables"].items():
        ttl = source_table["spec"].get("TimeToLiveSpecification")
        if ttl and ttl.get("Enabled") is True:
            name = receipt["binding"]["resources"][logical]["name"]
            status = ddb.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
            if status.get("TimeToLiveStatus") == "DISABLED":
                ddb.update_time_to_live(
                    TableName=name,
                    TimeToLiveSpecification={
                        "Enabled": True,
                        "AttributeName": ttl["AttributeName"],
                    },
                )
            elif status.get("AttributeName") != ttl["AttributeName"]:
                raise FleetManagerError("Restored TTL differs from the source specification")
    _, _, state, _ = _state(session, plan)
    result = {
        "state": "bound_resumed",
        "plan_sha256": sha256(plan),
        "receipt_sha256": change["receipt_sha256"],
        "change_set_arn": change["change_set_arn"],
        "binding": receipt["binding"],
        "original_resources_retained": True,
        "reads_activated": False,
        "history_complete": False,
        "freeze_started_at": state["frozen_at"],
        "completed_at": q.datetime.now(q.UTC).isoformat(),
    }
    s3, key, etag = _lease(session, plan)
    recovery._persist(s3, plan, "opencdr-recovery/" + sha256(plan) + "/completed.json", result)
    _transition(session, plan, "bound_resumed", change["change_set_arn"])
    s3.put_object(
        Bucket=plan["artifact_bucket"],
        Key=key,
        Body=canonical({"released_plan_sha256": sha256(plan)}),
        ExpectedBucketOwner=plan["account_id"],
        ServerSideEncryption="AES256",
        IfMatch=etag,
    )
    return result
