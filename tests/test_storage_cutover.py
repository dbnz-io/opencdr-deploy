import copy
from unittest.mock import Mock

import pytest
from test_storage_bindings import binding, template  # noqa: F401

from opencdr_deploy import storage_cutover as c
from opencdr_deploy.deployment_template import materialize
from opencdr_deploy.fleet_api import FleetManagerError
from opencdr_deploy.storage_snapshot import sha256


def test_binding_and_resume_templates_keep_storage_and_runtime_together(template, binding):  # noqa: F811
    template["Resources"]["Function"]["Properties"]["Environment"]["Variables"]["RUNTIME_ID"] = (
        "pinned"
    )
    template["Resources"]["Function"]["Properties"]["Code"] = {
        "S3Bucket": "pinned",
        "S3Key": "runtime",
    }
    template["Resources"]["Schedule"] = {
        "Type": "AWS::Events::Rule",
        "Properties": {"ScheduleExpression": "rate(1 minute)", "State": "ENABLED"},
    }
    template["Resources"]["Mapping"]["Properties"]["Enabled"] = True
    template["Resources"]["Mapping"]["Properties"]["ScalingConfig"] = {"MaximumConcurrency": 2}
    template["Resources"]["Queue"] = {
        "Type": "AWS::SQS::Queue",
        "Properties": {
            "QueueName": {"Fn::Sub": "opencdr-${Stage}-buffer"},
            "VisibilityTimeout": 900,
            "RedrivePolicy": {
                "deadLetterTargetArn": {"Fn::GetAtt": ["DeadLetterQueue", "Arn"]},
                "maxReceiveCount": 3,
            },
        },
    }
    template["Resources"]["BusinessRule"] = {
        "Type": "AWS::Events::Rule",
        "Properties": {"EventPattern": {"source": ["aws.cloudtrail"]}, "State": "ENABLED"},
    }
    plan = {"functions": {"Function": {"freeze": True}}, "runtime_sha256": "pinned"}
    receipt = {"binding": binding}
    before = copy.deepcopy(template)
    frozen = c.target_template(template, {"Stage": "dev"}, plan, receipt, phase="bind")
    props = frozen["Resources"]["Function"]["Properties"]
    assert props["ReservedConcurrentExecutions"] == 0
    assert props["Environment"]["Variables"]["TABLE"] == {
        "Fn::Sub": "opencdr-dev-signals-recovered"
    }
    assert props["Code"] == before["Resources"]["Function"]["Properties"]["Code"]
    assert frozen["Resources"]["Queue"] == before["Resources"]["Queue"]
    assert frozen["Resources"]["Mapping"]["Properties"]["Enabled"] is False
    assert frozen["Resources"]["Mapping"]["Properties"]["ScalingConfig"] == {}
    assert frozen["Resources"]["Mapping"]["UpdateReplacePolicy"] == "Retain"
    assert frozen["Resources"]["Schedule"]["Properties"]["State"] == "DISABLED"
    assert frozen["Resources"]["BusinessRule"] == before["Resources"]["BusinessRule"]
    assert (
        frozen["Resources"]["Signals"]["Properties"] == before["Resources"]["Signals"]["Properties"]
    )
    resumed = c.target_template(template, {"Stage": "dev"}, plan, receipt, phase="resume")
    assert resumed == materialize(template, {"Stage": "dev"}, binding=binding)
    assert "ReservedConcurrentExecutions" not in resumed["Resources"]["Function"]["Properties"]
    assert resumed["Resources"]["Mapping"]["Properties"]["Enabled"] is True
    assert resumed["Resources"]["Mapping"]["Properties"]["ScalingConfig"] == {
        "MaximumConcurrency": 2
    }
    assert c.QUARANTINE_KEY not in resumed["Metadata"]
    assert template == before


