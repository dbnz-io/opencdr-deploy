import json
import zipfile

import pytest

from opencdr_deploy.artifacts import load_release
from opencdr_deploy.fleet_api import FleetManagerError
from opencdr_deploy.release_builder import export_release


def test_export_preserves_logical_ids_and_parameterizes_aws_identity(tmp_path):
    source = tmp_path / "compiled.json"
    source.write_text(
        json.dumps(
            {
                "Resources": {
                    "ExecutionRole": {
                        "Type": "AWS::IAM::Role",
                        "Properties": {"RoleName": "opencdr-dev-operator-us-east-1-lambdaRole"},
                    },
                    "ApiGatewayRestApi": {
                        "Type": "AWS::ApiGateway::RestApi",
                        "Properties": {"Policy": "", "Name": "dev-opencdr"},
                    },
                    "ArchiveGlueDatabase": {
                        "Type": "AWS::Glue::Database",
                        "Properties": {"DatabaseInput": {"Name": "opencdr_dev_archive"}},
                    },
                    "ApiLambdaFunction": {
                        "Type": "AWS::Lambda::Function",
                        "Properties": {
                            "FunctionName": "opencdr-dev-api",
                            "Code": {"S3Bucket": "build", "S3Key": "runtime.zip"},
                            "Environment": {
                                "Variables": {
                                    "STAGE": "dev",
                                    "AWS_ACCOUNT": "111111111111",
                                    "OPENCDR_LEGACY_API_ENABLED": "false",
                                    "OPENCDR_CORE_VERSION": "0.3.0-dev",
                                    "DETECTION_FEED_RETENTION_DAYS": "7",
                                    "DETECTION_ACTIVATION_REQUIRED": "true",
                                    "DETECTION_RUNTIME_ID": "unexported",
                                }
                            },
                            "Role": "arn:aws:iam::111111111111:role/opencdr-dev-api",
                        },
                    },
                    "ApiGatewayDeployment": {
                        "Type": "AWS::ApiGateway::Deployment",
                        "Properties": {"StageName": "dev"},
                    },
                },
                "Outputs": {
                    "Endpoint": {"Value": "https://api.execute-api.us-east-1.amazonaws.com/dev"}
                },
            }
        )
    )
    runtime = tmp_path / "runtime.zip"
    with zipfile.ZipFile(runtime, "w") as archive:
        archive.writestr("handler.py", "def main(event, context): return {}")
    result = export_release(
        source,
        runtime,
        tmp_path / "release",
        product="opencdr",
        version="1.4.0",
        stage="dev",
        region="us-east-1",
        account_id="111111111111",
    )
    manifest, template = load_release(result["directory"], result["sha256"])
    function = template["Resources"]["ApiLambdaFunction"]["Properties"]
    assert function["FunctionName"] == {"Fn::Sub": "opencdr-${Stage}-api"}
    assert function["Code"]["S3Bucket"] == {"Ref": "ArtifactBucket"}
    assert function["Environment"]["Variables"]["STAGE"] == {"Ref": "Stage"}
    assert template["Outputs"]["Endpoint"]["Value"] == {
        "Fn::Sub": "https://api.execute-api.${AWS::Region}.${AWS::URLSuffix}/${Stage}"
    }
    assert manifest["management_contract_major"] == 2
    assert (
        function["Environment"]["Variables"]["DETECTION_RUNTIME_ID"]
        == manifest["files"]["runtime.zip"]
    )
    assert function["Environment"]["Variables"]["DETECTION_FEED_RETENTION_DAYS"] == {
        "Ref": "DetectionReplayDays"
    }
    assert template["Parameters"]["DetectionReplayDays"] == {
        "Type": "Number",
        "Default": 7,
        "MinValue": 7,
        "MaxValue": 30,
    }
    assert function["Environment"]["Variables"]["OPENCDR_CORE_VERSION"] == "0.3.0-dev"
    assert template["Resources"]["ArchiveGlueDatabase"]["Properties"]["DatabaseInput"]["Name"] == {
        "Fn::Sub": "opencdr_${Stage}_archive"
    }
    assert template["Resources"]["ApiGatewayRestApi"]["Properties"]["Name"] == {
        "Fn::Sub": "${Stage}-opencdr"
    }
    role = template["Resources"]["ExecutionRole"]["Properties"]["RoleName"]
    assert role["Fn::If"][0] == "PreserveRoleNames"
    assert role["Fn::If"][2] == {"Ref": "AWS::NoValue"}
    assert template["Parameters"]["PreserveRoleNames"]["Default"] == "false"
    (tmp_path / "release" / "runtime.zip").write_bytes(b"tampered")
    with pytest.raises(FleetManagerError, match="digest mismatch"):
        load_release(result["directory"], result["sha256"])


