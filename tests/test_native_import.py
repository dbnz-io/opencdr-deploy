
import pytest
from test_case_archive import fixture, pack
from test_library_archive import fixture as library_fixture
from test_library_archive import pack as library_pack

from opencdr_deploy import case_archive as c
from opencdr_deploy import migration_archive as m
from opencdr_deploy import native_import as n
from opencdr_deploy.fleet_api import FleetManagerError


def preview(tmp_path):
    rows, objects = fixture()
    source = pack(rows, objects)
    archive = tmp_path / "source"
    result = m.export(source, archive)
    mapping = {rows[0]["agent_ids"][0]: "member-1"}
    return (
        source,
        archive,
        result["manifest_sha256"],
        mapping,
        n.prepare(archive, result["manifest_sha256"], mapping),
    )


def test_case_mapping_keeps_audit_actors_and_evidence_and_never_grants_authority(tmp_path):
    source, archive, digest, mapping, plan = preview(tmp_path)
    assert plan == n.prepare(archive, digest, mapping)
    rows = [n.decoded(r["item"]) for r in plan["rows"]]
    head = next(r for r in rows if r["pk"] == "CASE")
    link = next(r for r in rows if r["pk"].startswith("LINK#"))
    assert head["agent_ids"] == ["member-1"]
    attachment = head["attachments"][0]
    assert attachment["agent_id"] == "member-1"
    assert link["pk"] == "LINK#" + attachment["attachment_id"]
    assert (
        next(r for r in rows if r["pk"].startswith("EXTENSION#"))["source"]["attachment_id"]
        == attachment["attachment_id"]
    )
    original_audit = [
        r["payload"]["item"]
        for r in source["records"]
        if r["family"] == "history" and r["payload"]["item"]["pk"]["S"].startswith("AUDIT#")
    ]
    mapped_audit = [r["item"] for r in plan["rows"] if r["item"]["pk"]["S"].startswith("AUDIT#")]
    assert original_audit == mapped_audit
    assert len(plan["retained_inert_records"]) == 2
    assert not any(r["pk"].startswith("REVIEW") for r in rows)
    assert plan["activation"] == "forbidden" and not plan["destination_verified"]


def test_round_trip_preserves_post_transfer_case_write(tmp_path):
    _, _, _, mapping, forward = preview(tmp_path)
    objects = {o["key"]: n.base64.b64decode(o["body_base64"]) for o in forward["objects"]}
    rows = [n.decoded(r["item"]) for r in forward["rows"]]
    head = next(r for r in rows if r["pk"] == "CASE")
    head.update(rev=3, description="written after transfer", updated_by="destination-actor")
    rows.append(
        {
            "pk": "AUDIT#" + head["case_id"],
            "sk": f"{3:012d}",
            "case_id": head["case_id"],
            "rev": 3,
            "actor": "destination-actor",
            "detail": {"text": "retained new work"},
        }
    )
    target_source = c.native_source(
        {"owner": "destination-fixture"}, [n.wire(r) for r in rows], objects
    )
    archive = tmp_path / "reverse"
    result = m.export(target_source, archive)
    reverse = n.prepare(archive, result["manifest_sha256"], {v: k for k, v in mapping.items()})
    restored = [n.decoded(r["item"]) for r in reverse["rows"]]
    recovered = next(r for r in restored if r["pk"] == "CASE")
    assert recovered["rev"] == 3 and recovered["description"] == "written after transfer"
    assert recovered["agent_ids"] == list(mapping)
    assert any(r.get("actor") == "destination-actor" for r in restored)
    assert reverse["objects"] == forward["objects"]


@pytest.mark.parametrize(
    "mapping",
    [
        {},
        {"111111111111:us-east-1:fixture": "bad scope"},
        {"111111111111:us-east-1:fixture": "member", "extra": "member"},
    ],
)
def test_ambiguous_or_incomplete_scope_mapping_rejected(tmp_path, mapping):
    _, archive, digest, _, _ = preview(tmp_path)
    with pytest.raises(FleetManagerError):
        n.prepare(archive, digest, mapping)


def test_pinned_but_semantically_forged_native_archive_rejected(tmp_path):
    rows, objects = fixture()
    source = pack(rows, objects)
    source["records"][0]["references"] = []
    source["records"][0]["payload"]["item"]["case_id"] = {"S": "f" * 32}
    archive = tmp_path / "forged"
    result = m.export(source, archive)
    with pytest.raises(FleetManagerError):
        n.prepare(archive, result["manifest_sha256"], {rows[0]["agent_ids"][0]: "member"})


def test_library_preview_preserves_versions_and_runtime_ids(tmp_path):
    rows, objects = library_fixture()
    source = library_pack(rows, objects)
    result = m.export(source, tmp_path / "library")
    prepared = n.prepare(tmp_path / "library", result["manifest_sha256"], {})
    assert len(prepared["rows"]) == len(rows)
    assert len(prepared["objects"]) == len(objects)
    assert all(r["source_sha256"] == r["target_sha256"] for r in prepared["rows"])
    assert not prepared["retained_inert_records"]
