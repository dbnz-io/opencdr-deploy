import copy

import pytest

from opencdr_deploy.fleet_api import FleetManagerError
from opencdr_deploy.storage_bindings import METADATA_KEY, apply_binding, current_binding, validate


@pytest.fixture
def binding():
    return {
        "schema": "opencdr.storage-binding/1",
        "account_id": "111111111111",
        "region": "us-east-1",
        "stage": "dev",
        "snapshot_sha256": "a" * 64,
        "resources": {
            "Signals": {
                "type": "AWS::DynamoDB::Table",
                "source_name": "opencdr-dev-signals",
                "source_id": "11111111-1111-1111-1111-111111111111",
                "name": "opencdr-dev-signals-recovered",
                "id": "22222222-2222-2222-2222-222222222222",
                "stream_arn": "arn:aws:dynamodb:us-east-1:111111111111:table/"
                "opencdr-dev-signals-recovered/stream/2026-11-10T00:00:00.000",
            },
            "CaseBucket": {
                "type": "AWS::S3::Bucket",
                "source_name": "original-cases",
                "source_id": "original-cases",
                "name": "restored-cases",
                "id": "restored-cases",
                "stream_arn": None,
            },
        },
    }


@pytest.fixture
def template():
    return {
        "Parameters": {"Stage": {"Type": "String", "Default": "dev"}},
        "Resources": {
            "Signals": {
                "Type": "AWS::DynamoDB::Table",
                "Properties": {"TableName": {"Fn::Sub": "opencdr-${Stage}-signals"}},
            },
            "CaseBucket": {
                "Type": "AWS::S3::Bucket",
                "Properties": {"BucketName": "original-cases"},
            },
            "CaseBucketPolicy": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {"Bucket": {"Ref": "CaseBucket"}},
            },
            "Function": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Environment": {
                        "Variables": {
                            "TABLE": {"Fn::Sub": "opencdr-${Stage}-signals"},
                            "BUCKET": {"Ref": "CaseBucket"},
                        }
                    }
                },
            },
            "Role": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "Policies": [
                        {
                            "PolicyName": "Data",
                            "PolicyDocument": {
                                "Statement": [
                                    {
                                        "Resource": [
                                            {
                                                "Fn::Sub": (
                                                    "arn:${AWS::Partition}:dynamodb:"
                                                    "${AWS::Region}:${AWS::AccountId}:table/"
                                                    "opencdr-${Stage}-signals/index/*"
                                                )
                                            },
                                            {"Fn::Sub": "${CaseBucket.Arn}/*"},
                                            {"Fn::GetAtt": ["Signals", "Arn"]},
                                        ]
                                    }
                                ]
                            },
                        }
                    ]
                },
            },
            "Mapping": {
                "Type": "AWS::Lambda::EventSourceMapping",
                "Properties": {
                    "EventSourceArn": {"Fn::GetAtt": ["Signals", "StreamArn"]},
                    "Enabled": False,
                },
            },
        },
        "Outputs": {"Table": {"Value": {"Ref": "Signals"}}},
    }


