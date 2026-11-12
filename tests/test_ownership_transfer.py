import copy
from contextlib import contextmanager

import boto3
import pytest
from moto import mock_aws
from test_case_archive import fixture as case_fixture
from test_library_archive import fixture as library_fixture

from opencdr_deploy import case_archive, library_archive
from opencdr_deploy import migration_archive as archive
from opencdr_deploy import ownership_transfer as t
from opencdr_deploy.fleet_api import FleetManagerError


@contextmanager
def stores(family="cases"):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        s3 = boto3.client("s3", region_name="us-east-1")
        result = []
        for mode in ["core", "fleet"]:
            ddb.create_table(
                TableName=mode,
                KeySchema=[
                    {"AttributeName": k, "KeyType": kind}
                    for k, kind in [("pk", "HASH"), ("sk", "RANGE")]
                ],
                AttributeDefinitions=[
                    {"AttributeName": k, "AttributeType": "S"} for k in ["pk", "sk"]
                ],
                BillingMode="PAY_PER_REQUEST",
            )["TableDescription"]
            bucket = "migration-" + mode
            s3.create_bucket(Bucket=bucket)
            pin = {
                "account_id": "123456789012",
                "region": "us-east-1",
                "stack_id": mode,
                "table": mode,
                "table_id": mode + "-table-id",
                "bucket": bucket,
            }
            # Moto may return the same placeholder TableId for different tables.
            pin["table_id"] = mode + "-table-id"
            result.append(t.Store(ddb, s3, pin, lambda pin: None))
        source, target = result
        rows, objects = (case_fixture if family == "cases" else library_fixture)()
        for row in rows:
            if row["pk"] != "OWNERSHIP":
                ddb.put_item(TableName=source.table, Item=t.wire(row))
        for key, raw in objects.items():
            s3.put_object(Bucket=source.bucket, Key=key, Body=raw)
        yield source, target, rows, objects


def make_plan(source, target, family="cases", previous=None):
    mapping = {"111111111111:us-east-1:fixture": "member-1"} if family == "cases" else {}
    if previous:
        mapping = {v: k for k, v in previous["plan"]["agent_map"].items()}
    return t.plan(
        source,
        target,
        family,
        mapping,
        reason="isolated transfer test",
        retain_until=2_000_000_000,
        previous=previous,
    )


def controller(source, target, plan):
    return t.Transfer(source, target, plan, t.sha(t.canonical(plan)))


def capture(store, family, directory):
    items = store.ddb.scan(TableName=store.table)["Items"]
    objects = {}
    for obj in store.s3.list_objects_v2(Bucket=store.bucket).get("Contents", []):
        response = store.s3.get_object(Bucket=store.bucket, Key=obj["Key"])
        objects[obj["Key"]] = response["Body"].read()
        response["Body"].close()
    adapter = case_archive if family == "cases" else library_archive
    return archive.export(adapter.native_source(store.pin, items, objects), directory)[
        "manifest_sha256"
    ]


@pytest.mark.parametrize("family", ["cases", "library"])
def test_resumable_handoff_never_copies_authority(tmp_path, family):
    with stores(family) as (source, target, _, _):
        p = make_plan(source, target, family)
        c = controller(source, target, p)
        assert c.freeze()["phase"] == "frozen"
        assert c.freeze()["phase"] == "frozen"
        digest = capture(source, family, tmp_path / "source")
        first = c.stage(tmp_path / "source", digest)
        assert first["destination_verified"]
        assert c.stage(tmp_path / "source", digest) == first
        assert target.get("REVIEW_POLICY", "old-owner") is None
        assert target.get("REVIEW_EXECUTION#old-owner", "META") is None
        done = c.activate(tmp_path / "source", digest)
        assert done["phase"] == "complete" and c.activate(tmp_path / "source", digest) == done
        assert source.get("OWNERSHIP", family) == c.sr
        assert target.get("OWNERSHIP", family) == c.ta


@pytest.mark.parametrize("after", [False, True])
def test_lost_activation_response_never_reactivates_source(tmp_path, monkeypatch, after):
    with stores() as (source, target, _, _):
        c = controller(source, target, make_plan(source, target))
        c.freeze()
        digest = capture(source, "cases", tmp_path / "source")
        c.stage(tmp_path / "source", digest)
        real = target.transaction

        def fail(actions):
            if any(a.get("Put", {}).get("Item") == c.ta for a in actions):
                if after:
                    real(actions)
                raise OSError("activation response lost")
            return real(actions)

        monkeypatch.setattr(target, "transaction", fail)
        with pytest.raises(OSError):
            c.activate(tmp_path / "source", digest)
        assert source.get("OWNERSHIP", "cases") == c.sr
        assert c.journal()["phase"] == "source_retired"
        monkeypatch.setattr(target, "transaction", real)
        if after:
            # A new target write must survive activation reconciliation.
            target.ddb.put_item(
                TableName=target.table,
                Item=t.wire({"pk": "REQUEST#new", "sk": "new", "result": {}}),
            )
        assert c.activate(tmp_path / "source", digest)["phase"] == "complete"
        if after:
            assert target.get("REQUEST#new", "new")


