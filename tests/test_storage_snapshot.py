import hashlib
import json
import uuid
from unittest.mock import Mock

import boto3
import pytest
from moto import mock_aws

from opencdr_deploy.fleet_api import FleetManagerError
from opencdr_deploy.storage_snapshot import (
    backup_table,
    capture_objects,
    compare_fingerprints,
    restore_objects,
    restore_table,
    table_fingerprint,
    verify_source_objects,
)

ACCOUNT = "123456789012"


@pytest.fixture
def storage(monkeypatch):
    # Moto omits DynamoDB's immutable physical IDs. Model them independently of
    # stored rows, including restored tables, exactly as the engine tests do.
    from moto.dynamodb.models import Table

    describe = Table.describe

    def identified(self, base_key="TableDescription"):
        value = describe(self, base_key)
        value[base_key]["TableId"] = str(
            uuid.uuid5(uuid.NAMESPACE_URL, self.table_arn + ":" + str(self.created_at))
        )
        return value

    monkeypatch.setattr(Table, "describe", identified)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        ddb = boto3.client("dynamodb")
        table = ddb.create_table(
            TableName="source-cases",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
        )["TableDescription"]
        for pk, sk in [("CASE#one", "META"), ("EVENT#one", "0001"), ("EFFECT#one", "CLAIM")]:
            ddb.put_item(
                TableName="source-cases",
                Item={
                    "pk": {"S": pk},
                    "sk": {"S": sk},
                    "evidence": {"B": b"bytes"},
                    "tags": {"SS": ["b", "a"]},
                },
            )
        s3 = boto3.client("s3")

        # Moto treats this conditional Deny as unconditional. Check the exact
        # policy here; real TLS policy enforcement belongs to the AWS rehearsal.
        def tls_policy(**args):
            policy = json.loads(args["Policy"])
            assert policy["Statement"][0]["Effect"] == "Deny"
            assert policy["Statement"][0]["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}

        monkeypatch.setattr(s3, "put_bucket_policy", tls_policy)
        for name in ["source-evidence", "snapshot-artifacts"]:
            s3.create_bucket(Bucket=name)
        s3.put_bucket_versioning(
            Bucket="source-evidence", VersioningConfiguration={"Status": "Enabled"}
        )
        s3.put_object(Bucket="source-evidence", Key="case/immutable.json", Body=b'{"id":1}')
        yield ddb, table, s3


def test_physical_restore_keeps_audit_and_permanent_claims(storage):
    ddb, table, _ = storage
    before = table_fingerprint(ddb, "source-cases", table["TableId"])
    assert before["rows"] == 3 and before["execution_claims"] == 1
    backup = backup_table(ddb, "source-cases", table["TableId"], "unique-plan-backup")
    assert backup_table(ddb, "source-cases", table["TableId"], "unique-plan-backup") == backup
    restored = restore_table(ddb, backup, "restored-cases", "a" * 64)
    assert restored["id"] != table["TableId"]
    compare_fingerprints(before, table_fingerprint(ddb, restored["name"], restored["id"]))
    assert restore_table(ddb, backup, "restored-cases", "a" * 64)["id"] == restored["id"]
    ddb.delete_item(
        TableName="restored-cases", Key={"pk": {"S": "EFFECT#one"}, "sk": {"S": "CLAIM"}}
    )
    with pytest.raises(FleetManagerError, match="permanent execution claims"):
        compare_fingerprints(before, table_fingerprint(ddb, restored["name"], restored["id"]))
    assert table_fingerprint(ddb, "source-cases", table["TableId"]) == before


def test_restore_rejects_existing_unrelated_table(storage):
    ddb, table, _ = storage
    backup = backup_table(ddb, "source-cases", table["TableId"], "unique-plan-backup")
    with pytest.raises(FleetManagerError, match="available"):
        restore_table(ddb, backup, "source-cases", "a" * 64)
    ddb.create_table(
        TableName="unrelated",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
    )
    with pytest.raises(FleetManagerError, match="not the reviewed"):
        restore_table(ddb, backup, "unrelated", "a" * 64)


def test_restore_summary_may_disappear_but_physical_identity_cannot(storage, monkeypatch):
    ddb, table, _ = storage
    backup = backup_table(ddb, "source-cases", table["TableId"], "witness-backup")
    record = Mock()
    restored = restore_table(ddb, backup, "witness-restored", "a" * 64, record=record)
    witness = record.call_args.args[0]
    describe = ddb.describe_table

    def without_summary(**kwargs):
        value = describe(**kwargs)
        value["Table"].pop("RestoreSummary", None)
        return value

    monkeypatch.setattr(ddb, "describe_table", without_summary)
    assert (
        restore_table(ddb, backup, "witness-restored", "a" * 64, provenance=witness)["id"]
        == restored["id"]
    )
    with pytest.raises(FleetManagerError, match="not the reviewed"):
        restore_table(ddb, backup, "witness-restored", "a" * 64)
    with pytest.raises(FleetManagerError, match="not the reviewed"):
        restore_table(
            ddb,
            backup,
            "witness-restored",
            "a" * 64,
            provenance={**witness, "table_id": "replaced"},
        )


def test_identity_change_and_row_bound_block_capture(storage):
    ddb, table, _ = storage
    with pytest.raises(FleetManagerError, match="identity changed"):
        table_fingerprint(ddb, "source-cases", "wrong-id")
    with pytest.raises(FleetManagerError, match="row bound"):
        table_fingerprint(ddb, "source-cases", table["TableId"], max_rows=2)


def test_evidence_restore_verifies_bytes_versions_and_cannot_overwrite_source(storage):
    _, _, s3 = storage
    snapshot = capture_objects(
        s3,
        "source-evidence",
        "snapshot-artifacts",
        ACCOUNT,
        prefix="case/",
        run_prefix="recovery/one",
    )
    verify_source_objects(s3, snapshot, ACCOUNT)
    restored = restore_objects(s3, snapshot, "restored-evidence", ACCOUNT, "us-east-1", "a" * 64)
    receipt = restored["versions"][0]
    assert receipt["source_version_id"] != receipt["restored_version_id"]
    assert receipt["sha256"] == hashlib.sha256(b'{"id":1}').hexdigest()
    assert (
        restore_objects(s3, snapshot, "restored-evidence", ACCOUNT, "us-east-1", "a" * 64)[
            "versions"
        ]
        == restored["versions"]
    )
    with pytest.raises(FleetManagerError, match="separate destination"):
        restore_objects(s3, snapshot, "source-evidence", ACCOUNT, "us-east-1", "a" * 64)
    s3.put_object(Bucket="restored-evidence", Key="case/immutable.json", Body=b"corruption")
    with pytest.raises(FleetManagerError, match="Restored evidence bytes"):
        restore_objects(s3, snapshot, "restored-evidence", ACCOUNT, "us-east-1", "a" * 64)


def test_snapshot_detects_change_and_backup_tampering(storage):
    _, _, s3 = storage
    snapshot = capture_objects(
        s3,
        "source-evidence",
        "snapshot-artifacts",
        ACCOUNT,
        prefix="case/",
        run_prefix="recovery/one",
    )
    s3.put_object(Bucket="source-evidence", Key="case/new.json", Body=b"new")
    with pytest.raises(FleetManagerError, match="Source object set changed"):
        verify_source_objects(s3, snapshot, ACCOUNT)
    obj = snapshot["objects"][0]
    s3.put_object(Bucket=obj["backup_bucket"], Key=obj["backup_key"], Body=b"tampered")
    with pytest.raises(FleetManagerError, match="Backup object integrity"):
        restore_objects(s3, snapshot, "restored-evidence", ACCOUNT, "us-east-1", "a" * 64)
