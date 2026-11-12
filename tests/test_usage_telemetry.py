import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opencdr_deploy import usage_telemetry as module
from opencdr_deploy.cli import build_parser
from opencdr_deploy.fleet_api import FleetManagerError

ACCOUNT = "123456789012"
REGION = "us-east-1"
RUN = "abcdef012345"


def selection():
    return {
        "stack_id": f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/opencdr-test/id",
        "tables": [f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/retained"],
        "bucket_prefixes": ["arn:aws:s3:::retained-evidence/"],
        "queues": [f"arn:aws:sqs:{REGION}:{ACCOUNT}:retained"],
    }


def test_only_observer_resources_and_exact_data_selectors():
    doc = module.template(ACCOUNT, REGION, RUN, selection())
    assert {x["Type"] for x in doc["Resources"].values()} == {
        "AWS::S3::Bucket",
        "AWS::S3::BucketPolicy",
        "AWS::CloudTrail::Trail",
    }
    trail = doc["Resources"]["RequestTrail"]["Properties"]
    assert trail["IsLogging"] and not trail["IsMultiRegionTrail"]
    assert len(trail["AdvancedEventSelectors"]) == 3
    assert all(
        x["FieldSelectors"][0] == {"Field": "eventCategory", "Equals": ["Data"]}
        for x in trail["AdvancedEventSelectors"]
    )
    assert doc["Resources"]["AuditBucket"]["DeletionPolicy"] == "Retain"
    allow = [
        x
        for x in doc["Resources"]["AuditBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]
        if x["Effect"] == "Allow"
    ]
    assert all(x["Principal"] == {"Service": "cloudtrail.amazonaws.com"} for x in allow)
    assert all(
        x["Condition"]["StringEquals"]["aws:SourceArn"].endswith("trail/opencdr-usage-" + RUN)
        for x in allow
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("tables", "arn:aws:dynamodb:us-east-1:999999999999:table/foreign"),
        ("queues", "arn:aws:sqs:us-west-2:123456789012:foreign"),
        ("queues", "arn:aws:sqs:us-east-1:123456789012:*"),
        ("bucket_prefixes", "arn:aws:s3:::bucket/*"),
        ("tables", "arn:aws:dynamodb:us-east-1:123456789012:table/a/*"),
    ],
)
def test_foreign_or_wildcard_scope_fails(key, value):
    scope = selection()
    scope[key] = [value]
    with pytest.raises(FleetManagerError, match="escapes"):
        module.template(ACCOUNT, REGION, RUN, scope)


def test_stop_template_changes_only_logging():
    enabled = module.template(ACCOUNT, REGION, RUN, selection())
    stopped = module.template(ACCOUNT, REGION, RUN, selection(), logging=False)
    enabled["Resources"]["RequestTrail"]["Properties"]["IsLogging"] = False
    assert enabled == stopped


def test_cli_exposes_required_exact_identity(tmp_path):
    args = build_parser().parse_args(
        [
            "usage-telemetry",
            "plan",
            "--profile",
            "test",
            "--account-id",
            ACCOUNT,
            "--region",
            REGION,
            "--stack",
            "opencdr-test",
            "--run-id",
            RUN,
            "--output",
            str(tmp_path / "plan.json"),
        ]
    )
    assert args.command == "usage-telemetry" and args.run_id == RUN


def test_plan_persists_intent_before_uncertain_cloud_result(tmp_path, monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(module.boto3, "Session", lambda **kw: object())
    monkeypatch.setattr(module, "verify_account", lambda *a: None)
    monkeypatch.setattr(module, "scope", lambda *a: selection())
    monkeypatch.setattr(module, "aws_client", lambda *a: client)
    args = SimpleNamespace(
        action="plan",
        profile="test",
        account_id=ACCOUNT,
        region=REGION,
        stack="opencdr-test",
        run_id=RUN,
        output=tmp_path / "plan.json",
        original_plan=None,
        original_sha256=None,
    )

    def fail(**kwargs):
        intent = json.loads(args.output.with_suffix(".intent.json").read_text())
        assert intent["request_id"] == kwargs["ClientToken"]
        assert intent["change_set_name"] == kwargs["ChangeSetName"]
        assert kwargs["ChangeSetName"].startswith("opencdr-")
        raise OSError("response lost")

    client.create_change_set.side_effect = fail
    with pytest.raises(OSError, match="response lost"):
        module.run(args)
    assert not args.output.exists()
    assert client.execute_change_set.call_count == 0


def test_stop_requires_reviewed_bytes_and_can_stop_after_source_upgrades(tmp_path, monkeypatch):
    original = {
        "account_id": ACCOUNT,
        "region": REGION,
        "run_id": RUN,
        "source": selection(),
        "template": module.template(ACCOUNT, REGION, RUN, selection()),
    }
    path = tmp_path / "original.json"
    path.write_text(json.dumps(original))
    client = MagicMock()
    client.create_change_set.return_value = {"Id": "arn:review", "StackId": "arn:stack"}
    client.describe_stacks.return_value = {
        "Stacks": [{"StackStatus": "CREATE_COMPLETE", "ChangeSetId": "arn:original"}]
    }
    monkeypatch.setattr(module.boto3, "Session", lambda **kw: object())
    monkeypatch.setattr(module, "verify_account", lambda *a: None)
    monkeypatch.setattr(
        module,
        "scope",
        lambda *a: pytest.fail("Stopping must not depend on live source availability"),
    )
    monkeypatch.setattr(module, "aws_client", lambda *a: client)
    monkeypatch.setattr(module, "template_body", lambda *a, **kw: deepcopy(original["template"]))
    args = SimpleNamespace(
        action="stop-plan",
        profile="test",
        account_id=ACCOUNT,
        region=REGION,
        stack="opencdr-test",
        run_id=RUN,
        output=tmp_path / "stop.json",
        original_plan=path,
        original_sha256="0" * 64,
    )
    with pytest.raises(FleetManagerError, match="SHA256 mismatch"):
        module.run(args)
    assert not client.create_change_set.called
    args.original_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    assert module.run(args) == 0
    sent = json.loads(client.create_change_set.call_args.kwargs["TemplateBody"])
    assert not sent["Resources"]["RequestTrail"]["Properties"]["IsLogging"]
    assert not client.execute_change_set.called


def test_restart_requires_stopped_exact_template_and_binds_predecessor(tmp_path, monkeypatch):
    original = {
        "account_id": ACCOUNT,
        "region": REGION,
        "run_id": RUN,
        "source": selection(),
        "template": module.template(ACCOUNT, REGION, RUN, selection()),
    }
    path = tmp_path / "original.json"
    path.write_text(json.dumps(original))
    client = MagicMock()
    client.create_change_set.return_value = {"Id": "arn:new", "StackId": "arn:stack"}
    client.describe_stacks.return_value = {
        "Stacks": [{"StackStatus": "UPDATE_COMPLETE", "ChangeSetId": "arn:stopped"}]
    }
    monkeypatch.setattr(module.boto3, "Session", lambda **kw: object())
    monkeypatch.setattr(module, "verify_account", lambda *a: None)
    monkeypatch.setattr(module, "aws_client", lambda *a: client)
    monkeypatch.setattr(module, "scope", lambda *a: selection())
    live = deepcopy(original["template"])
    monkeypatch.setattr(module, "template_body", lambda *a, **kw: deepcopy(live))
    args = SimpleNamespace(
        action="restart-plan",
        profile="test",
        account_id=ACCOUNT,
        region=REGION,
        stack="opencdr-test",
        run_id=RUN,
        output=tmp_path / "restart.json",
        original_plan=path,
        original_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    with pytest.raises(FleetManagerError, match="differs"):
        module.run(args)
    assert not client.create_change_set.called
    live["Resources"]["RequestTrail"]["Properties"]["IsLogging"] = False
    module.run(args)
    assert json.loads(client.create_change_set.call_args.kwargs["TemplateBody"])["Resources"][
        "RequestTrail"
    ]["Properties"]["IsLogging"]
    assert (
        json.loads(args.output.with_suffix(".intent.json").read_text())["predecessor"]
        == "arn:stopped"
    )