def test_failed_binding_repair_only_clears_disabled_mapping_scaling():
    before = {
        "Resources": {
            "Mapping": {
                "Type": "AWS::Lambda::EventSourceMapping",
                "Properties": {"Enabled": False, "ScalingConfig": {"MaximumConcurrency": 2}},
            }
        }
    }
    target = copy.deepcopy(before)
    target["Resources"]["Mapping"]["Properties"]["ScalingConfig"] = {}
    assert c._repair_delta(before, target) == {"Mapping"}
    before["Resources"]["OldDeployment"] = {
        "Type": "AWS::ApiGateway::Deployment",
        "Properties": {"StageName": "dev"},
    }
    target["Resources"]["NewDeployment"] = copy.deepcopy(before["Resources"]["OldDeployment"])
    before["Resources"]["UsagePlan"] = {
        "Type": "AWS::ApiGateway::UsagePlan",
        "DependsOn": "OldDeployment",
        "Properties": {"UsagePlanName": "unchanged"},
    }
    target["Resources"]["UsagePlan"] = {
        "Type": "AWS::ApiGateway::UsagePlan",
        "DependsOn": "NewDeployment",
        "Properties": {"UsagePlanName": "unchanged"},
    }
    assert c._repair_delta(before, target) == {"Mapping"}
    changed_deployment = copy.deepcopy(target)
    changed_deployment["Resources"]["NewDeployment"]["Properties"]["StageName"] = "other"
    with pytest.raises(FleetManagerError, match="only correct"):
        c._repair_delta(before, changed_deployment)
    for key, value in [("Enabled", True), ("BatchSize", 100)]:
        changed = copy.deepcopy(target)
        changed["Resources"]["Mapping"]["Properties"][key] = value
        with pytest.raises(FleetManagerError, match="only correct"):
            c._repair_delta(before, changed)
    before["Resources"]["Mapping"]["Properties"]["Enabled"] = True
    with pytest.raises(FleetManagerError, match="stay disabled"):
        c._repair_delta(before, target)


@pytest.mark.parametrize("failed_status", ["UPDATE_FAILED", "UPDATE_ROLLBACK_COMPLETE"])
def test_failed_binding_repair_rechecks_runtime_and_trigger_fences(monkeypatch, failed_status):
    import base64

    client, session = Mock(), Mock()
    session.client.return_value = client
    old = {
        "Resources": {
            "Mapping": {
                "Type": "AWS::Lambda::EventSourceMapping",
                "Properties": {"Enabled": False, "ScalingConfig": {"MaximumConcurrency": 2}},
            }
        }
    }
    target = copy.deepcopy(old)
    target["Resources"]["Mapping"]["Properties"]["ScalingConfig"] = {}
    client.describe_stacks.return_value = {
        "Stacks": [{"StackStatus": "UPDATE_FAILED", "ChangeSetId": "failed"}]
    }
    client.get_template.return_value = {"TemplateBody": old}
    client.get_paginator.return_value.paginate.return_value = [
        {
            "StackResourceSummaries": [
                {"LogicalResourceId": "Mapping", "ResourceStatus": failed_status}
            ]
        }
    ]
    client.get_function_configuration.return_value = {
        "FunctionArn": "fn",
        "State": "Active",
        "LastUpdateStatus": "Successful",
        "CodeSha256": base64.b64encode(bytes.fromhex("a" * 64)).decode(),
    }
    client.get_function_concurrency.return_value = {"ReservedConcurrentExecutions": 0}
    client.get_event_source_mapping.return_value = {
        "FunctionArn": "fn",
        "EventSourceArn": "queue",
        "State": "Disabled",
        "ScalingConfig": {"MaximumConcurrency": 2},
    }
    monkeypatch.setattr(
        c,
        "_state",
        Mock(return_value=(None, None, {"state": "binding_started", "frozen_at": 100}, None)),
    )
    monkeypatch.setattr(c, "verify_account", Mock())
    monkeypatch.setattr(c.q, "_cloud_time", Mock(return_value=2000))
    plan = {
        "mode": "fleet",
        "account_id": "111111111111",
        "stack_id": "stack",
        "runtime_sha256": "a" * 64,
        "drain_seconds": 960,
        "functions": {"Function": {"name": "fn", "arn": "fn", "freeze": True}},
        "mappings": {
            "mapping": {"UUID": "mapping", "FunctionArn": "fn", "EventSourceArn": "queue"}
        },
        "rules": {},
    }
    first = c._verify_failed_binding(session, plan, target, "failed")
    assert len(first) == 64
    client.get_function_configuration.return_value["MemorySize"] = 128
    assert c._verify_failed_binding(session, plan, target, "failed") != first
    client.get_function_concurrency.return_value["ReservedConcurrentExecutions"] = 2
    with pytest.raises(FleetManagerError, match="execution fence"):
        c._verify_failed_binding(session, plan, target, "failed")
    client.get_function_concurrency.return_value["ReservedConcurrentExecutions"] = 0
    client.get_event_source_mapping.return_value["State"] = "Enabled"
    with pytest.raises(FleetManagerError, match="escaped its fence"):
        c._verify_failed_binding(session, plan, target, "failed")


