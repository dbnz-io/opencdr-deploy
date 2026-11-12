import base64
import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from opencdr_deploy import artifacts
from opencdr_deploy import detection_activation as activation
from opencdr_deploy.fleet_api import FleetManagerError


@pytest.fixture
def system(tmp_path, monkeypatch):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        describe = ddb.describe_table

        def with_identity(**kwargs):
            # Moto omits TableId. Model distinct physical identities explicitly;
            # conditions and transactions still execute through Moto's engine.
            result = describe(**kwargs)
            result["Table"]["TableId"] = "physical-" + kwargs["TableName"]
            return result

        monkeypatch.setattr(ddb, "describe_table", with_identity)
        for name in ("feed", "signals"):
            ddb.create_table(
                TableName=name,
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[
                    {"AttributeName": k, "AttributeType": "N" if k == "sequence" else "S"}
                    for k in ("pk", "sequence", "day", "order")
                ],
                KeySchema=[
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "sequence", "KeyType": "RANGE"},
                ],
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": "by-day",
                        "KeySchema": [
                            {"AttributeName": "day", "KeyType": "HASH"},
                            {"AttributeName": "order", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    }
                ],
            )
        ids = {
            "DetectionFeedTable": "feed",
            "SignalsTableV2": "signals",
            "ProcessorLambdaFunction": "opencdr-dev-processor",
        }
        table_ids = {
            name: ddb.describe_table(TableName=name)["Table"]["TableId"]
            for name in ("feed", "signals")
        }
        ddb.put_item(
            TableName="feed",
            Item=activation.encode(
                {
                    "pk": "detections",
                    "sequence": 0,
                    "epoch": "e" * 32,
                    "table_id": table_ids["feed"],
                    "last_sequence": 3,
                }
            ),
        )
        job = {
            "pk": "backfill#history",
            "sequence": 0,
            "state": "completed",
            "retained_source_scan_complete": True,
            "epoch": "e" * 32,
            "feed_table_id": table_ids["feed"],
            "source_table_id": table_ids["signals"],
            "plan_digest": "b" * 64,
            "rev": 2,
            "counts": {"published": 3, "expired": 1, "invalid": 0, "conflict": 0},
            "created_at": "2026-01-01T01:00:00+00:00",
        }
        ddb.put_item(TableName="feed", Item=activation.encode(job))
        env = {
            "DETECTION_ACTIVATION_REQUIRED": "true",
            "DETECTION_RUNTIME_ID": "a" * 64,
            "DETECTION_FEED_TABLE_NAME": "feed",
            "SIGNALS_TABLE_NAME": "signals",
        }
        template = {
            "Resources": {
                "ProcessorLambdaFunction": {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {
                        "Handler": "src.handlers.processor.lambda_handler",
                        "Environment": {"Variables": deepcopy(env)},
                    },
                },
                "DetectionFeedTable": {"Type": "AWS::DynamoDB::Table"},
                "SignalsTableV2": {"Type": "AWS::DynamoDB::Table"},
            }
        }
        cfn, lambdas, sts, events, session = (Mock() for _ in range(5))
        stack = {
            "StackId": "arn:aws:cloudformation:us-east-1:111111111111:stack/opencdr-dev/test",
            "StackStatus": "UPDATE_COMPLETE",
            "CreationTime": datetime(2026, 1, 1, tzinfo=UTC),
            "Parameters": [],
        }
        cfn.describe_stacks.return_value = {"Stacks": [stack]}
        cfn.get_template.return_value = {"TemplateBody": template}
        cfn.list_stack_resources.return_value = {
            "StackResourceSummaries": [
                {"LogicalResourceId": k, "PhysicalResourceId": v} for k, v in ids.items()
            ]
        }
        config = {
            "FunctionArn": "arn:aws:lambda:us-east-1:111111111111:function:opencdr-dev-processor",
            "State": "Active",
            "LastUpdateStatus": "Successful",
            "Handler": "src.handlers.processor.lambda_handler",
            "Environment": {"Variables": env},
            "CodeSha256": base64.b64encode(bytes.fromhex("a" * 64)).decode(),
            "RevisionId": "revision-1",
            "LastModified": "2026-01-01T00:00:00+0000",
            "Timeout": 30,
        }
        lambdas.get_function_configuration.return_value = config
        lambdas.list_aliases.return_value = {"Aliases": []}
        lambdas.get_paginator.return_value.paginate.return_value = [
            {"EventSourceMappings": [{"FunctionArn": config["FunctionArn"]}]}
        ]
        sts.get_caller_identity.return_value = {
            "Account": "111111111111",
            "Arn": "arn:aws:iam::111111111111:role/operator",
        }
        session.client.side_effect = lambda service, **kwargs: {
            "dynamodb": ddb,
            "cloudformation": cfn,
            "lambda": lambdas,
            "events": events,
            "sts": sts,
        }[service]
        release = ({"product": "opencdr", "files": {"runtime.zip": "a" * 64}}, deepcopy(template))
        monkeypatch.setattr(artifacts, "load_release", lambda *args: release)
        args = SimpleNamespace(
            stack="opencdr-dev",
            action="activate",
            directory=tmp_path / "release",
            release_sha256="c" * 64,
            backfill_id="history",
            reason="Retained findings reviewed; unavailable history accepted",
            output=tmp_path / "activation.json",
            account_id="111111111111",
            region="us-east-1",
            profile="dev",
        )
        yield SimpleNamespace(
            ddb=ddb,
            cfn=cfn,
            lambdas=lambdas,
            config=config,
            events=events,
            session=session,
            args=args,
            template=template,
            job=job,
            stack=stack,
        )