def test_binding_moves_runtime_iam_stream_and_output_together_but_retains_sources(
    template, binding
):
    original = copy.deepcopy(template)
    value = apply_binding(template, binding)
    assert template == original
    resources = value["Resources"]
    assert resources["Signals"]["Properties"] == original["Resources"]["Signals"]["Properties"]
    assert resources["Signals"]["DeletionPolicy"] == "Retain"
    assert resources["CaseBucket"]["UpdateReplacePolicy"] == "Retain"
    assert resources["CaseBucketPolicy"] == original["Resources"]["CaseBucketPolicy"]
    env = resources["Function"]["Properties"]["Environment"]["Variables"]
    assert env == {
        "TABLE": {"Fn::Sub": "opencdr-dev-signals-recovered"},
        "BUCKET": "restored-cases",
    }
    permissions = resources["Role"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"][0]
    assert permissions["Resource"] == [
        {
            "Fn::Sub": "arn:aws:dynamodb:us-east-1:111111111111:table/"
            "opencdr-dev-signals-recovered/index/*"
        },
        {"Fn::Sub": "arn:aws:s3:::restored-cases/*"},
        "arn:aws:dynamodb:us-east-1:111111111111:table/opencdr-dev-signals-recovered",
    ]
    assert resources["Mapping"]["Properties"]["Enabled"] is False
    assert resources["Mapping"]["Properties"]["EventSourceArn"] == {
        "Fn::GetAtt": ["Signals", "StreamArn"]
    }
    new_name = next(name for name in resources if name.startswith("RecoveredMapping"))
    assert (
        resources[new_name]["Properties"]["EventSourceArn"]
        == binding["resources"]["Signals"]["stream_arn"]
    )
    assert apply_binding(value, binding) == value
    assert resources["Mapping"]["UpdateReplacePolicy"] == "Retain"
    assert value["Outputs"]["Table"]["Value"] == "opencdr-dev-signals-recovered"
    assert current_binding(value) == binding
    assert "stream_arn" not in value["Metadata"][METADATA_KEY]["resources"]["CaseBucket"]


def test_cloudformation_metadata_omits_null_and_roundtrips_without_losing_identity(
    template, binding
):
    template["Resources"].pop("Mapping")
    binding["resources"]["Signals"]["stream_arn"] = None
    frozen = copy.deepcopy(binding)
    deployed = apply_binding(template, binding)
    assert current_binding(deployed) == frozen
    assert all(
        "stream_arn" not in row for row in deployed["Metadata"][METADATA_KEY]["resources"].values()
    )
    assert apply_binding(deployed, binding) == deployed
    assert binding == frozen


@pytest.mark.parametrize(
    "change",
    [
        {"name": "opencdr-dev-signals"},
        {"name": "original-cases"},
        {"id": "11111111-1111-1111-1111-111111111111"},
        {"stream_arn": "arn:aws:dynamodb:us-east-1:222222222222:table/other/stream/2026"},
        {"type": "AWS::IAM::Role"},
    ],
)
def test_binding_cannot_overwrite_source_reuse_identity_or_redirect_stream(binding, change):
    binding["resources"]["Signals"].update(change)
    with pytest.raises(FleetManagerError):
        validate(binding)


def test_missing_restored_stream_blocks_mapping_even_with_valid_table(template, binding):
    binding["resources"]["Signals"]["stream_arn"] = None
    with pytest.raises(FleetManagerError, match="Unsupported recovered attribute"):
        apply_binding(template, binding)


def test_retained_stream_reader_keeps_old_stream_without_old_table_writes(template, binding):
    statements = template["Resources"]["Role"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    statements[:] = [
        {
            "Effect": "Allow",
            "Action": ["dynamodb:GetRecords", "dynamodb:GetShardIterator"],
            "Resource": [{"Fn::GetAtt": ["Signals", "StreamArn"]}],
        },
        {
            "Effect": "Allow",
            "Action": ["dynamodb:PutItem"],
            "Resource": [{"Fn::GetAtt": ["Signals", "Arn"]}],
        },
    ]
    result = apply_binding(template, binding)
    after = result["Resources"]["Role"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert after[0]["Resource"] == [
        binding["resources"]["Signals"]["stream_arn"],
        {"Fn::GetAtt": ["Signals", "StreamArn"]},
    ]
    assert after[1]["Resource"] == [
        "arn:aws:dynamodb:us-east-1:111111111111:table/opencdr-dev-signals-recovered"
    ]
    assert apply_binding(result, binding) == result


def test_future_release_cannot_drop_recovered_resource(template, binding):
    del template["Resources"]["Signals"]
    with pytest.raises(FleetManagerError, match="no longer defines"):
        apply_binding(template, binding)


def test_unknown_attribute_and_shadowed_alias_fail_closed(template, binding):
    template["Outputs"]["Table"]["Value"] = {"Fn::GetAtt": "Signals.Unknown"}
    with pytest.raises(FleetManagerError, match="Unsupported recovered attribute"):
        apply_binding(template, binding)
    template["Outputs"]["Table"]["Value"] = {"Fn::Sub": ["${Signals}", {"Signals": "alias"}]}
    with pytest.raises(FleetManagerError, match="shadowed"):
        apply_binding(template, binding)


def test_feed_payload_binding_preserves_raw_ingestion_and_exact_iam_prefix(template, binding):
    binding["resources"]["IngestionBucket"] = {
        "type": "AWS::S3::Bucket",
        "source_name": "raw-ingestion",
        "source_id": "raw-ingestion",
        "name": "recovered-feed-payloads",
        "id": "recovered-feed-payloads",
        "stream_arn": None,
        "usage": "detection_payload",
    }
    template["Resources"]["IngestionBucket"] = {
        "Type": "AWS::S3::Bucket",
        "Properties": {"BucketName": "raw-ingestion"},
    }
    env = template["Resources"]["Function"]["Properties"]["Environment"]["Variables"]
    env.update(INGESTION_BUCKET={"Ref": "IngestionBucket"}, DETECTION_FEED_TABLE_NAME="feed")
    policy = template["Resources"]["Role"]["Properties"]["Policies"][0]["PolicyDocument"]
    original = {"Fn::Sub": "${IngestionBucket.Arn}/detection-feed/*"}
    policy["Statement"].append({"Action": ["s3:GetObject"], "Resource": original})
    value = apply_binding(template, binding)
    env = value["Resources"]["Function"]["Properties"]["Environment"]["Variables"]
    assert env["INGESTION_BUCKET"] == {"Ref": "IngestionBucket"}
    assert env["DETECTION_PAYLOAD_BUCKET"] == "recovered-feed-payloads"
    statement = value["Resources"]["Role"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ][-1]
    assert statement == {
        "Action": ["s3:GetObject"],
        "Resource": [
            original,
            {
                "Fn::Sub": "arn:aws:s3:::recovered-feed-payloads/detection-feed/*",
            },
        ],
    }
    assert value["Resources"]["IngestionBucket"]["Properties"]["BucketName"] == "raw-ingestion"


def test_materialized_release_and_activation_use_restored_ids(template, binding):
    from opencdr_deploy.deployment_template import materialize
    from opencdr_deploy.storage_bindings import bound_ids, current_binding

    deployed = materialize(template, {"Stage": "dev"}, binding=binding)
    assert current_binding(deployed) == binding
    ids = bound_ids(
        {"Signals": "opencdr-dev-signals", "CaseBucket": "original-cases", "Queue": "kept"},
        deployed,
    )
    assert ids == {
        "Signals": "opencdr-dev-signals-recovered",
        "CaseBucket": "restored-cases",
        "Queue": "kept",
    }
    assert deployed == materialize(template, {"Stage": "dev"}, binding=current_binding(deployed))


def test_second_restore_keeps_original_literal_names_and_does_not_alias_sources(template, binding):  # noqa: F811
    prior = binding["resources"]["Signals"]
    prior.update(
        original_name=prior["source_name"],
        source_name=prior["name"],
        source_id=prior["id"],
        name="opencdr-dev-signals-recovered-again",
        id="33333333-3333-3333-3333-333333333333",
    )
    prior["stream_arn"] = prior["stream_arn"].replace(
        "/opencdr-dev-signals-recovered/", "/opencdr-dev-signals-recovered-again/"
    )
    value = apply_binding(template, binding)
    assert value["Resources"]["Function"]["Properties"]["Environment"]["Variables"]["TABLE"] == {
        "Fn::Sub": prior["name"]
    }
    prior["name"] = prior["original_name"]
    prior["stream_arn"] = None
    with pytest.raises(FleetManagerError, match="other source resources"):
        validate(binding)
