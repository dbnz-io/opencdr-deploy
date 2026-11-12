import json
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from opencdr_deploy import ownership_migration as m
from opencdr_deploy import ownership_transfer as t
from opencdr_deploy.cli import build_parser
from opencdr_deploy.fleet_api import FleetManagerError
from opencdr_deploy.storage_quarantine import _get, require_unlocked


@pytest.fixture
def system():
    with mock_aws():
        session = boto3.Session(region_name="us-east-1")
        s3 = session.client("s3")
        s3.create_bucket(Bucket="migration-evidence")
        ddb = session.client("dynamodb")
        ddb.create_table(
            TableName="cases",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in ["pk", "sk"]],
            BillingMode="PAY_PER_REQUEST",
        )
        stack = {
            "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/opencdr-test/id",
            "Parameters": [
                {"ParameterKey": "ArtifactBucket", "ParameterValue": "migration-evidence"}
            ],
        }
        pin = {
            "account_id": "123456789012",
            "stack_id": stack["StackId"],
            "artifact_bucket": "migration-evidence",
            "table": "cases",
        }
        yield SimpleNamespace(session=session, s3=s3, ddb=ddb, stack=stack, pin=pin)


def test_ownership_lease_is_the_existing_deploy_recovery_exclusion(system):
    m.lease(system.session, system.pin, "a" * 64)
    m.lease(system.session, system.pin, "a" * 64)
    with pytest.raises(FleetManagerError, match="owns this stack"):
        require_unlocked(system.session, system.stack)
    with pytest.raises(FleetManagerError, match="Another recovery"):
        m.lease(system.session, system.pin, "b" * 64)
    assert _get(system.s3, "migration-evidence", m.lock_key(system.pin), "123456789012")[
        0
    ] == m.lock_value("a" * 64)


def test_completed_plan_cannot_reacquire_released_lease(system):
    system.s3.put_object(
        Bucket="migration-evidence",
        Key=m.lock_key(system.pin),
        Body=m.canonical({"released_plan_sha256": "a" * 64}),
    )
    require_unlocked(system.session, system.stack)
    with pytest.raises(FleetManagerError, match="already released"):
        m.lease(system.session, system.pin, "a" * 64)
    m.lease(system.session, system.pin, "b" * 64)


def test_retained_evidence_never_overwrites_different_bytes(system):
    first = m.retain(system.session, system.pin, "a" * 64, "plan.json", b"first")
    assert m.retain(system.session, system.pin, "a" * 64, "plan.json", b"first") == first
    with pytest.raises(FleetManagerError, match="differs"):
        m.retain(system.session, system.pin, "a" * 64, "plan.json", b"other")


def test_lost_first_lease_can_be_cancelled_without_touching_operational_owner(system, monkeypatch):
    m.lease(system.session, system.pin, "a" * 64)
    monkeypatch.setattr(m, "verify_account", lambda *args: None)
    client = Mock(wraps=system.ddb)
    client.describe_table.return_value = {"Table": {"TableId": "physical"}}
    real = m.aws_client
    monkeypatch.setattr(
        m, "aws_client", lambda session, kind: client if kind == "dynamodb" else real(session, kind)
    )
    pin = {**system.pin, "table_id": "physical"}
    plan = {"source": pin}
    done = m.cancel_unstarted(system.session, plan, "a" * 64)
    assert done["phase"] == "cancelled_unstarted"
    assert not system.ddb.get_item(
        TableName="cases", Key=t.wire({"pk": "OWNERSHIP", "sk": "cases"})
    ).get("Item")
    with pytest.raises(ClientError, match="ConditionalCheckFailed"):
        m.cancel_unstarted(system.session, plan, "a" * 64)


def test_cli_requires_reviewed_identity_and_archive_for_mutations():
    parser = build_parser()
    args = parser.parse_args(
        [
            "ownership",
            "activate",
            "--source-profile",
            "source",
            "--target-profile",
            "target",
            "--plan",
            "plan.json",
            "--sha256",
            "a" * 64,
            "--directory",
            "archive",
            "--manifest-sha256",
            "b" * 64,
            "--output",
            "receipt.json",
        ]
    )
    assert args.ownership_action == "activate"
    with pytest.raises(SystemExit):
        parser.parse_args(["ownership", "freeze"])


def test_plan_tampering_is_rejected_before_aws(tmp_path):
    file = tmp_path / "plan.json"
    file.write_text(json.dumps({"schema": t.SCHEMA}))
    with pytest.raises(FleetManagerError, match="digest"):
        m.load_plan(file, "0" * 64)


