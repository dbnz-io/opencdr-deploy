"""Resumable physical storage recovery. Capture and restore do not activate data."""

from __future__ import annotations

import json
import re
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from . import storage_quarantine as quarantine
from . import storage_snapshot as snapshot
from .fleet_api import FleetManagerError, aws_client
from .storage_bindings import SCHEMA as BINDING_SCHEMA

SNAPSHOT_SCHEMA = "opencdr.storage-snapshot/1"
RECEIPT_SCHEMA = "opencdr.storage-restore/1"


def _persist(s3, plan, key, document):
    """Immutable metadata; an uncertain successful write is safe to retry."""
    if len(snapshot.canonical(document)) > 2_000_000:
        raise FleetManagerError("Recovery metadata exceeds the supported 2 MB inventory bound")
    try:
        s3.put_object(
            Bucket=plan["artifact_bucket"],
            Key=key,
            Body=snapshot.canonical(document),
            ExpectedBucketOwner=plan["account_id"],
            ServerSideEncryption="AES256",
            IfNoneMatch="*",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
            raise
        observed, _ = quarantine._get(s3, plan["artifact_bucket"], key, plan["account_id"])
        if observed != document:
            raise FleetManagerError("Immutable recovery record has changed") from None
    return document


def capture(session, plan, digest):
    """Freeze/drain first, then copy objects and make independently verified backups.

    Per-table before-fingerprints survive interruption. TTL or out-of-band changes
    fail comparison rather than producing a supposedly coherent mixed snapshot.
    """
    quarantine.require_drained(session, plan, digest)
    ddb, s3 = aws_client(session, "dynamodb"), aws_client(session, "s3")
    prefix = "opencdr-recovery/" + digest
    completed, _ = quarantine._get(
        s3, plan["artifact_bucket"], prefix + "/snapshot.json", plan["account_id"]
    )
    if completed:
        verify_capture(session, plan, completed)
        return {
            "state": "captured",
            "snapshot_sha256": snapshot.sha256(completed),
            "snapshot": completed,
        }
    tables, pending = {}, []
    for logical, source in plan["tables"].items():
        key = prefix + "/tables/" + logical + ".json"
        before, _ = quarantine._get(s3, plan["artifact_bucket"], key, plan["account_id"])
        if before is None:
            before = {
                "source": source,
                "fingerprint": snapshot.table_fingerprint(ddb, source["name"], source["id"]),
            }
            _persist(s3, plan, key, before)
        elif before["source"] != source:
            raise FleetManagerError("Recovery table observation changed")
        backup = snapshot.backup_table(
            ddb, source["name"], source["id"], "opencdr-" + digest[:32] + "-" + logical
        )
        tables[logical] = {**before, "backup": backup}
        if backup["state"] != "AVAILABLE":
            pending.append(logical)
    if pending:
        return {"state": "backups_pending", "pending": pending, "plan_sha256": digest}
    buckets = {}
    for logical, source in plan["buckets"].items():
        key = prefix + "/buckets/" + logical + ".json"
        captured, _ = quarantine._get(s3, plan["artifact_bucket"], key, plan["account_id"])
        if captured is None:
            captured = snapshot.capture_objects(
                s3,
                source["name"],
                plan["artifact_bucket"],
                plan["account_id"],
                prefix=source["prefix"],
                run_prefix=prefix,
            )
            _persist(s3, plan, key, captured)
        buckets[logical] = captured
    document = {
        "schema": SNAPSHOT_SCHEMA,
        "plan_sha256": digest,
        "stack_id": plan["stack_id"],
        "tables": tables,
        "buckets": buckets,
        "history_complete": False,
        "coverage": (
            "retained records and current operational evidence; no external effects rewound"
        ),
    }
    verify_capture(session, plan, document)
    _persist(s3, plan, prefix + "/snapshot.json", document)
    return {"state": "captured", "snapshot_sha256": snapshot.sha256(document), "snapshot": document}


def verify_capture(session, plan, document):
    digest = snapshot.sha256(plan)
    quarantine.require_drained(session, plan, digest)
    if (
        document.get("schema") != SNAPSHOT_SCHEMA
        or document.get("plan_sha256") != digest
        or document.get("stack_id") != plan["stack_id"]
        or set(document.get("tables", {})) != set(plan["tables"])
        or set(document.get("buckets", {})) != set(plan["buckets"])
    ):
        raise FleetManagerError("Snapshot does not cover this reviewed storage set")
    ddb, s3 = aws_client(session, "dynamodb"), aws_client(session, "s3")
    for logical, table in document["tables"].items():
        source = plan["tables"][logical]
        if table["source"] != source:
            raise FleetManagerError("Snapshot source identity changed")
        snapshot.compare_fingerprints(
            table["fingerprint"], snapshot.table_fingerprint(ddb, source["name"], source["id"])
        )
    for logical, bucket in document["buckets"].items():
        if (
            bucket["source_bucket"] != plan["buckets"][logical]["name"]
            or bucket["prefix"] != plan["buckets"][logical]["prefix"]
        ):
            raise FleetManagerError("Snapshot bucket identity changed")
        snapshot.verify_source_objects(s3, bucket, plan["account_id"])
    quarantine.require_drained(session, plan, digest)


def restore(session, plan, document, digest, *, attempt=1):
    if snapshot.sha256(document) != digest or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise FleetManagerError("Snapshot digest mismatch")
    verify_capture(session, plan, document)
    ddb, s3 = aws_client(session, "dynamodb"), aws_client(session, "s3")
    if type(attempt) is not int or not 1 <= attempt <= 100:
        raise FleetManagerError("Restore attempt must be an integer from 1 to 100")
    prefix = "opencdr-recovery/" + snapshot.sha256(plan)
    prior, _ = quarantine._get(
        s3, plan["artifact_bucket"], prefix + "/restore.json", plan["account_id"]
    )
    if prior and prior.get("restore_attempt", 1) != attempt:
        raise FleetManagerError("A different restore attempt is already sealed")
    suffix = "" if attempt == 1 else "-a" + str(attempt)
    resources, fingerprints, pending = {}, {}, []
    for logical, table in document["tables"].items():
        target = plan["stack_name"] + "-r-" + digest[:12] + suffix + "-" + logical.lower()
        key = prefix + "/restores/" + str(attempt) + "/" + logical + ".json"
        provenance, _ = quarantine._get(s3, plan["artifact_bucket"], key, plan["account_id"])
        restored = snapshot.restore_table(
            ddb,
            table["backup"],
            target,
            digest,
            provenance=provenance,
            record=lambda value, key=key: _persist(s3, plan, key, value),
        )
        if restored["state"] != "ACTIVE":
            pending.append(logical)
            continue
        if not snapshot.configure_restored_table(ddb, restored, table["source"]["spec"]):
            pending.append(logical)
            continue
        current = ddb.describe_table(TableName=target)["Table"]
        restored["stream_arn"] = current.get("LatestStreamArn")
        fingerprint = snapshot.table_fingerprint(ddb, target, restored["id"])
        snapshot.compare_fingerprints(table["fingerprint"], fingerprint)
        resources[logical] = {k: v for k, v in restored.items() if k != "state"}
        resources[logical]["original_name"] = plan["tables"][logical].get(
            "original_name", table["source"]["name"]
        )
        fingerprints[logical] = fingerprint
    if pending:
        return {"state": "restores_pending", "pending": pending, "snapshot_sha256": digest}
    versions = {}
    for logical, bucket in document["buckets"].items():
        target = (
            "opencdr-r-" + plan["account_id"] + "-" + digest[:16] + suffix + "-" + logical.lower()
        )
        restored = snapshot.restore_objects(
            s3, bucket, target, plan["account_id"], plan["region"], digest
        )
        resources[logical] = {
            k: v for k, v in restored.items() if k not in {"versions", "completed_at"}
        }
        resources[logical]["usage"] = plan["buckets"][logical]["usage"]
        resources[logical]["original_name"] = plan["buckets"][logical].get(
            "original_name", bucket["source_bucket"]
        )
        versions[logical] = restored["versions"]
    # Source controls, claims, records and evidence must still match at completion.
    verify_capture(session, plan, document)
    binding = {
        "schema": BINDING_SCHEMA,
        "account_id": plan["account_id"],
        "region": plan["region"],
        "stage": plan["stage"],
        "snapshot_sha256": digest,
        "resources": resources,
    }
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "plan_sha256": snapshot.sha256(plan),
        "snapshot_sha256": digest,
        "restore_attempt": attempt,
        "binding": binding,
        "fingerprints": fingerprints,
        "object_versions": versions,
        "activated": False,
        "original_resources_retained": True,
        "history_complete": False,
    }
    _persist(s3, plan, "opencdr-recovery/" + snapshot.sha256(plan) + "/restore.json", receipt)
    return {"state": "restored", "receipt_sha256": snapshot.sha256(receipt), "receipt": receipt}


