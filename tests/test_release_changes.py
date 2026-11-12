import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from opencdr_deploy import artifacts
from opencdr_deploy.cli import effective_parameters
from opencdr_deploy.fleet_api import FleetManagerError


@pytest.fixture
def plan(tmp_path, monkeypatch):
    for name in ("runtime.zip", "template.json", "release.json"):
        (tmp_path / name).write_bytes(b"reviewed fixture")
    keys = [
        "Stage",
        "ArtifactBucket",
        "ArtifactKey",
        "LegacyApiEnabled",
        "ManagementPrincipalArns",
        "PreserveRoleNames",
    ]
    template = {"Parameters": {key: {} for key in keys}, "Resources": {}}
    monkeypatch.setattr(
        artifacts,
        "load_release",
        lambda *a: (
            {"product": "opencdr", "version": "1.4.0"},
            template,
        ),
    )
    cfn, s3, sts, session = Mock(), Mock(), Mock(), Mock()
    session.client.side_effect = lambda service, **kwargs: {
        "cloudformation": cfn,
        "s3": s3,
        "sts": sts,
    }[service]
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    cfn.describe_stacks.return_value = {
        "Stacks": [
            {
                "Parameters": [
                    {"ParameterKey": "LegacyApiEnabled", "ParameterValue": "true"},
                    {"ParameterKey": "ManagementPrincipalArns", "ParameterValue": "existing-trust"},
                    {"ParameterKey": "PreserveRoleNames", "ParameterValue": "false"},
                ]
            }
        ]
    }
    cfn.get_template.return_value = {"TemplateBody": {"Resources": {}}}
    cfn.create_change_set.return_value = {"Id": "reviewed-change-set"}
    cfn.describe_change_set.side_effect = [
        {"Changes": [{"first": True}], "NextToken": "next"},
        {"Changes": [{"second": True}]},
    ]
    args = SimpleNamespace(
        directory=tmp_path,
        sha256="a" * 64,
        stage="prod",
        profile="test",
        account_id="123456789012",
        region="us-east-1",
        artifact_bucket="owned-bucket",
        principal_arn=None,
        legacy_api=None,
    )
    return args, session, cfn, s3, template


def test_upgrade_preserves_trust_and_auth_and_returns_every_review_page(plan):
    args, session, cfn, s3, _ = plan
    result = artifacts.prepare_change(args, session)
    assert result["changes"] == [{"first": True}, {"second": True}]
    parameters = {
        p["ParameterKey"]: p["ParameterValue"]
        for p in cfn.create_change_set.call_args.kwargs["Parameters"]
    }
    assert parameters["LegacyApiEnabled"] == "true"
    assert parameters["ManagementPrincipalArns"] == "existing-trust"
    assert parameters["PreserveRoleNames"] == "false"
    cfn.execute_change_set.assert_not_called()
    s3.head_bucket.assert_called_once_with(
        Bucket="owned-bucket", ExpectedBucketOwner="123456789012"
    )


def test_invalid_principal_never_contacts_aws(plan):
    args, session, *_ = plan
    args.principal_arn = ["arn:aws:iam::123456789012:root"]
    with pytest.raises(FleetManagerError, match="exact IAM role"):
        artifacts.prepare_change(args, session)
    session.client.assert_not_called()


@pytest.mark.parametrize("days", [0, 6, 31, True, "7"])
def test_invalid_detection_replay_never_contacts_aws(plan, days):
    args, session, _, _, template = plan
    template["Parameters"]["DetectionReplayDays"] = {"Default": 7}
    args.detection_replay_days = days
    with pytest.raises(FleetManagerError, match="replay"):
        artifacts.prepare_change(args, session)
    session.client.assert_not_called()


def test_detection_replay_is_explicit_and_survives_upgrade(plan):
    args, session, cfn, _, template = plan
    template["Parameters"]["DetectionReplayDays"] = {"Default": 7}
    args.detection_replay_days = 14
    artifacts.prepare_change(args, session)
    parameters = {
        p["ParameterKey"]: p["ParameterValue"]
        for p in cfn.create_change_set.call_args.kwargs["Parameters"]
    }
    assert parameters["DetectionReplayDays"] == "14"