def test_stream_repair_keeps_original_and_adds_exact_disabled_mapping(binding):  # noqa: F811
    stream = binding["resources"]["Signals"]["stream_arn"]
    original = {
        "Type": "AWS::Lambda::EventSourceMapping",
        "DeletionPolicy": "Retain",
        "UpdateReplacePolicy": "Retain",
        "Properties": {"Enabled": False, "EventSourceArn": stream, "FunctionName": "function"},
    }
    before = {"Resources": {"Mapping": original}}
    target = copy.deepcopy(before)
    target["Resources"]["RecoveredMappingabc"] = copy.deepcopy(original)
    target["Resources"]["Mapping"]["Properties"]["EventSourceArn"] = {
        "Fn::GetAtt": ["Signals", "StreamArn"]
    }
    assert c._repair_delta(before, target, binding) == {"Mapping"}
    for field, value in [
        ("Enabled", True),
        ("EventSourceArn", "unreviewed-stream"),
        ("FunctionName", "other-function"),
    ]:
        changed = copy.deepcopy(target)
        changed["Resources"]["RecoveredMappingabc"]["Properties"][field] = value
        with pytest.raises(FleetManagerError, match="only correct"):
            c._repair_delta(before, changed, binding)


@pytest.fixture
def execution(monkeypatch):
    plan = {
        "stack_id": "stack",
        "release_sha256": "a" * 64,
        "runtime_sha256": "b" * 64,
        "tables": {},
        "account_id": "111111111111",
    }
    receipt = {"source": "restored"}
    before = {"Resources": {"before": {}}}
    target = {"Resources": {"after": {}}}
    change = {
        "schema": c.SCHEMA,
        "phase": "resume",
        "plan_sha256": sha256(plan),
        "receipt_sha256": sha256(receipt),
        "parameters": {},
        "change_set_arn": "reviewed-change",
        "previous_template_sha256": sha256(before),
    }
    cfn = Mock()
    cfn.describe_change_set.return_value = {
        "StackId": "stack",
        "Status": "CREATE_COMPLETE",
        "ExecutionStatus": "AVAILABLE",
    }
    cfn.get_template.side_effect = lambda **kw: {
        "TemplateBody": target if kw.get("ChangeSetName") else before
    }
    session = Mock()
    session.client.return_value = cfn
    monkeypatch.setattr(c, "_lease", Mock())
    monkeypatch.setattr(
        c, "_state", Mock(return_value=(None, None, {"state": "bound_frozen"}, None))
    )
    monkeypatch.setattr(
        c, "load_release", Mock(return_value=({"files": {"runtime.zip": "b" * 64}}, {}))
    )
    monkeypatch.setattr(c, "target_template", Mock(return_value=target))
    monkeypatch.setattr(c, "verify_receipt", Mock())
    monkeypatch.setattr(c, "_verify_target", Mock())
    monkeypatch.setattr(c, "_transition", Mock())
    return session, plan, receipt, change, cfn


def test_reviewed_resume_never_allows_automatic_rollback_to_remove_fence(execution):
    session, plan, receipt, change, cfn = execution
    assert (
        c.execute(session, plan, receipt, change, sha256(change), "release")["state"] == "started"
    )
    cfn.execute_change_set.assert_called_once_with(
        ChangeSetName="reviewed-change", DisableRollback=True
    )
    c._transition.assert_called_once_with(session, plan, "resume_started", "reviewed-change")