def _load(path, kind):
    raw = Path(path).read_bytes()
    if len(raw) > 2_000_000:
        raise FleetManagerError("Recovery file exceeds its byte limit")
    document = json.loads(raw)
    return document[kind] if kind in document and "schema" not in document else document


def configure_parser(actions):
    command = actions.add_parser(
        "storage-recovery", help="fence, snapshot and restore operational storage"
    )
    modes = command.add_subparsers(dest="recovery_command", required=True)
    prepare = modes.add_parser("plan")
    prepare.add_argument("stack")
    prepare.add_argument("--directory", type=Path, required=True)
    prepare.add_argument("--release-sha256", required=True)
    prepare.add_argument("--artifact-bucket", required=True)
    prepare.add_argument("--reason", required=True)
    for action in [
        "freeze",
        "capture",
        "restore",
        "resume-source",
        "bind-plan",
        "repair-plan",
        "resume-plan",
        "cutover-execute",
        "cutover-complete",
    ]:
        parser = modes.add_parser(action)
        parser.add_argument("plan", type=Path)
        parser.add_argument("--plan-sha256", required=True)
        if action == "restore":
            parser.add_argument("--snapshot", type=Path, required=True)
            parser.add_argument("--snapshot-sha256", required=True)
            parser.add_argument(
                "--restore-attempt",
                type=int,
                default=1,
                help="New destination suffix; retains previous unrecorded restores",
            )
        if action in {
            "bind-plan",
            "repair-plan",
            "resume-plan",
            "cutover-execute",
            "cutover-complete",
        }:
            parser.add_argument("--receipt", type=Path, required=True)
            parser.add_argument("--receipt-sha256", required=True)
            parser.add_argument("--directory", type=Path, required=True)
        if action in {"cutover-execute", "cutover-complete"}:
            parser.add_argument("--change-plan", type=Path, required=True)
            parser.add_argument("--change-sha256", required=True)
    for parser in modes.choices.values():
        parser.add_argument("--profile", required=True)
        parser.add_argument("--account-id", required=True)
        parser.add_argument("--region", default="us-east-1")
        parser.add_argument("--output", type=Path, required=True)


