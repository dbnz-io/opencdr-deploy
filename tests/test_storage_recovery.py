import copy
from unittest.mock import Mock

import pytest
from test_storage_snapshot import storage  # noqa: F401 -- shared Moto fixture, real storage operations

from opencdr_deploy import storage_recovery as r
from opencdr_deploy import storage_snapshot as s
from opencdr_deploy.fleet_api import FleetManagerError


@pytest.fixture
def snapshot_system(storage, monkeypatch):  # noqa: F811 -- imported pytest fixture
    ddb, table, s3 = storage
    session = Mock()
    session.client.side_effect = lambda name, **_: {"dynamodb": ddb, "s3": s3}[name]
    plan = {
        "account_id": "123456789012",
        "region": "us-east-1",
        "stage": "verify",
        "stack_name": "opencdr-verify",
        "stack_id": "reviewed-stack",
        "artifact_bucket": "snapshot-artifacts",
        "tables": {
            "CaseTable": {
                "name": "source-cases",
                "id": table["TableId"],
                "spec": {
                    "StreamSpecification": {
                        "StreamViewType": "NEW_AND_OLD_IMAGES",
                    }
                },
            }
        },
        "buckets": {
            "CaseBucket": {"name": "source-evidence", "prefix": "case/", "usage": "storage"}
        },
    }
    # Quarantine controls are exercised separately. This fixture runs real
    # DynamoDB backup/restore and S3 object/CAS operations through Moto.
    drained = Mock(return_value={"state": "frozen"})
    monkeypatch.setattr(r.quarantine, "require_drained", drained)
    return session, plan, ddb, s3, drained


def test_capture_restore_retries_preserve_claims_versions_and_originals(snapshot_system):
    session, plan, ddb, s3, drained = snapshot_system
    capture = r.capture(session, plan, s.sha256(plan))
    assert capture["state"] == "captured"
    assert r.capture(session, plan, s.sha256(plan)) == capture
    document = capture["snapshot"]
    result = r.restore(session, plan, document, capture["snapshot_sha256"])
    if result["state"] == "restores_pending":
        result = r.restore(session, plan, document, capture["snapshot_sha256"])
    assert result["state"] == "restored"
    assert result["receipt"]["fingerprints"]["CaseTable"]["execution_claims"] == 1
    assert (
        result["receipt"]["binding"]["resources"]["CaseTable"]["id"]
        != plan["tables"]["CaseTable"]["id"]
    )
    assert r.restore(session, plan, document, capture["snapshot_sha256"]) == result
    assert result["receipt"]["activated"] is False
    assert result["receipt"]["original_resources_retained"] is True
    assert len(s3.list_objects_v2(Bucket="source-evidence")["Contents"]) == 1
    s.compare_fingerprints(
        document["tables"]["CaseTable"]["fingerprint"],
        s.table_fingerprint(ddb, "source-cases", plan["tables"]["CaseTable"]["id"]),
    )
    assert drained.call_count >= 6


def test_changed_source_or_missing_quarantine_never_produces_restore_receipt(snapshot_system):
    session, plan, ddb, _, drained = snapshot_system
    capture = r.capture(session, plan, s.sha256(plan))
    ddb.delete_item(TableName="source-cases", Key={"pk": {"S": "EFFECT#one"}, "sk": {"S": "CLAIM"}})
    with pytest.raises(FleetManagerError, match="permanent execution claims"):
        r.restore(session, plan, capture["snapshot"], capture["snapshot_sha256"])
    drained.side_effect = FleetManagerError("quarantine lost")
    with pytest.raises(FleetManagerError, match="quarantine lost"):
        r.capture(session, plan, s.sha256(plan))


def test_tampered_manifest_and_other_snapshot_are_rejected(snapshot_system):
    session, plan, *_ = snapshot_system
    capture = r.capture(session, plan, s.sha256(plan))
    bad = copy.deepcopy(capture["snapshot"])
    bad["tables"].clear()
    with pytest.raises(FleetManagerError, match="digest mismatch"):
        r.restore(session, plan, bad, capture["snapshot_sha256"])
    with pytest.raises(FleetManagerError, match="does not cover"):
        r.restore(session, plan, bad, s.sha256(bad))