def test_omitted_detection_replay_preserves_existing_value(plan):
    args, session, cfn, _, template = plan
    template["Parameters"]["DetectionReplayDays"] = {"Default": 7}
    cfn.describe_stacks.return_value["Stacks"][0]["Parameters"].append(
        {"ParameterKey": "DetectionReplayDays", "ParameterValue": "21"}
    )
    artifacts.prepare_change(args, session)
    parameters = {
        p["ParameterKey"]: p["ParameterValue"]
        for p in cfn.create_change_set.call_args.kwargs["Parameters"]
    }
    assert parameters["DetectionReplayDays"] == "21"


def test_retention_downgrade_fails_before_artifact_upload(plan):
    args, session, cfn, s3, template = plan
    protected = {
        "Type": "AWS::DynamoDB::Table",
        "DeletionPolicy": "Retain",
        "UpdateReplacePolicy": "Retain",
    }
    cfn.get_template.return_value = {"TemplateBody": {"Resources": {"Data": protected}}}
    template["Resources"]["Data"] = {**protected, "UpdateReplacePolicy": "Delete"}
    with pytest.raises(FleetManagerError, match="removes protection"):
        artifacts.prepare_change(args, session)
    s3.put_object.assert_not_called()
    cfn.create_change_set.assert_not_called()


@pytest.mark.parametrize("broken", [None, "old_release", "wrong_binding", "import_only"])
def test_fleet_release_preserves_recovered_bucket_or_fails_before_upload(plan, monkeypatch, broken):
    args, session, cfn, s3, template = plan
    monkeypatch.setattr(
        artifacts,
        "load_release",
        lambda *a: ({"product": "opencdr-fleet-manager", "version": "0.2.0"}, template),
    )
    bucket = "retained-fleet-evidence"
    live = {"Resources": {"DetectionBucket": {"Properties": {"BucketName": bucket}}}}
    from opencdr_deploy.fleet_recovery import IMPORT_DESCRIPTION

    if broken == "import_only":
        live["Description"] = IMPORT_DESCRIPTION
    cfn.get_template.return_value = {"TemplateBody": live}
    if broken != "old_release":
        template["Parameters"]["DetectionEvidenceBucketName"] = {"Default": ""}
        template["Resources"]["DetectionBucket"] = {
            "Properties": {
                "BucketName": "other-evidence"
                if broken == "wrong_binding"
                else {"Ref": "DetectionEvidenceBucketName"}
            }
        }
    if broken:
        with pytest.raises(FleetManagerError):
            artifacts.prepare_change(args, session)
        s3.put_object.assert_not_called()
        cfn.create_change_set.assert_not_called()
    else:
        artifacts.prepare_change(args, session)
        parameters = {
            p["ParameterKey"]: p["ParameterValue"]
            for p in cfn.create_change_set.call_args.kwargs["Parameters"]
        }
        assert parameters["DetectionEvidenceBucketName"] == bucket
        cfn.execute_change_set.assert_not_called()


def test_existing_legacy_installs_keep_access_and_new_iam_installs_disable_it():
    assert effective_parameters({})["legacyApiEnabled"] == "true"
    assert effective_parameters({"auth_mode": "iam"})["legacyApiEnabled"] == "false"
    assert (
        effective_parameters(
            {
                "auth_mode": "iam",
                "parameters": {"legacyApiEnabled": "true"},
            }
        )["legacyApiEnabled"]
        == "true"
    )


def test_management_script_uses_installed_cli_environment(tmp_path):
    from opencdr_deploy import cli

    core_python = tmp_path / ".venv" / "bin" / "python"
    core_python.parent.mkdir(parents=True)
    core_python.touch()
    script = tmp_path / "scripts" / "opencdr.py"
    assert cli.script_command(tmp_path, script) == [cli.sys.executable, str(script)]
    build_script = tmp_path / "scripts" / "build.py"
    assert cli.script_command(tmp_path, build_script) == [str(core_python), str(build_script)]


def test_upgrade_allows_retiring_a_published_lambda_version(plan):
    args, session, cfn, s3, _ = plan
    cfn.get_template.return_value = {
        "TemplateBody": {
            "Resources": {
                "OldRuntimeVersion": {"Type": "AWS::Lambda::Version", "DeletionPolicy": "Retain"}
            }
        }
    }
    result = artifacts.prepare_change(args, session)
    assert result["executed"] is False
    assert s3.put_object.call_count == 4