def run(args):
    # Output is exclusive: do not perform cloud work if its receipt would be lost.
    if args.output.exists():
        raise FleetManagerError("Choose a new recovery output file")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if args.recovery_command == "plan":
        value = quarantine.inventory(
            session,
            stack_name=args.stack,
            directory=args.directory,
            release_sha256=args.release_sha256,
            bucket=args.artifact_bucket,
            account_id=args.account_id,
            region=args.region,
            reason=args.reason,
        )
        result = {"state": "planned", "plan": value, "plan_sha256": snapshot.sha256(value)}
    else:
        value = _load(args.plan, "plan")
        quarantine.validate_plan(value, args.plan_sha256)
        if (value["account_id"], value["region"]) != (args.account_id, args.region):
            raise FleetManagerError("Recovery owner differs from the selected account/region")
        if args.recovery_command in {
            "bind-plan",
            "repair-plan",
            "resume-plan",
            "cutover-execute",
            "cutover-complete",
        }:
            from . import storage_cutover as cutover

            receipt = _load(args.receipt, "receipt")
            if snapshot.sha256(receipt) != args.receipt_sha256:
                raise FleetManagerError("Restoration receipt digest mismatch")
            if args.recovery_command in {"bind-plan", "repair-plan", "resume-plan"}:
                change = cutover.prepare(
                    session,
                    value,
                    receipt,
                    args.receipt_sha256,
                    args.directory,
                    phase=args.recovery_command.removesuffix("-plan"),
                )
                result = {
                    "state": "review_required",
                    "change": change,
                    "change_sha256": snapshot.sha256(change),
                }
            else:
                change = _load(args.change_plan, "change")
                fn = (
                    cutover.execute
                    if args.recovery_command == "cutover-execute"
                    else cutover.complete
                )
                result = fn(session, value, receipt, change, args.change_sha256, args.directory)
        elif args.recovery_command == "restore":
            result = restore(
                session,
                value,
                _load(args.snapshot, "snapshot"),
                args.snapshot_sha256,
                attempt=args.restore_attempt,
            )
        else:
            fn = {
                "freeze": quarantine.freeze,
                "capture": capture,
                "resume-source": quarantine.resume_source,
            }[args.recovery_command]
            result = fn(session, value, args.plan_sha256)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    args.output.chmod(0o600)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in {"plan", "snapshot", "receipt", "change"}},
            indent=2,
        )
    )
    return 0
