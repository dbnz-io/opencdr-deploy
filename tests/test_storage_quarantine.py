import base64
import copy
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from opencdr_deploy import storage_quarantine as q
from opencdr_deploy.fleet_api import FleetManagerError
from opencdr_deploy.storage_snapshot import sha256


@pytest.fixture
def system(monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="recovery-artifacts")
        cfn, ddb, lambdas, events, sts = [Mock() for _ in range(5)]
        template = {"Resources": {}}
        account = "123456789012"
        stack_id = f"arn:aws:cloudformation:us-east-1:{account}:stack/opencdr-verify/one"
        functions = {
            name: {
                "name": name,
                "arn": f"arn:aws:lambda:us-east-1:{account}:function:{name}",
                "revision_id": "revision-1",
                "code_sha256": base64.b64encode(b"0" * 32).decode(),
                "timeout": 900,
                "freeze": name not in q.CORE_CONTINUING,
                "concurrency": None,
            }
            for name in q.CORE_FUNCTIONS
        }
        plan = {
            "schema": q.SCHEMA,
            "mode": "core",
            "account_id": account,
            "region": "us-east-1",
            "stage": "verify",
            "stack_name": "opencdr-verify",
            "stack_id": stack_id,
            "template_sha256": sha256(template),
            "functions": functions,
            "tables": {name: {"name": name, "id": "physical-" + name} for name in q.CORE_TABLES},
            "drain_seconds": q.DRAIN_SECONDS,
            "artifact_bucket": "recovery-artifacts",
            "rules": {
                "publisher": {
                    "state": "ENABLED",
                    "schedule": "rate(1 minute)",
                    "targets_sha256": sha256([]),
                }
            },
            "mappings": {
                "mapping": {
                    "UUID": "mapping",
                    "FunctionArn": functions["SignalWriterLambdaFunction"]["arn"],
                    "EventSourceArn": "arn:aws:sqs:us-east-1:123456789012:existing-queue",
                    "State": "Enabled",
                }
            },
        }
        cfn.describe_stacks.return_value = {
            "Stacks": [{"StackId": stack_id, "StackStatus": "UPDATE_COMPLETE"}]
        }
        cfn.get_template.return_value = {"TemplateBody": template}
        ddb.describe_table.side_effect = lambda **kw: {
            "Table": {"TableId": "physical-" + kw["TableName"], "TableStatus": "ACTIVE"}
        }
        sts.get_caller_identity.return_value = {"Account": account}
        concurrency = {name: None for name in functions}
        configurations = {
            name: {
                "FunctionArn": r["arn"],
                "RevisionId": r["revision_id"],
                "CodeSha256": r["code_sha256"],
                "LastUpdateStatus": "Successful",
            }
            for name, r in functions.items()
        }
        for name in functions:
            functions[name]["configuration_sha256"] = q.configuration_digest(configurations[name])
        lambdas.get_function_configuration.side_effect = lambda **kw: configurations[
            kw["FunctionName"]
        ]
        clock = {"date": "Tue, 10 Nov 2026 12:00:00 GMT"}

        def get_concurrency(**kw):
            n = concurrency[kw["FunctionName"]]
            return {
                "ResponseMetadata": {"HTTPHeaders": clock},
                **({"ReservedConcurrentExecutions": n} if n is not None else {}),
            }

        lambdas.get_function_concurrency.side_effect = get_concurrency
        lambdas.put_function_concurrency.side_effect = lambda **kw: concurrency.update(
            {kw["FunctionName"]: kw["ReservedConcurrentExecutions"]}
        )
        lambdas.delete_function_concurrency.side_effect = lambda **kw: concurrency.update(
            {kw["FunctionName"]: None}
        )
        mapping = copy.deepcopy(plan["mappings"]["mapping"])
        lambdas.get_event_source_mapping.side_effect = lambda **_: copy.deepcopy(mapping)

        def set_mapping(**kw):
            mapping["State"] = "Enabled" if kw["Enabled"] else "Disabled"
            return copy.deepcopy(mapping)

        lambdas.update_event_source_mapping.side_effect = set_mapping
        rule = {"State": "ENABLED", "ScheduleExpression": "rate(1 minute)"}
        events.describe_rule.side_effect = lambda **_: copy.deepcopy(rule)
        events.list_targets_by_rule.return_value = {"Targets": []}
        events.disable_rule.side_effect = lambda **_: rule.update(State="DISABLED")
        events.enable_rule.side_effect = lambda **_: rule.update(State="ENABLED")
        clients = {
            "s3": s3,
            "cloudformation": cfn,
            "lambda": lambdas,
            "dynamodb": ddb,
            "events": events,
            "sts": sts,
        }
        session = Mock()
        session.client.side_effect = lambda service, **_: clients[service]
        monkeypatch.setattr(q, "require_paused", Mock())
        yield session, plan, concurrency, mapping, rule, clock, configurations


