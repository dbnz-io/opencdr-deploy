import copy
import json
import sqlite3

import pytest

from opencdr_deploy import migration_archive as m
from opencdr_deploy.fleet_api import FleetManagerError


def source(count=205):
    return {
        "source": {"owner": "core:123456789012:us-east-1:test", "snapshot": "fixture-only"},
        "identity_map": {"core-agent": "fleet-member"},
        "unresolved": [{"legacy_actor": None, "reason": "historical actor unavailable"}],
        "records": [
            {
                "family": "case",
                "id": str(i),
                "payload": {"actor": "unknown", "rule_provenance": None, "enabled": True},
                "references": [] if i == 0 else [{"family": "case", "id": "0"}],
            }
            for i in range(count)
        ],
    }


def test_export_resume_and_inert_stage_keeps_historical_fields(tmp_path):
    src = source()
    result = m.export(src, tmp_path / "archive")
    assert m.export(src, tmp_path / "archive") == result
    manifest, rows = m.verify(tmp_path / "archive", result["manifest_sha256"])
    assert manifest["source_consistency"] == "not_attested_by_archive"
    assert rows[0]["payload"]["rule_provenance"] is None
    db = tmp_path / "staged.sqlite"
    assert m.stage(tmp_path / "archive", result["manifest_sha256"], db)["status"] == "staged_inert"
    assert not m.stage(tmp_path / "archive", result["manifest_sha256"], db)["activated"]
    with sqlite3.connect(db) as c:
        assert c.execute("select count(*) from migration_records").fetchone()[0] == 205
        assert c.execute("select count(*) from migration_chunks").fetchone()[0] == 3
    assert manifest["unresolved"] == src["unresolved"]


def test_tampered_chunk_prevents_any_database_creation(tmp_path):
    result = m.export(source(), tmp_path / "a")
    chunk = next((tmp_path / "a").glob("chunk-*.json"))
    chunk.write_text("[]")
    with pytest.raises(FleetManagerError, match="chunk SHA256"):
        m.stage(tmp_path / "a", result["manifest_sha256"], tmp_path / "db")
    assert not (tmp_path / "db").exists()


def test_changed_source_cannot_reuse_export_or_staging(tmp_path):
    original = source(1)
    first = m.export(original, tmp_path / "a")
    m.stage(tmp_path / "a", first["manifest_sha256"], tmp_path / "db")
    changed = copy.deepcopy(original)
    changed["records"][0]["payload"]["actor"] = "different"
    with pytest.raises(FleetManagerError, match="differs"):
        m.export(changed, tmp_path / "a")
    second = m.export(changed, tmp_path / "b")
    with pytest.raises(FleetManagerError, match="different archive"):
        m.stage(tmp_path / "b", second["manifest_sha256"], tmp_path / "db")


def test_partial_chunk_transaction_rolls_back_and_resume_rechecks_rows(tmp_path, monkeypatch):
    original = source()
    first = m.export(original, tmp_path / "a")
    real = m.canonical

    def fail(value):
        if isinstance(value, dict) and value.get("id") == "150":
            raise OSError("simulated interruption")
        return real(value)

    monkeypatch.setattr(m, "canonical", fail)
    with pytest.raises(OSError):
        m.stage(tmp_path / "a", first["manifest_sha256"], tmp_path / "db")
    with sqlite3.connect(tmp_path / "db") as c:
        assert c.execute("select count(*) from migration_records").fetchone()[0] == 0
    monkeypatch.setattr(m, "canonical", real)
    assert m.stage(tmp_path / "a", first["manifest_sha256"], tmp_path / "db")["records"] == 205
    with sqlite3.connect(tmp_path / "db") as c:
        c.execute("update migration_records set payload='changed' where identity='0'")
    with pytest.raises(FleetManagerError, match="record was changed"):
        m.stage(tmp_path / "a", first["manifest_sha256"], tmp_path / "db")


@pytest.mark.parametrize("problem", ["duplicate", "reference", "mapping"])
def test_ambiguous_identity_and_dangling_reference_rejected(tmp_path, problem):
    value = source(1)
    if problem == "duplicate":
        value["records"].append(copy.deepcopy(value["records"][0]))
    elif problem == "reference":
        value["records"][0]["references"] = [{"family": "case", "id": "missing"}]
    else:
        value["identity_map"]["another"] = "fleet-member"
    with pytest.raises(FleetManagerError):
        m.export(value, tmp_path / "a")
    assert not (tmp_path / "a").exists()


def test_paths_hash_and_schema_are_fail_closed(tmp_path):
    result = m.export(source(1), tmp_path / "a")
    with pytest.raises(FleetManagerError, match="manifest SHA256"):
        m.verify(tmp_path / "a", "0" * 64)
    path = tmp_path / "a/manifest.json"
    doc = json.loads(path.read_bytes())
    doc["chunks"][0]["file"] = "../escape"
    raw = m.canonical(doc)
    path.write_bytes(raw)
    with pytest.raises(FleetManagerError, match="chunk path"):
        m.verify(tmp_path / "a", m.sha(raw))
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "a", target_is_directory=True)
    with pytest.raises(FleetManagerError, match="symlink"):
        m.verify(link, result["manifest_sha256"])


def test_interrupted_export_publishes_no_partial_chunk_and_resumes(tmp_path, monkeypatch):
    real = m.os.link

    def interrupt(src, dst):
        if dst.name.startswith("chunk-"):
            raise OSError("interrupted before publication")
        return real(src, dst)

    monkeypatch.setattr(m.os, "link", interrupt)
    with pytest.raises(OSError):
        m.export(source(), tmp_path / "a")
    assert not list((tmp_path / "a").glob("chunk-*"))
    assert not (tmp_path / "a/manifest.json").exists()
    monkeypatch.setattr(m.os, "link", real)
    result = m.export(source(), tmp_path / "a")
    assert len(m.verify(tmp_path / "a", result["manifest_sha256"])[1]) == 205


def test_total_archive_bytes_are_bounded(tmp_path, monkeypatch):
    result = m.export(source(), tmp_path / "a")
    largest = max(p.stat().st_size for p in (tmp_path / "a").glob("*.json"))
    monkeypatch.setattr(m, "MAX_BYTES", largest + 1)
    with pytest.raises(FleetManagerError, match="total byte bound"):
        m.verify(tmp_path / "a", result["manifest_sha256"])
