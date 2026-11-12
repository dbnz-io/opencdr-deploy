import copy

import pytest
from boto3.dynamodb.types import TypeSerializer

from opencdr_deploy import case_archive as a
from opencdr_deploy import migration_archive as m
from opencdr_deploy.fleet_api import FleetManagerError


def fixture():
    cid, agent = "a" * 32, "111111111111:us-east-1:fixture"
    aid = m.sha(m.canonical({"agent_id": agent, "detection_id": "retained-detection"}))
    raw = m.canonical(
        {"detection_id": "retained-detection", "actor": None, "provenance": "unknown"}
    )
    key = "cases/" + cid + "/" + aid + "/" + m.sha(raw) + ".json"
    head = {
        "pk": "CASE",
        "sk": cid,
        "case_id": cid,
        "agent_ids": [agent],
        "rev": 2,
        "status": "open",
        "created_by": "unknown-historical-actor",
        "attachments": [
            {
                "attachment_id": aid,
                "agent_id": agent,
                "detection_id": "retained-detection",
                "attached": True,
                "evidence_key": key,
                "evidence_sha256": m.sha(raw),
            }
        ],
    }
    rows = [
        head,
        {"pk": "AUDIT#" + cid, "sk": f"{1:012d}", "case_id": cid, "rev": 1, "actor": None},
        {"pk": "AUDIT#" + cid, "sk": f"{2:012d}", "case_id": cid, "rev": 2, "actor": "historical"},
        {"pk": "LINK#" + aid, "sk": "ACTIVE", "case_id": cid},
        {"pk": "REQUEST#" + cid, "sk": "retry", "result": {"case_id": cid, "rev": 1}},
        {
            "pk": "EXTENSION#" + cid + "#indicators",
            "sk": "indicator",
            "case_id": cid,
            "source": {"kind": "attachment", "attachment_id": aid},
        },
        {
            "pk": "REVIEW_EXECUTION#old-owner",
            "sk": "META",
            "state": "sending",
            "dispatch_attempted": True,
        },
        {"pk": "REVIEW_POLICY", "sk": "old-owner", "enabled": True},
        {"pk": "OWNERSHIP", "sk": "cases", "state": "frozen", "revision": 2},
    ]
    return rows, {key: raw}


def pack(rows, objects):
    return a.native_source(
        {"owner": "fixture-only"},
        [{k: TypeSerializer().serialize(v) for k, v in row.items()} for row in rows],
        objects,
    )


def test_case_history_evidence_and_sending_jobs_remain_inert(tmp_path):
    rows, objects = fixture()
    source = pack(rows, objects)
    assert source["source"]["worker_replay"] == "forbidden"
    assert source["source"]["authority_import"] == "forbidden"
    assert not source["source"]["writer_fence_attested"]
    assert not source["unresolved"]
    result = m.export(source, tmp_path / "archive")
    manifest, records = m.verify(tmp_path / "archive", result["manifest_sha256"])
    assert len(records) == len(rows)
    assert any(
        r["family"] == "job" and r["payload"]["item"]["state"]["S"] == "sending" for r in records
    )
    assert all(not r["payload"].get("execution_authorized", False) for r in records)
    assert (
        m.stage(tmp_path / "archive", result["manifest_sha256"], tmp_path / "db")["activated"]
        is False
    )


@pytest.mark.parametrize(
    "defect",
    [
        "audit_gap",
        "lock_missing",
        "orphan_lock",
        "double_owner",
        "wrong_evidence",
        "missing_related",
        "bad_extension",
    ],
)
def test_inconsistent_case_snapshot_fails_closed(defect):
    rows, objects = fixture()
    if defect == "audit_gap":
        rows.pop(1)
    elif defect == "lock_missing":
        rows = [r for r in rows if not r["pk"].startswith("LINK#")]
    elif defect == "orphan_lock":
        rows[0]["status"] = "closed"
    elif defect == "double_owner":
        clone = copy.deepcopy(rows[0])
        clone.update(sk="b" * 32, case_id="b" * 32)
        rows.append(clone)
        rows += [{"pk": "AUDIT#" + clone["case_id"], "sk": f"{n:012d}", "rev": n} for n in (1, 2)]
    elif defect == "wrong_evidence":
        objects[next(iter(objects))] = b"{}"
    elif defect == "missing_related":
        rows[0]["duplicate_of"] = "f" * 32
    else:
        rows[5]["source"]["attachment_id"] = "f" * 64
    with pytest.raises(FleetManagerError):
        pack(rows, objects)


def test_unrecognized_rows_are_preserved_as_unresolved():
    rows, objects = fixture()
    rows.append({"pk": "FUTURE_EXECUTOR", "sk": "state", "enabled": True})
    source = pack(rows, objects)
    assert len(source["unresolved"]) == 1
    assert source["unresolved"][0]["item"]["enabled"] == {"BOOL": True}