def test_freeze_drains_without_discarding_ingress_then_resumes_exact_source(system):
    session, plan, concurrency, mapping, rule, clock, _ = system
    digest = sha256(plan)
    assert q.freeze(session, plan, digest)["state"] == "disabling_mappings"
    assert all(v is None for v in concurrency.values())  # queued source first
    state = q.freeze(session, plan, digest)
    assert state["state"] == "frozen" and not state["drained"]
    assert concurrency["ProcessorLambdaFunction"] is None
    assert concurrency["SignalWriterLambdaFunction"] == 0
    with pytest.raises(FleetManagerError, match="fully drained"):
        q.require_drained(session, plan, digest)
    clock["date"] = "Tue, 10 Nov 2026 12:16:01 GMT"
    assert q.require_drained(session, plan, digest)["frozen_at"] == state["frozen_at"]
    assert q.freeze(session, plan, digest)["drained"] is True  # retry preserves deadline
    assert q.resume_source(session, plan, digest)["state"] == "resuming_source"
    assert q.resume_source(session, plan, digest)["state"] == "source_resumed"
    assert all(v is None for v in concurrency.values())
    assert mapping["State"] == "Enabled" and rule["State"] == "ENABLED"
    assert q.resume_source(session, plan, digest)["state"] == "source_resumed"
    with pytest.raises(FleetManagerError, match="cannot be frozen again"):
        q.freeze(session, plan, digest)


def test_competing_recovery_is_rejected_before_any_writer_is_changed(system):
    session, plan, concurrency, *_ = system
    q.freeze(session, plan, sha256(plan))
    other = {**plan, "reason": "another operator"}
    with pytest.raises(FleetManagerError, match="Another recovery"):
        q.freeze(session, other, sha256(other))
    assert all(v is None for v in concurrency.values())


def test_replaced_runtime_blocks_resume_and_source_is_not_implicitly_reenabled(system):
    session, plan, concurrency, _, _, _, configs = system
    q.freeze(session, plan, sha256(plan))
    q.freeze(session, plan, sha256(plan))
    configs["SignalWriterLambdaFunction"]["Environment"] = {"Variables": {"TABLE": "wrong"}}
    with pytest.raises(FleetManagerError, match="runtime changed"):
        q.resume_source(session, plan, sha256(plan))
    assert concurrency["SignalWriterLambdaFunction"] == 0


def test_changed_stream_or_service_clock_blocks_capture(system):
    session, plan, _, mapping, _, clock, _ = system
    q.freeze(session, plan, sha256(plan))
    q.freeze(session, plan, sha256(plan))
    mapping["EventSourceArn"] = "different-stream"
    with pytest.raises(FleetManagerError, match="not safely quarantined"):
        q.require_drained(session, plan, sha256(plan))
    mapping.update(plan["mappings"]["mapping"], State="Disabled")
    clock.clear()
    with pytest.raises(FleetManagerError, match="no service clock"):
        q.require_drained(session, plan, sha256(plan))


def test_tampering_with_drain_or_writer_set_fails_before_aws(system):
    session, plan, *_ = system
    plan["drain_seconds"] = 0
    with pytest.raises(FleetManagerError, match="digest or schema"):
        q.freeze(session, plan, sha256(plan))
    plan["drain_seconds"] = q.DRAIN_SECONDS
    plan["functions"]["ProcessorLambdaFunction"]["freeze"] = True
    with pytest.raises(FleetManagerError, match="writer fence"):
        q.freeze(session, plan, sha256(plan))
    session.client.assert_not_called()


def test_concurrency_revision_change_preserves_pinned_functional_configuration(system):
    session, plan, _, _, _, _, configs = system
    q.freeze(session, plan, sha256(plan))
    for config in configs.values():
        config["RevisionId"] = "concurrency-changed-revision"
    assert q.freeze(session, plan, sha256(plan))["state"] == "frozen"


def test_normal_deployment_cannot_enter_an_active_quarantine(system):
    session, plan, *_ = system
    stack = {
        "StackId": plan["stack_id"],
        "Parameters": [
            {"ParameterKey": "ArtifactBucket", "ParameterValue": plan["artifact_bucket"]}
        ],
    }
    q.require_unlocked(session, stack)
    q.freeze(session, plan, sha256(plan))
    with pytest.raises(FleetManagerError, match="storage recovery owns"):
        q.require_unlocked(session, stack)