def test_lost_backup_response_reuses_the_same_backup(snapshot_system, monkeypatch):
    session, plan, ddb, *_ = snapshot_system
    original = ddb.create_backup

    def lost(**kwargs):
        original(**kwargs)
        raise TimeoutError("response lost after AWS committed")

    monkeypatch.setattr(ddb, "create_backup", lost)
    with pytest.raises(TimeoutError):
        r.capture(session, plan, s.sha256(plan))
    monkeypatch.setattr(ddb, "create_backup", original)
    result = r.capture(session, plan, s.sha256(plan))
    assert result["state"] == "captured"
    assert len(ddb.list_backups(TableName="source-cases")["BackupSummaries"]) == 1


def test_restoration_rejects_a_changed_index_even_when_rows_match(snapshot_system):
    session, plan, ddb, _, _ = snapshot_system
    capture = r.capture(session, plan, s.sha256(plan))
    result = r.restore(session, plan, capture["snapshot"], capture["snapshot_sha256"])
    if result["state"] == "restores_pending":
        result = r.restore(session, plan, capture["snapshot"], capture["snapshot_sha256"])
    name = result["receipt"]["binding"]["resources"]["CaseTable"]["name"]
    ddb.update_table(
        TableName=name,
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        GlobalSecondaryIndexUpdates=[
            {
                "Create": {
                    "IndexName": "unexpected-index",
                    "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                }
            }
        ],
    )
    with pytest.raises(FleetManagerError, match="permanent execution claims"):
        r.restore(session, plan, capture["snapshot"], capture["snapshot_sha256"])


def test_cutover_rejects_forged_receipt_even_when_its_hash_is_supplied(
    snapshot_system, monkeypatch
):
    from opencdr_deploy import storage_cutover as c

    session, plan, _, _, _ = snapshot_system
    capture = r.capture(session, plan, s.sha256(plan))
    result = r.restore(session, plan, capture["snapshot"], capture["snapshot_sha256"])
    if result["state"] == "restores_pending":
        result = r.restore(session, plan, capture["snapshot"], capture["snapshot_sha256"])
    receipt = result["receipt"]
    # Physical verification has separate tests; exercise the sealed records and
    # real restored data here, including a caller supplying a recomputed hash.
    monkeypatch.setattr(c, "verify_physical", Mock())
    c.verify_receipt(session, plan, receipt, s.sha256(receipt))
    bad = copy.deepcopy(receipt)
    bad["fingerprints"]["CaseTable"]["execution_claims"] = 0
    with pytest.raises(FleetManagerError, match="sealed recovery records"):
        c.verify_receipt(session, plan, bad, s.sha256(bad))


def test_cutover_rechecks_restored_claims_after_receipt_was_sealed(snapshot_system, monkeypatch):
    from opencdr_deploy import storage_cutover as c

    session, plan, ddb, _, _ = snapshot_system
    capture = r.capture(session, plan, s.sha256(plan))
    result = r.restore(session, plan, capture["snapshot"], capture["snapshot_sha256"])
    if result["state"] == "restores_pending":
        result = r.restore(session, plan, capture["snapshot"], capture["snapshot_sha256"])
    receipt = result["receipt"]
    name = receipt["binding"]["resources"]["CaseTable"]["name"]
    ddb.delete_item(TableName=name, Key={"pk": {"S": "EFFECT#one"}, "sk": {"S": "CLAIM"}})
    monkeypatch.setattr(c, "verify_physical", Mock())
    with pytest.raises(FleetManagerError, match="permanent execution claims"):
        c.verify_receipt(session, plan, receipt, s.sha256(receipt))


def test_controller_persists_restore_identity_before_summary_disappears(
    snapshot_system, monkeypatch
):
    session, plan, ddb, _, _ = snapshot_system
    capture = r.capture(session, plan, s.sha256(plan))
    result = r.restore(session, plan, capture["snapshot"], capture["snapshot_sha256"], attempt=2)
    original = ddb.describe_table

    def without_summary(**kwargs):
        value = original(**kwargs)
        value["Table"].pop("RestoreSummary", None)
        return value

    monkeypatch.setattr(ddb, "describe_table", without_summary)
    result = r.restore(session, plan, capture["snapshot"], capture["snapshot_sha256"], attempt=2)
    assert result["state"] == "restored"
    assert result["receipt"]["restore_attempt"] == 2
    assert "-a2-" in result["receipt"]["binding"]["resources"]["CaseTable"]["name"]
    with pytest.raises(FleetManagerError, match="already sealed"):
        r.restore(session, plan, capture["snapshot"], capture["snapshot_sha256"], attempt=3)