def test_completion_releases_only_after_durable_state_and_resumes_lost_release(
    tmp_path, monkeypatch
):
    from test_ownership_transfer import capture, controller, make_plan, stores

    with stores() as (source, target, _, _):
        session = boto3.Session(region_name="us-east-1")
        for item in [source, target]:
            item.pin["artifact_bucket"] = item.bucket
        plan = make_plan(source, target)
        c = controller(source, target, plan)
        for item in [source, target]:
            m.lease(session, item.pin, c.id)
        c.freeze()
        digest = capture(source, "cases", tmp_path / "archive")
        c.stage(tmp_path / "archive", digest)
        c.activate(tmp_path / "archive", digest)
        monkeypatch.setattr(m, "guard", lambda *args: None)
        monkeypatch.setattr(m, "verify_functions", lambda *args: None)
        real = m.aws_client
        failed = []

        def client(session_, kind):
            wrapped = real(session_, kind)
            if kind == "s3":
                put = wrapped.put_object

                def write(**kwargs):
                    result = put(**kwargs)
                    if (
                        kwargs["Key"] == m.lock_key(source.pin)
                        and b"released_plan_sha256" in kwargs["Body"]
                        and not failed
                    ):
                        failed.append(True)
                        raise OSError("release response lost")
                    return result

                wrapped.put_object = write
            return wrapped

        monkeypatch.setattr(m, "aws_client", client)
        with pytest.raises(OSError):
            m.complete([session, session], plan, c.id)
        result = m.complete([session, session], plan, c.id)
        assert result["journal"]["phase"] == "complete" and not result["data_deleted"]
        for item in [source, target]:
            old, _ = _get(source.s3, item.bucket, m.lock_key(item.pin), "123456789012")
            assert old == {"released_plan_sha256": c.id}
        assert source.get("OWNERSHIP", "cases") == c.sr


def test_retrieve_recovers_pinned_archive_without_writes(system, tmp_path):
    from test_case_archive import fixture, pack

    from opencdr_deploy import migration_archive as archive

    plan = {
        "schema": t.SCHEMA,
        "source": system.pin,
        "target": {**system.pin, "account_id": "999999999999"},
    }
    digest = m.sha(m.canonical(plan))
    m.retain(system.session, system.pin, digest, "plan.json", m.canonical(plan))
    rows, objects = fixture()
    result = archive.export(pack(rows, objects), tmp_path / "original")
    m.retained_archive(
        system.session, system.pin, digest, tmp_path / "original", result["manifest_sha256"]
    )
    before = system.s3.list_objects_v2(Bucket="migration-evidence")["Contents"]
    report = m.retrieve(
        system.session,
        "123456789012",
        "migration-evidence",
        digest,
        tmp_path / "retrieved",
        result["manifest_sha256"],
    )
    assert not report["cloud_mutations"]
    assert archive.verify(
        tmp_path / "retrieved/archive", result["manifest_sha256"]
    ) == archive.verify(tmp_path / "original", result["manifest_sha256"])
    assert before == system.s3.list_objects_v2(Bucket="migration-evidence")["Contents"]


@pytest.mark.parametrize(
    "defect", [None, "wrong_identity", "missing_registration", "collision", "wrong_core_scope"]
)
def test_scope_map_pins_actual_registry_identity_before_fencing(defect):
    from test_ownership_transfer import stores

    with stores() as (source, target, _, _):
        source.pin.update(
            product="opencdr",
            account_id="111111111111",
            region="us-east-1",
            stage="fixture",
            scope_bindings={},
        )
        target.pin.update(
            product="opencdr-fleet-manager", registry_table="registry", scope_bindings={}
        )
        target.ddb.create_table(
            TableName="registry",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        row = {
            "id": "AGENT#member-1",
            "account_id": "111111111111",
            "region": "us-east-1",
            "stage": "fixture",
            "rev": 1,
        }
        if defect == "wrong_identity":
            row["account_id"] = "999999999999"
        if defect != "missing_registration":
            target.ddb.put_item(TableName="registry", Item=t.wire(row))
        mapping = {"111111111111:us-east-1:fixture": "member-1"}
        if defect == "collision":
            mapping["extra"] = "member-1"
        if defect == "wrong_core_scope":
            source.pin["stage"] = "different"
        if defect:
            with pytest.raises(FleetManagerError):
                m.validate_scope_map(source, target, "cases", mapping)
        else:
            m.validate_scope_map(source, target, "cases", mapping)
            assert target.pin["scope_bindings"]["member-1"] == m.sha(m.canonical(t.wire(row)))
        assert source.get("OWNERSHIP", "cases") is None and target.get("OWNERSHIP", "cases") is None