def prepare(system):
    result = activation.plan(system.args, system.session)
    return SimpleNamespace(
        plan=system.args.output,
        sha256=result["sha256"],
        account_id=system.args.account_id,
        region=system.args.region,
    ), result


def head(system):
    return activation.decode(
        system.ddb.get_item(TableName="feed", Key=activation.HEAD, ConsistentRead=True)["Item"]
    )


def test_review_apply_retry_and_pause_preserve_head_and_keep_history_unverified(system):
    args, result = prepare(system)
    assert not result["applied"] and "activation" not in head(system)
    assert result["review"]["snapshot"]["backfill"]["counts"]["expired"] == 1
    assert not result["review"]["history_complete"]
    assert activation.apply(args, system.session)["revision"] == 1
    assert activation.apply(args, system.session)["duplicate"]
    assert head(system)["activation"]["approved_by"].endswith("role/operator")
    system.args.action = "pause"
    system.args.directory = None
    system.args.output = system.args.output.with_name("pause.json")
    pause, _ = prepare(system)
    activation.apply(pause, system.session)
    assert head(system)["last_sequence"] == 3 and "activation" not in head(system)
    assert head(system)["activation_revision"] == 2
    with pytest.raises(FleetManagerError, match="superseded"):
        activation.apply(args, system.session)


@pytest.mark.parametrize(
    "change", ["runtime", "source", "backfill", "revision", "account", "digest"]
)
def test_stale_or_wrong_plans_cannot_activate(system, change):
    args, _ = prepare(system)
    if change == "runtime":
        system.config["RevisionId"] = "changed-after-review"
    elif change in {"source", "backfill"}:
        system.job["source_table_id" if change == "source" else "created_at"] = (
            "another-table" if change == "source" else "2026-01-01T00:01:00+00:00"
        )
        system.ddb.put_item(TableName="feed", Item=activation.encode(system.job))
    elif change == "revision":
        system.ddb.update_item(
            TableName="feed",
            Key=activation.HEAD,
            UpdateExpression="SET activation_revision = :r",
            ExpressionAttributeValues={":r": {"N": "1"}},
        )
    elif change == "account":
        args.account_id = "222222222222"
    else:
        args.sha256 = "f" * 64
    with pytest.raises(FleetManagerError):
        activation.apply(args, system.session)
    assert "activation" not in head(system)


