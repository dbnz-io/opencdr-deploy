"""Portable, checksummed migration staging. No AWS writes or ownership activation.

The archive retains source identities, historical actors and unknown provenance verbatim.
A staged archive is not a live import or proof of a fenced source snapshot.
"""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from .fleet_api import FleetManagerError

SCHEMA = "opencdr.migration-archive/1"
MAX_BYTES = 64 * 1024 * 1024
CHUNK_SIZE = 100
MAX_RECORDS = 100_000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def require(test, message):
    if not test:
        raise FleetManagerError(message)


def read(path):
    require(not path.is_symlink() and path.is_file(), "Migration input must be a regular file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "Migration file exceeds its byte bound")
    return raw


def validate(source):
    require(isinstance(source, dict), "Invalid migration source")
    require(
        set(source) == {"source", "identity_map", "records", "unresolved"}, "Invalid source fields"
    )
    require(
        isinstance(source["source"], dict) and bool(source["source"]),
        "Explicit source identity required",
    )
    identities = source["identity_map"]
    require(isinstance(identities, dict), "Explicit identity map required")
    require(
        all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in identities.items()),
        "Invalid identity map",
    )
    require(len(set(identities.values())) == len(identities), "Identity mapping collision")
    require(
        isinstance(source["unresolved"], list), "Unresolved records must be retained explicitly"
    )
    records = source["records"]
    require(
        isinstance(records, list) and len(records) <= MAX_RECORDS, "Migration record bound exceeded"
    )
    keys = set()
    for row in records:
        require(
            isinstance(row, dict) and set(row) == {"family", "id", "payload", "references"},
            "Invalid archive record",
        )
        require(
            row["family"] in {"case", "library", "history", "policy", "job", "evidence"},
            "Unknown record family",
        )
        require(
            isinstance(row["id"], str) and 0 < len(row["id"]) <= 1024, "Invalid record identity"
        )
        require(isinstance(row["payload"], dict), "Record payload must retain its source object")
        key = (row["family"], row["id"])
        require(key not in keys, "Duplicate source identity")
        keys.add(key)
        refs = row["references"]
        require(isinstance(refs, list), "References must be explicit")
        for ref in refs:
            require(isinstance(ref, dict) and set(ref) == {"family", "id"}, "Invalid reference")
            require(
                isinstance(ref["family"], str) and isinstance(ref["id"], str),
                "Invalid reference identity",
            )
    missing = []
    for row in records:
        for ref in row["references"]:
            if (ref["family"], ref["id"]) not in keys:
                missing.append({"from": {"family": row["family"], "id": row["id"]}, "to": ref})
    require(
        not missing,
        "Dangling references; retain unresolved data in the explicit unresolved collection",
    )
    return source


def exclusive_or_identical(path, raw):
    require(not path.is_symlink(), "Migration symlinks are not permitted")
    if path.exists():
        require(read(path) == raw, "Existing archive content differs; preserve it and reconcile")
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".migration-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                require(read(path) == raw, "Concurrent archive content differs")
        finally:
            temporary.unlink(missing_ok=True)


def export(source, directory):
    validate(source)
    require(len(canonical(source)) <= MAX_BYTES, "Migration source exceeds total byte bound")
    require(not directory.is_symlink(), "Archive directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    source = {**source, "records": sorted(source["records"], key=lambda x: (x["family"], x["id"]))}
    source_hash = sha(canonical(source))
    exclusive_or_identical(
        directory / "intent.json", canonical({"schema": SCHEMA, "source_sha256": source_hash})
    )
    chunks = []
    records = sorted(source["records"], key=lambda x: (x["family"], x["id"]))
    for offset in range(0, len(records), CHUNK_SIZE):
        raw = canonical(records[offset : offset + CHUNK_SIZE])
        digest = sha(raw)
        filename = f"chunk-{offset // CHUNK_SIZE:06d}-{digest}.json"
        exclusive_or_identical(directory / filename, raw)
        chunks.append(
            {"file": filename, "sha256": digest, "records": min(CHUNK_SIZE, len(records) - offset)}
        )
    manifest = {
        "schema": SCHEMA,
        "source": source["source"],
        "source_sha256": source_hash,
        "identity_map": source["identity_map"],
        "unresolved": source["unresolved"],
        "chunks": chunks,
        "records": len(records),
        "activation": "forbidden",
        "source_consistency": "not_attested_by_archive",
    }
    raw = canonical(manifest)
    exclusive_or_identical(directory / "manifest.json", raw)
    return {
        "manifest_sha256": sha(raw),
        "records": len(records),
        "unresolved": len(source["unresolved"]),
        "activated": False,
    }


def verify(directory, expected):
    require(not directory.is_symlink(), "Archive directory must not be a symlink")
    require(
        isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected),
        "Exact manifest SHA256 required",
    )
    raw = read(directory / "manifest.json")
    require(sha(raw) == expected, "Migration manifest SHA256 mismatch")
    manifest = json.loads(raw)
    require(
        manifest.get("schema") == SCHEMA and manifest.get("activation") == "forbidden",
        "Unsupported migration schema or activation",
    )
    chunks = manifest["chunks"]
    require(
        isinstance(chunks, list) and len(chunks) <= MAX_RECORDS // CHUNK_SIZE, "Invalid chunk bound"
    )
    records = []
    total_bytes = len(raw)
    for index, chunk in enumerate(chunks):
        digest = chunk["sha256"]
        require(
            isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest),
            "Invalid chunk digest",
        )
        require(
            chunk["file"] == f"chunk-{index:06d}-{digest}.json", "Invalid chunk path or sequence"
        )
        data = read(directory / chunk["file"])
        total_bytes += len(data)
        require(total_bytes <= MAX_BYTES, "Migration archive exceeds total byte bound")
        require(sha(data) == digest, "Migration chunk SHA256 mismatch")
        rows = json.loads(data)
        require(
            isinstance(rows, list)
            and len(rows) == chunk["records"]
            and 1 <= len(rows) <= CHUNK_SIZE,
            "Migration chunk count mismatch",
        )
        require(
            index == len(chunks) - 1 or len(rows) == CHUNK_SIZE, "Nonterminal chunk is incomplete"
        )
        records.extend(rows)
    require(len(records) == manifest["records"], "Migration record count mismatch")
    source = {
        "source": manifest["source"],
        "identity_map": manifest["identity_map"],
        "unresolved": manifest["unresolved"],
        "records": records,
    }
    validate(source)
    require(sha(canonical(source)) == manifest["source_sha256"], "Source content digest mismatch")
    return manifest, records