def test_unknown_manifest_is_rejected_before_aws_calls(tmp_path):
    (tmp_path / "release.json").write_text("{}")
    with pytest.raises(FleetManagerError, match="digest mismatch"):
        load_release(tmp_path, "0" * 64)


def test_fleet_export_keeps_schedule_and_runtime_interval_bound_together(tmp_path):
    source = tmp_path / "compiled.json"
    source.write_text(
        json.dumps(
            {
                "Resources": {
                    "PollLambdaFunction": {
                        "Type": "AWS::Lambda::Function",
                        "Properties": {
                            "Code": {"S3Bucket": "build", "S3Key": "runtime.zip"},
                            "Environment": {
                                "Variables": {
                                    "POLL_INTERVAL_MINUTES": "5",
                                    "FULL_REFRESH_MINUTES": "120",
                                    "DETECTION_POLL_INTERVAL_MINUTES": "2",
                                    "DETECTION_RETENTION_DAYS": "90",
                                    "DETECTION_POLLING_ENABLED": "true",
                                }
                            },
                        },
                    },
                    "DetectionScheduleEventsRuleSchedule1": {
                        "Type": "AWS::Events::Rule",
                        "Properties": {"ScheduleExpression": "rate(2 minutes)", "State": "ENABLED"},
                    },
                    "DetectionBucket": {
                        "Type": "AWS::S3::Bucket",
                        "Properties": {
                            "LifecycleConfiguration": {"Rules": [{"ExpirationInDays": 90}]}
                        },
                    },
                    "DetectionMapping": {
                        "Type": "AWS::Lambda::EventSourceMapping",
                        "Properties": {
                            "FunctionName": {"Fn::GetAtt": ["DetectionPollLambdaFunction", "Arn"]},
                            "ScalingConfig": {"MaximumConcurrency": 2},
                        },
                    },
                    "ScheduleEventsRuleSchedule1": {
                        "Type": "AWS::Events::Rule",
                        "Properties": {"ScheduleExpression": "rate(5 minutes)"},
                    },
                }
            }
        )
    )
    runtime = tmp_path / "runtime.zip"
    with zipfile.ZipFile(runtime, "w") as archive:
        archive.writestr("handler.py", "pass")
    result = export_release(
        source,
        runtime,
        tmp_path / "release",
        product="opencdr-fleet-manager",
        version="0.2.0",
        stage="prod",
        region="us-east-1",
        account_id="111111111111",
    )
    _, template = load_release(result["directory"], result["sha256"])
    from opencdr_deploy.fleet_recovery import pinned_bucket_name

    assert pinned_bucket_name(template) is None
    assert (
        pinned_bucket_name(template, {"DetectionEvidenceBucketName": "retained-evidence"})
        == "retained-evidence"
    )
    env = template["Resources"]["PollLambdaFunction"]["Properties"]["Environment"]["Variables"]
    assert env["POLL_INTERVAL_MINUTES"] == {"Ref": "PollIntervalMinutes"}
    assert env["FULL_REFRESH_MINUTES"] == {"Ref": "FullRefreshMinutes"}
    assert template["Resources"]["ScheduleEventsRuleSchedule1"]["Properties"][
        "ScheduleExpression"
    ] == {
        "Fn::If": [
            "PollEveryMinute",
            "rate(1 minute)",
            {"Fn::Sub": "rate(${PollIntervalMinutes} minutes)"},
        ]
    }

    assert env["DETECTION_POLL_INTERVAL_MINUTES"] == {"Ref": "DetectionPollIntervalMinutes"}
    assert env["DETECTION_RETENTION_DAYS"] == {"Ref": "DetectionRetentionDays"}
    schedule = template["Resources"]["DetectionScheduleEventsRuleSchedule1"]["Properties"]
    assert schedule["ScheduleExpression"]["Fn::If"][2] == {
        "Fn::Sub": "rate(${DetectionPollIntervalMinutes} minutes)"
    }
    assert schedule["State"]["Fn::If"] == ["DetectionPollingEnabled", "ENABLED", "DISABLED"]
    assert template["Resources"]["DetectionBucket"]["Properties"]["LifecycleConfiguration"][
        "Rules"
    ][0]["ExpirationInDays"] == {"Ref": "DetectionRetentionDays"}
    assert template["Resources"]["DetectionMapping"]["Properties"]["ScalingConfig"][
        "MaximumConcurrency"
    ] == {"Ref": "DetectionPollConcurrency"}