def test_changed_template_or_receipt_blocks_execution(execution):
    session, plan, receipt, change, cfn = execution
    with pytest.raises(FleetManagerError, match="digest mismatch"):
        c.execute(session, plan, {"source": "other"}, change, sha256(change), "release")
    cfn.execute_change_set.assert_not_called()
    cfn.get_template.return_value = {"TemplateBody": {}}
    cfn.get_template.side_effect = None
    with pytest.raises(FleetManagerError, match="no longer matches"):
        c.execute(session, plan, receipt, change, sha256(change), "release")
    cfn.execute_change_set.assert_not_called()


def test_lost_execution_response_observes_same_change_without_reexecuting(execution):
    session, plan, receipt, change, cfn = execution
    cfn.describe_change_set.return_value["ExecutionStatus"] = "EXECUTE_IN_PROGRESS"
    cfn.describe_stacks.return_value = {"Stacks": [{"ChangeSetId": "reviewed-change"}]}
    assert (
        c.execute(session, plan, receipt, change, sha256(change), "release")["state"] == "started"
    )
    cfn.execute_change_set.assert_not_called()
    cfn.describe_stacks.return_value = {"Stacks": [{"ChangeSetId": "another-change"}]}
    with pytest.raises(FleetManagerError, match="superseded"):
        c.execute(session, plan, receipt, change, sha256(change), "release")


def test_resumed_recovery_cannot_be_reexecuted(execution):
    session, plan, receipt, change, cfn = execution
    c._state.return_value = (None, None, {"state": "bound_resumed"}, None)
    with pytest.raises(FleetManagerError, match="no longer frozen"):
        c.execute(session, plan, receipt, change, sha256(change), "release")
    cfn.execute_change_set.assert_not_called()


def test_lost_completion_releases_only_its_own_lease(execution, monkeypatch):
    session, plan, receipt, change, client = execution
    plan["artifact_bucket"] = "artifacts"
    change["plan_sha256"] = sha256(plan)
    recorded = {
        "state": "bound_resumed",
        "change_set_arn": change["change_set_arn"],
        "receipt_sha256": sha256(receipt),
        "completed_at": "original-time",
    }
    get = Mock(
        side_effect=[(recorded, "receipt-etag"), ({"plan_sha256": sha256(plan)}, "lock-etag")]
    )
    monkeypatch.setattr(c.q, "_get", get)
    assert c.complete(session, plan, receipt, change, sha256(change), "release") == recorded
    assert client.put_object.call_args.kwargs["IfMatch"] == "lock-etag"
    c._verify_target.assert_not_called()
    client.reset_mock()
    get.side_effect = [(recorded, "receipt-etag"), ({"plan_sha256": "new-owner"}, "new-etag")]
    assert c.complete(session, plan, receipt, change, sha256(change), "release") == recorded
    client.put_object.assert_not_called()


def test_completion_checks_retained_mapping_identity_before_enabling(execution, monkeypatch):
    session, plan, receipt, change, client = execution
    plan.update(
        artifact_bucket="artifacts",
        mode="fleet",
        mappings={
            "old": {
                "UUID": "old",
                "FunctionArn": "reviewed-function",
                "EventSourceArn": "reviewed-stream",
                "State": "Enabled",
            }
        },
    )
    receipt["binding"] = {}
    change.update(plan_sha256=sha256(plan), receipt_sha256=sha256(receipt))
    monkeypatch.setattr(c.q, "_get", Mock(return_value=(None, None)))
    monkeypatch.setattr(c, "verify_physical", Mock())
    c._verify_target.return_value = {"ChangeSetId": "reviewed-change"}
    client.get_event_source_mapping.return_value = {
        "FunctionArn": "other-function",
        "EventSourceArn": "reviewed-stream",
        "State": "Disabled",
    }
    with pytest.raises(FleetManagerError, match="identity changed"):
        c.complete(session, plan, receipt, change, sha256(change), "release")
    client.update_event_source_mapping.assert_not_called()
    client.get_event_source_mapping.return_value["FunctionArn"] = "reviewed-function"
    assert (
        c.complete(session, plan, receipt, change, sha256(change), "release")["state"]
        == "resuming_retained_streams"
    )
    client.update_event_source_mapping.assert_called_once_with(UUID="old", Enabled=True)
