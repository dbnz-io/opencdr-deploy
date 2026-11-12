import copy

import pytest
from boto3.dynamodb.types import TypeSerializer

from opencdr_deploy import library_archive as a
from opencdr_deploy import migration_archive as m
from opencdr_deploy.fleet_api import FleetManagerError


def wire(row):
    return {k: TypeSerializer().serialize(v) for k, v in row.items()}


def fixture():
    rows, objects = [], {}
    content, release, compilation = "a" * 32, "b" * 32, "c" * 32

    def artifact(row, body, key):
        raw = m.canonical(body)
        objects[key] = raw
        rows.append({**row, "artifact_key": key, "artifact_sha256": m.sha(raw)})
        return rows[-1]

    rows += [
        {
            "pk": "ITEM",
            "sk": content,
            "content_id": content,
            "kind": "list",
            "runtime_id": "retained-list",
            "latest_version": 1,
            "created_by": "historical-unknown",
        },
        {"pk": "IDENTITY#list", "sk": "retained-list", "content_id": content},
    ]
    document = {"list_id": "retained-list", "entries": ["retained"]}
    version = {
        "content_id": content,
        "version": 1,
        "kind": "list",
        "runtime_id": "retained-list",
        "dependencies": [],
        "provenance": {"kind": "unknown"},
    }
    saved = artifact(
        {
            "pk": "VERSION#" + content,
            "sk": f"{1:016d}",
            **version,
            "document_sha256": m.sha(m.canonical(document)),
        },
        {**version, "document": document},
        "library/versions/one.json",
    )
    ref = {"content_id": content, "version": 1, "artifact_sha256": saved["artifact_sha256"]}
    rel = artifact(
        {"pk": "RELEASE", "sk": release, "release_id": release},
        {"release_id": release, "roots": [ref], "items": [ref]},
        "library/releases/one.json",
    )
    artifact(
        {"pk": "TEST#" + content, "sk": "d" * 32, "reference": ref},
        {"reference": ref, "dependencies": [ref]},
        "library/quality/test.json",
    )
    compiled = artifact(
        {"pk": "COMPILE#" + release, "sk": compilation, "release_id": release},
        {"release_id": release, "source_artifact_sha256": rel["artifact_sha256"]},
        "library/quality/compile.json",
    )
    artifact(
        {
            "pk": "MAPPING#" + release,
            "sk": "e" * 32,
            "release_id": release,
            "compilation_id": compilation,
        },
        {
            "release_id": release,
            "compilation_id": compilation,
            "compilation_sha256": compiled["artifact_sha256"],
        },
        "library/quality/mapping.json",
    )
    rows += [
        {"pk": "AUDIT#" + content, "sk": "0001", "actor": "old-actor"},
        {"pk": "REQUEST#old-actor", "sk": "receipt", "result": {"historical": True}},
    ]
    return rows, objects


def pack(rows, objects):
    return a.native_source({"owner": "fixture-only"}, [wire(r) for r in rows], objects)


def test_native_graph_retains_raw_history_and_round_trips_inert(tmp_path):
    rows, objects = fixture()
    source = pack(rows, objects)
    result = m.export(source, tmp_path / "archive")
    manifest, records = m.verify(tmp_path / "archive", result["manifest_sha256"])
    assert not manifest["source"]["writer_fence_attested"]
    assert manifest["source_consistency"] == "not_attested_by_archive"
    assert len(records) == len(rows) + len(objects)
    native = [r["payload"]["item"] for r in records if r["family"] == "library"]
    assert sorted(native, key=m.canonical) == sorted([wire(r) for r in rows], key=m.canonical)
    assert (
        m.stage(tmp_path / "archive", result["manifest_sha256"], tmp_path / "db")["activated"]
        is False
    )


@pytest.mark.parametrize(
    "defect",
    [
        "missing_version",
        "wrong_hash",
        "missing_object",
        "bad_object",
        "identity_collision",
        "missing_compile",
    ],
)
def test_broken_native_relationship_or_bytes_rejected(defect):
    rows, objects = fixture()
    if defect == "missing_version":
        rows = [r for r in rows if not r["pk"].startswith("VERSION#")]
    elif defect == "wrong_hash":
        row = next(r for r in rows if r["pk"].startswith("TEST#"))
        row["reference"]["artifact_sha256"] = "0" * 64
    elif defect == "missing_object":
        objects.pop(next(iter(objects)))
    elif defect == "bad_object":
        objects[next(iter(objects))] = b"{}"
    elif defect == "identity_collision":
        rows.append(copy.deepcopy(rows[0]))
    else:
        rows = [r for r in rows if not r["pk"].startswith("COMPILE#")]
    with pytest.raises(FleetManagerError):
        pack(rows, objects)


def test_unknown_partition_preserved_as_unresolved_not_authority():
    rows, objects = fixture()
    unknown = {"pk": "FUTURE_AUTHORITY", "sk": "row", "enabled": True}
    ownership = {"pk": "OWNERSHIP", "sk": "STATE", "state": "frozen", "revision": 2}
    source = pack([*rows, unknown, ownership], objects)
    assert source["unresolved"] == [
        {"reason": "unknown_native_library_partition", "item": wire(unknown)}
    ]
    assert source["source"]["ownership_observation"] == wire(ownership)
    assert all(r["payload"].get("item") != wire(ownership) for r in source["records"])