@pytest.mark.parametrize(
    "change", ["alias", "mapping", "code", "guard", "template", "in_progress", "managed"]
)
def test_unqualified_settled_pinned_runtime_is_required(system, change):
    if change == "alias":
        system.lambdas.list_aliases.return_value = {"Aliases": [{"Name": "old"}]}
    elif change == "mapping":
        system.lambdas.get_paginator.return_value.paginate.return_value = [
            {"EventSourceMappings": [{"FunctionArn": system.config["FunctionArn"] + ":1"}]}
        ]
    elif change == "code":
        system.config["CodeSha256"] = base64.b64encode(bytes.fromhex("b" * 64)).decode()
    elif change == "guard":
        system.config["Environment"]["Variables"]["DETECTION_ACTIVATION_REQUIRED"] = "false"
    elif change == "template":
        system.template["Description"] = "unreviewed change"
    elif change == "managed":
        system.config["CapacityProviderConfig"] = {
            "LambdaManagedInstancesCapacityProviderConfig": {"CapacityProviderArn": "managed"}
        }
    else:
        system.stack["StackStatus"] = "UPDATE_IN_PROGRESS"
    with pytest.raises(FleetManagerError):
        prepare(system)
    assert not system.args.output.exists()


def test_pending_deployment_blocks_activation_until_exact_changeset_completes(system):
    activation.claim_deployment(system.session, "opencdr-dev", system.template, "change-123")
    assert head(system)["deployment_pending"] == "change-123"
    with pytest.raises(FleetManagerError, match="not completed"):
        prepare(system)
    system.stack["ChangeSetId"] = "change-123"
    args, _ = prepare(system)
    assert activation.apply(args, system.session)["applied"]
    assert "deployment_pending" not in head(system)
    assert activation.apply(args, system.session)["duplicate"]


def test_uncertain_deployment_retry_preserves_the_original_claim(system):
    activation.claim_deployment(system.session, "opencdr-dev", system.template, "change-123")
    before = head(system)
    activation.claim_deployment(system.session, "opencdr-dev", system.template, "change-123")
    assert head(system) == before


def test_second_change_set_cannot_steal_an_unexecuted_deployment_claim(system):
    activation.claim_deployment(system.session, "opencdr-dev", system.template, "change-123")
    before = head(system)
    with pytest.raises(FleetManagerError, match="owns the deployment barrier"):
        activation.claim_deployment(system.session, "opencdr-dev", system.template, "change-456")
    assert head(system) == before
    system.cfn.execute_change_set.assert_not_called()


@pytest.mark.parametrize("status", ["UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"])
def test_settled_claim_can_be_superseded_by_a_reviewed_deployment(system, status):
    activation.claim_deployment(system.session, "opencdr-dev", system.template, "change-123")
    system.stack.update(StackStatus=status, ChangeSetId="change-123")
    activation.claim_deployment(system.session, "opencdr-dev", system.template, "change-456")
    assert head(system)["deployment_pending"] == "change-456"
    assert head(system)["activation_revision"] == 2


def test_deployment_claim_loses_race_to_activation_without_starting_update(system, monkeypatch):
    update = system.ddb.update_item

    def race(**kwargs):
        update(
            TableName="feed",
            Key=activation.HEAD,
            UpdateExpression="SET activation = :a",
            ExpressionAttributeValues={":a": {"M": {"runtime_sha256": {"S": "a" * 64}}}},
        )
        return update(**kwargs)

    monkeypatch.setattr(system.ddb, "update_item", race)
    with pytest.raises(FleetManagerError, match="raced"):
        activation.claim_deployment(system.session, "opencdr-dev", system.template, "change-123")
    assert "deployment_pending" not in head(system)
    system.cfn.execute_change_set.assert_not_called()


def test_release_execution_rechecks_pause_after_review(system, monkeypatch):
    args, _ = prepare(system)
    activation.apply(args, system.session)
    monkeypatch.setattr(artifacts.boto3, "Session", lambda **kwargs: system.session)
    system.cfn.describe_change_set.return_value = {
        "StackName": "opencdr-dev",
        "StackId": system.stack["StackId"],
        "Status": "CREATE_COMPLETE",
        "ExecutionStatus": "AVAILABLE",
    }
    args = SimpleNamespace(
        release_command="execute",
        profile="dev",
        account_id="111111111111",
        region="us-east-1",
        change_set_arn="arn:aws:cloudformation:us-east-1:111111111111:changeSet/opencdr-test/id",
    )
    with pytest.raises(FleetManagerError, match="Pause detection reads"):
        artifacts.run(args)
    system.cfn.execute_change_set.assert_not_called()


def test_plan_files_are_not_silently_overwritten(system):
    prepare(system)
    original = system.args.output.read_text()
    with pytest.raises(FileExistsError):
        prepare(system)
    assert system.args.output.read_text() == original
    assert json.loads(original)["schema"] == activation.SCHEMA