def test_bootstrap_reviews_private_retained_bucket_without_execution(plan):
    args, session, cfn, *_ = plan
    result = artifacts.prepare_bootstrap(args, session)
    call = cfn.create_change_set.call_args.kwargs
    template = json.loads(call["TemplateBody"])
    bucket = template["Resources"]["ArtifactBucket"]
    assert bucket["DeletionPolicy"] == bucket["UpdateReplacePolicy"] == "Retain"
    assert all(bucket["Properties"]["PublicAccessBlockConfiguration"].values())
    assert bucket["Properties"]["VersioningConfiguration"]["Status"] == "Enabled"
    assert bucket["Properties"]["BucketEncryption"]
    assert result["artifact_bucket"] == "opencdr-artifacts-123456789012-us-east-1"
    assert result["executed"] is False
    assert call["ChangeSetType"] == "CREATE"
    cfn.execute_change_set.assert_not_called()


def test_bootstrap_wrong_account_fails_before_creating_resources(plan):
    args, session, cfn, *_ = plan
    args.account_id = "999999999999"
    with pytest.raises(FleetManagerError, match="account"):
        artifacts.prepare_bootstrap(args, session)
    cfn.create_change_set.assert_not_called()


def test_poll_override_rejects_core_before_contacting_aws(plan):
    args, session, *_ = plan
    args.poll_interval_minutes = 5
    with pytest.raises(FleetManagerError, match="compatible Fleet"):
        artifacts.prepare_change(args, session)
    session.client.assert_not_called()


def test_fleet_poll_overrides_preserve_unspecified_values_and_validate_combination(
    plan, monkeypatch
):
    args, session, cfn, s3, template = plan
    template["Parameters"].update(
        {"PollIntervalMinutes": {"Default": 1}, "FullRefreshMinutes": {"Default": 60}}
    )
    monkeypatch.setattr(
        artifacts,
        "load_release",
        lambda *a: ({"product": "opencdr-fleet-manager", "version": "0.2.0"}, template),
    )
    cfn.describe_stacks.return_value["Stacks"][0]["Parameters"].extend(
        [
            {"ParameterKey": "PollIntervalMinutes", "ParameterValue": "5"},
            {"ParameterKey": "FullRefreshMinutes", "ParameterValue": "120"},
        ]
    )
    args.poll_interval_minutes = 10
    artifacts.prepare_change(args, session)
    parameters = {
        p["ParameterKey"]: p["ParameterValue"]
        for p in cfn.create_change_set.call_args.kwargs["Parameters"]
    }
    assert parameters["PollIntervalMinutes"] == "10"
    assert parameters["FullRefreshMinutes"] == "120"
    s3.reset_mock()
    args.full_refresh_minutes = 2
    with pytest.raises(FleetManagerError, match="at least the poll"):
        artifacts.prepare_change(args, session)
    s3.put_object.assert_not_called()


def test_detection_polling_options_preserve_omitted_values(plan, monkeypatch):
    args, session, cfn, s3, template = plan
    template["Parameters"].update(
        {
            "DetectionPollIntervalMinutes": {"Default": 1},
            "DetectionRetentionDays": {"Default": 90},
            "DetectionPollConcurrency": {"Default": 2},
            "DetectionPollingEnabled": {"Default": "true"},
        }
    )
    monkeypatch.setattr(
        artifacts,
        "load_release",
        lambda *a: ({"product": "opencdr-fleet-manager", "version": "0.2.0"}, template),
    )
    cfn.describe_stacks.return_value["Stacks"][0]["Parameters"].extend(
        [
            {"ParameterKey": "DetectionPollIntervalMinutes", "ParameterValue": "5"},
            {"ParameterKey": "DetectionRetentionDays", "ParameterValue": "180"},
        ]
    )
    args.detection_polling_enabled = False
    artifacts.prepare_change(args, session)
    values = {
        p["ParameterKey"]: p["ParameterValue"]
        for p in cfn.create_change_set.call_args.kwargs["Parameters"]
    }
    assert values["DetectionPollIntervalMinutes"] == "5"
    assert values["DetectionRetentionDays"] == "180"
    assert values["DetectionPollingEnabled"] == "false"
    s3.reset_mock()
    args.detection_retention_days = 1
    with pytest.raises(FleetManagerError, match="compatible Fleet"):
        artifacts.prepare_change(args, session)
    s3.put_object.assert_not_called()