def test_partial_import_and_restart_preserve_rows_and_objects(tmp_path, monkeypatch):
    with stores() as (source, target, _, _):
        c = controller(source, target, make_plan(source, target))
        c.freeze()
        digest = capture(source, "cases", tmp_path / "source")
        real = target.transaction
        calls = []

        def fail(actions):
            real(actions)
            calls.append(actions)
            if len(calls) == 1:
                raise OSError("lost import response")

        monkeypatch.setattr(target, "transaction", fail)
        with pytest.raises(OSError):
            c.stage(tmp_path / "source", digest)
        assert c.journal()["phase"] == "staging" and target.rows("cases")
        with pytest.raises(FleetManagerError, match="finish the transfer"):
            c.cancel_before_import()
        monkeypatch.setattr(target, "transaction", real)
        assert c.stage(tmp_path / "source", digest)["destination_verified"]
        assert c.activate(tmp_path / "source", digest)["phase"] == "complete"


def test_reverse_carries_new_write_and_closed_case_link_removal(tmp_path):
    with stores() as (source, target, _, _):
        p = make_plan(source, target)
        c = controller(source, target, p)
        c.freeze()
        digest = capture(source, "cases", tmp_path / "source")
        c.stage(tmp_path / "source", digest)
        done = c.activate(tmp_path / "source", digest)
        cid = "a" * 32
        head = t.plain(t.decoded(target.get("CASE", cid)))
        head.update(rev=3, status="closed", description="destination operator work")
        target.ddb.put_item(TableName=target.table, Item=t.wire(head))
        target.ddb.put_item(
            TableName=target.table,
            Item=t.wire(
                {
                    "pk": "AUDIT#" + cid,
                    "sk": f"{3:012d}",
                    "case_id": cid,
                    "rev": 3,
                    "actor": "destination-operator",
                }
            ),
        )
        for row in target.rows("cases"):
            if t.key(row)[0].startswith("LINK#"):
                target.ddb.delete_item(
                    TableName=target.table, Key={k: row[k] for k in ["pk", "sk"]}
                )
        previous = {"plan": p, "completion": done}
        rp = make_plan(target, source, previous=previous)
        r = controller(target, source, rp)
        r.freeze()
        rd = capture(target, "cases", tmp_path / "reverse")
        r.stage(tmp_path / "reverse", rd)
        assert r.activate(tmp_path / "reverse", rd)["phase"] == "complete"
        recovered = t.decoded(source.get("CASE", cid))
        assert recovered["rev"] == 3
        assert recovered["description"] == "destination operator work"
        assert not any(t.key(row)[0].startswith("LINK#") for row in source.rows("cases"))
        assert source.get("AUDIT#" + cid, f"{3:012d}")["actor"]["S"] == "destination-operator"
        assert target.get("OWNERSHIP", "cases") == r.sr and source.get("OWNERSHIP", "cases") == r.ta
        # Replaying old completion cannot reactivate the retired destination.
        assert c.activate(tmp_path / "source", digest) == done
        assert target.get("OWNERSHIP", "cases") == r.sr


def test_preimport_cancellation_increments_epochs_and_blocks_late_work(tmp_path):
    with stores() as (source, target, _, _):
        c = controller(source, target, make_plan(source, target))
        c.freeze()
        digest = capture(source, "cases", tmp_path / "source")
        assert c.cancel_before_import()["phase"] == "cancelled"
        assert c.cancel_before_import()["phase"] == "cancelled"
        with pytest.raises(FleetManagerError):
            c.freeze()
        with pytest.raises(FleetManagerError):
            c.stage(tmp_path / "source", digest)
        assert t.decoded(source.get("OWNERSHIP", "cases"))["state"] == "active"
        assert t.decoded(target.get("OWNERSHIP", "cases"))["state"] == "active"
        assert target.rows("cases") == []


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_source",
        "missing_fence",
        "changed_bytes",
        "unexpected_target",
        "object_collision",
        "lost_lease",
    ],
)
def test_fails_closed_without_retiring_source(tmp_path, defect):
    with stores() as (source, target, _, objects):
        c = controller(source, target, make_plan(source, target))
        c.freeze()
        digest = capture(source, "cases", tmp_path / "source")
        if defect == "wrong_source":
            c.source.pin["table_id"] = "changed"
        if defect == "missing_fence":
            source.ddb.delete_item(
                TableName=source.table, Key=t.wire({"pk": "OWNERSHIP", "sk": "cases"})
            )
        if defect == "changed_bytes":
            next((tmp_path / "source").glob("chunk-*")).write_text("[]")
        if defect == "unexpected_target":
            target.ddb.put_item(
                TableName=target.table, Item=t.wire({"pk": "CASE", "sk": "unexpected"})
            )
        if defect == "object_collision":
            target.s3.put_object(Bucket=target.bucket, Key=next(iter(objects)), Body=b"wrong")
        if defect == "lost_lease":

            def deny(pin):
                raise FleetManagerError("Migration lease changed")

            target.guard = deny
        with pytest.raises(FleetManagerError):
            c.stage(tmp_path / "source", digest)
        assert source.get("OWNERSHIP", "cases") != c.sr
        assert target.get("OWNERSHIP", "cases") != c.ta