def stage(directory, expected, database):
    manifest, records = verify(directory, expected)  # Verify all bytes before staging any record.
    require(not database.is_symlink(), "Staging database must not be a symlink")
    db = sqlite3.connect(database)
    try:
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS migration_meta "
                "(id INTEGER PRIMARY KEY CHECK(id=1), manifest TEXT NOT NULL, "
                "payload BLOB NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS migration_records "
                "(family TEXT NOT NULL, identity TEXT NOT NULL, payload BLOB NOT NULL, "
                "PRIMARY KEY(family,identity))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS migration_chunks "
                "(number INTEGER PRIMARY KEY, sha256 TEXT NOT NULL)"
            )
            old = db.execute("SELECT manifest,payload FROM migration_meta WHERE id=1").fetchone()
            require(
                old is None or old == (expected, canonical(manifest)),
                "Staging database belongs to a different archive",
            )
            if old is None:
                require(
                    db.execute("SELECT count(*) FROM migration_records").fetchone()[0] == 0,
                    "Unowned staging data exists",
                )
                db.execute(
                    "INSERT INTO migration_meta VALUES (1,?,?)", (expected, canonical(manifest))
                )
        for number, chunk in enumerate(manifest["chunks"]):
            selected = records[number * CHUNK_SIZE : number * CHUNK_SIZE + chunk["records"]]
            with db:
                for row in selected:
                    key = (row["family"], row["id"])
                    raw = canonical(row)
                    old = db.execute(
                        "SELECT payload FROM migration_records WHERE family=? AND identity=?", key
                    ).fetchone()
                    require(old is None or old[0] == raw, "Staged record was changed")
                    if old is None:
                        db.execute("INSERT INTO migration_records VALUES (?,?,?)", (*key, raw))
                old = db.execute(
                    "SELECT sha256 FROM migration_chunks WHERE number=?", (number,)
                ).fetchone()
                require(old is None or old[0] == chunk["sha256"], "Staged checkpoint was changed")
                if old is None:
                    db.execute(
                        "INSERT INTO migration_chunks VALUES (?,?)", (number, chunk["sha256"])
                    )
        require(
            db.execute("SELECT count(*) FROM migration_records").fetchone()[0] == len(records),
            "Unexpected staging records",
        )
        return {
            "manifest_sha256": expected,
            "records": len(records),
            "unresolved": len(manifest["unresolved"]),
            "activated": False,
            "status": "staged_inert",
        }
    finally:
        db.close()


def configure(commands):
    root = commands.add_parser(
        "migration-archive", help="portable inert migration archives; no live cutover"
    )
    actions = root.add_subparsers(dest="archive_action", required=True)
    create = actions.add_parser("export")
    create.add_argument("source", type=Path)
    create.add_argument("directory", type=Path)
    for family in ["library", "cases"]:
        capture = actions.add_parser(
            "capture-" + family, help="read-only native export; no cutover"
        )
        capture.add_argument("directory", type=Path)
        for name in ["profile", "account-id", "region", "stack"]:
            capture.add_argument("--" + name, required=True)
    preview = actions.add_parser("import-preview", help="map native rows locally; no live import")
    preview.add_argument("directory", type=Path)
    preview.add_argument("--sha256", required=True)
    preview.add_argument("--agent-map", type=Path, required=True)
    preview.add_argument("--output", type=Path, required=True)
    for action in ["verify", "stage"]:
        p = actions.add_parser(action)
        p.add_argument("directory", type=Path)
        p.add_argument("--sha256", required=True)
        if action == "stage":
            p.add_argument("--database", type=Path, required=True)


def run(args):
    if args.archive_action in {"capture-library", "capture-cases"}:
        import boto3

        from .library_archive import capture

        result = capture(
            boto3.Session(profile_name=args.profile, region_name=args.region),
            args.account_id,
            args.region,
            args.stack,
            args.directory,
            family=args.archive_action.removeprefix("capture-"),
        )
    elif args.archive_action == "import-preview":
        from .native_import import prepare

        value = prepare(args.directory, args.sha256, json.loads(read(args.agent_map)))
        raw = canonical(value)
        require(len(raw) <= MAX_BYTES, "Import preview exceeds byte limit")
        exclusive_or_identical(args.output, raw)
        result = {
            "preview_sha256": sha(raw),
            "rows": len(value["rows"]),
            "objects": len(value["objects"]),
            "retained_inert_records": len(value["retained_inert_records"]),
            "activated": False,
        }
    elif args.archive_action == "export":
        result = export(json.loads(read(args.source)), args.directory)
    elif args.archive_action == "stage":
        result = stage(args.directory, args.sha256, args.database)
    else:
        manifest, records = verify(args.directory, args.sha256)
        result = {
            "manifest_sha256": args.sha256,
            "records": len(records),
            "unresolved": len(manifest["unresolved"]),
            "activated": False,
        }
    print(json.dumps(result, indent=2))
    return 0