def test_nonempty_initial_destination_and_changed_retained_source_are_rejected(tmp_path):
    with stores() as (source, target, _, _):
        target.ddb.put_item(TableName=target.table, Item=t.wire({"pk": "CASE", "sk": "existing"}))
        with pytest.raises(FleetManagerError, match="empty"):
            make_plan(source, target)


def test_forged_completed_transfer_cannot_authorize_reverse(tmp_path):
    with stores() as (source, target, _, _):
        p = make_plan(source, target)
        c = controller(source, target, p)
        c.freeze()
        d = capture(source, "cases", tmp_path / "s")
        c.stage(tmp_path / "s", d)
        done = c.activate(tmp_path / "s", d)
        forged = copy.deepcopy(done)
        forged["material_sha256"] = "0" * 64
        with pytest.raises(FleetManagerError, match="authoritative"):
            make_plan(target, source, previous={"plan": p, "completion": forged})


def test_cancelled_reverse_can_be_reviewed_again_without_rewinding_epochs(tmp_path):
    with stores() as (source, target, _, _):
        p = make_plan(source, target)
        c = controller(source, target, p)
        c.freeze()
        d = capture(source, "cases", tmp_path / "source")
        c.stage(tmp_path / "source", d)
        done = c.activate(tmp_path / "source", d)
        previous = {"plan": p, "completion": done}
        rp = make_plan(target, source, previous=previous)
        reverse = controller(target, source, rp)
        reverse.freeze()
        reverse.cancel_before_import()
        rp2 = make_plan(target, source, previous=previous)
        assert rp2["source_revision"] > rp["source_revision"]
        again = controller(target, source, rp2)
        assert again.freeze()["phase"] == "frozen"


def test_target_write_before_freeze_is_preserved_by_preimport_cancellation(tmp_path, monkeypatch):
    with stores() as (source, target, _, _):
        c = controller(source, target, make_plan(source, target))
        real = target.transaction

        def race(actions):
            target.ddb.put_item(TableName=target.table, Item=t.wire({"pk": "CASE", "sk": "late"}))
            return real(actions)

        monkeypatch.setattr(target, "transaction", race)
        with pytest.raises(FleetManagerError, match="changed before its fence"):
            c.freeze()
        monkeypatch.setattr(target, "transaction", real)
        assert c.cancel_before_import()["phase"] == "cancelled"
        assert target.get("CASE", "late")
        assert t.decoded(source.get("OWNERSHIP", "cases"))["state"] == "active"


def test_large_case_import_batches_respect_transaction_byte_limit(tmp_path, monkeypatch):
    with stores() as (source, target, _, _):
        for i in range(30):
            cid = f"{i + 1:032x}"
            source.ddb.put_item(
                TableName=source.table,
                Item=t.wire(
                    {
                        "pk": "CASE",
                        "sk": cid,
                        "case_id": cid,
                        "rev": 1,
                        "agent_ids": ["111111111111:us-east-1:fixture"],
                        "status": "open",
                        "attachments": [],
                        "description": "x" * 140000,
                    }
                ),
            )
            source.ddb.put_item(
                TableName=source.table,
                Item=t.wire(
                    {
                        "pk": "AUDIT#" + cid,
                        "sk": f"{1:012d}",
                        "case_id": cid,
                        "rev": 1,
                        "actor": "fixture",
                    }
                ),
            )
        c = controller(source, target, make_plan(source, target))
        c.freeze()
        # The fixture capture helper deliberately pages all rows in this large test.
        items = []
        cursor = None
        while True:
            page = source.ddb.scan(
                TableName=source.table, **({"ExclusiveStartKey": cursor} if cursor else {})
            )
            items.extend(page["Items"])
            cursor = page.get("LastEvaluatedKey")
            if not cursor:
                break
        objects = {}
        for obj in source.s3.list_objects_v2(Bucket=source.bucket).get("Contents", []):
            objects[obj["Key"]] = source.s3.get_object(Bucket=source.bucket, Key=obj["Key"])[
                "Body"
            ].read()
        digest = archive.export(
            case_archive.native_source(source.pin, items, objects), tmp_path / "large"
        )["manifest_sha256"]
        sizes = []
        real = target.transaction

        def counted(actions):
            sizes.append(len(t.canonical(actions)))
            return real(actions)

        monkeypatch.setattr(target, "transaction", counted)
        assert c.stage(tmp_path / "large", digest)["destination_verified"]
        assert len(sizes) > 2 and max(sizes) < 4_000_000
