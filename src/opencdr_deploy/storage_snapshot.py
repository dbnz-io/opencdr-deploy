"""Physical backup/restore primitives for a fenced operational storage set.

No function here authorizes cutover or enables a worker. Callers must verify the
quarantine before and after capture. Snapshot manifests are content-addressed;
restoration never overwrites source resources or guesses a physical identity.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from .fleet_api import FleetManagerError
from .storage_bindings import BUCKET_TYPE, TABLE_TYPE

MAX_ROWS = 1_000_000
MAX_OBJECT_BYTES = 16_000_000
MAX_OBJECTS = 100_000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _typed(value):
    """DynamoDB sets are unordered; normalize their binary/numeric wire values."""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    if isinstance(value, dict):
        if len(value) == 1 and next(iter(value)) in {"SS", "NS", "BS"}:
            kind, entries = next(iter(value.items()))
            return {kind: sorted(_typed(entry) for entry in entries)}
        return {key: _typed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_typed(item) for item in value]
    return value


def table_schema(table):
    result = {
        k: sorted(table.get(k, []), key=canonical) for k in ["KeySchema", "AttributeDefinitions"]
    }
    for kind in ["GlobalSecondaryIndexes", "LocalSecondaryIndexes"]:
        values = []
        for index in table.get(kind, []):
            if index.get("IndexStatus", "ACTIVE") != "ACTIVE":
                raise FleetManagerError("Snapshot indexes must be active")
            value = {k: index[k] for k in ["IndexName", "KeySchema", "Projection"]}
            value["KeySchema"] = sorted(value["KeySchema"], key=canonical)
            values.append(value)
        result[kind] = sorted(values, key=canonical)
    return sha256(result)


def table_fingerprint(ddb, name, table_id, *, max_rows=MAX_ROWS):
    before = ddb.describe_table(TableName=name)["Table"]
    schema = table_schema(before)
    if before.get("TableStatus") != "ACTIVE" or before.get("TableId") != table_id:
        raise FleetManagerError("Snapshot table identity changed or is unavailable")
    cursor, hashes, pages, claims = None, [], 0, []
    while True:
        response = ddb.scan(
            TableName=name,
            ConsistentRead=True,
            **({"ExclusiveStartKey": cursor} if cursor else {}),
        )
        pages += 1
        for item in response.get("Items", []):
            normalized = _typed(item)
            hashes.append(sha256(normalized))
            if item.get("pk", {}).get("S", "").startswith("EFFECT#"):
                claims.append(sha256(normalized))
        if len(hashes) > max_rows:
            raise FleetManagerError("Snapshot table exceeds the declared row bound")
        following = response.get("LastEvaluatedKey")
        if not following:
            break
        if following == cursor:
            raise FleetManagerError("Snapshot scan did not advance")
        cursor = following
    after = ddb.describe_table(TableName=name)["Table"]
    if after.get("TableStatus") != "ACTIVE" or after.get("TableId") != table_id:
        raise FleetManagerError("Snapshot table was replaced during its inventory")
    if table_schema(after) != schema:
        raise FleetManagerError("Snapshot table schema changed during inventory")
    return {
        "schema_sha256": schema,
        "rows": len(hashes),
        "sha256": sha256(sorted(hashes)),
        "scan_pages": pages,
        "execution_claims": len(claims),
        "execution_claims_sha256": sha256(sorted(claims)),
    }


def compare_fingerprints(expected, observed):
    keys = {"rows", "sha256", "execution_claims", "execution_claims_sha256", "schema_sha256"}
    if {k: expected[k] for k in keys} != {k: observed[k] for k in keys}:
        raise FleetManagerError("Restored records or permanent execution claims do not match")


def backup_table(ddb, name, table_id, backup_name):
    """Reconcile a lost CreateBackup response using the exact unique plan name."""
    table = ddb.describe_table(TableName=name)["Table"]
    if table.get("TableId") != table_id or table.get("TableStatus") != "ACTIVE":
        raise FleetManagerError("Source table changed before backup")
    matches, cursor = [], None
    for _ in range(100):
        page = ddb.list_backups(
            TableName=name,
            BackupType="USER",
            Limit=100,
            **({"ExclusiveStartBackupArn": cursor} if cursor else {}),
        )
        matches += [r for r in page.get("BackupSummaries", []) if r["BackupName"] == backup_name]
        cursor = page.get("LastEvaluatedBackupArn")
        if not cursor:
            break
    else:
        raise FleetManagerError("Backup inventory exceeded its page bound")
    if len(matches) > 1:
        raise FleetManagerError("Ambiguous backup name; no restore is authorized")
    if matches:
        backup_arn = matches[0]["BackupArn"]
    else:
        backup_arn = ddb.create_backup(TableName=name, BackupName=backup_name)["BackupDetails"][
            "BackupArn"
        ]
    description = ddb.describe_backup(BackupArn=backup_arn)["BackupDescription"]
    if description["SourceTableDetails"]["TableId"] != table_id:
        raise FleetManagerError("Backup belongs to another physical source table")
    return {
        "arn": backup_arn,
        "state": description["BackupDetails"]["BackupStatus"],
        "created_at": description["BackupDetails"]["BackupCreationDateTime"].isoformat(),
        "source_name": name,
        "source_id": table_id,
    }


def restore_table(ddb, backup, target_name, snapshot_sha256, *, provenance=None, record=None):
    """Start or observe an exact backup restore; existing unrelated tables fail."""
    description = ddb.describe_backup(BackupArn=backup["arn"])["BackupDescription"]
    if (
        description["BackupDetails"]["BackupStatus"] != "AVAILABLE"
        or description["SourceTableDetails"]["TableId"] != backup["source_id"]
        or target_name == backup["source_name"]
    ):
        raise FleetManagerError("Backup is not available for this physical restore")
    try:
        table = ddb.describe_table(TableName=target_name)["Table"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        table = ddb.restore_table_from_backup(
            TargetTableName=target_name,
            BackupArn=backup["arn"],
        )["TableDescription"]
    restored = table.get("RestoreSummary", {})
    witness = {
        "backup_arn": backup["arn"],
        "table_name": target_name,
        "table_id": table.get("TableId"),
        "snapshot_sha256": snapshot_sha256,
    }
    if (
        (restored.get("SourceBackupArn") != backup["arn"] and provenance != witness)
        or (
            restored.get("SourceBackupArn") is not None
            and restored["SourceBackupArn"] != backup["arn"]
        )
        or table.get("TableName") != target_name
        or not table.get("TableId")
        or table["TableId"] == backup["source_id"]
        or (provenance is not None and provenance != witness)
    ):
        raise FleetManagerError("Destination is not the reviewed physical backup restore")
    # AWS can omit RestoreSummary after the table becomes ACTIVE. Preserve the
    # physical ID while the successful restore response still proves its source.
    # An unrecorded, already-active destination is never silently adopted.
    if record is not None:
        record(witness)
    if table.get("TableStatus") == "ACTIVE":
        ddb.tag_resource(
            ResourceArn=table["TableArn"],
            Tags=[
                {"Key": "OpenCDRSnapshot", "Value": snapshot_sha256},
                {"Key": "OpenCDRSourceTableId", "Value": backup["source_id"]},
            ],
        )
    return {
        "type": TABLE_TYPE,
        "source_name": backup["source_name"],
        "source_id": backup["source_id"],
        "name": target_name,
        "id": table.get("TableId"),
        "stream_arn": table.get("LatestStreamArn"),
        "state": table["TableStatus"],
    }


def configure_restored_table(ddb, table, source_spec):
    """Restore settings DynamoDB does not inherit. TTL stays disabled during review."""
    current = ddb.describe_table(TableName=table["name"])["Table"]
    if current.get("TableId") != table["id"] or current["TableStatus"] != "ACTIVE":
        raise FleetManagerError("Wait for the reviewed restored table to become ACTIVE")
    stream = source_spec.get("StreamSpecification", {"StreamEnabled": False})
    expected = {k: v for k, v in stream.items() if k in {"StreamEnabled", "StreamViewType"}}
    if "StreamViewType" in expected:
        # CloudFormation omits StreamEnabled; the DynamoDB API requires it.
        expected.setdefault("StreamEnabled", True)
    actual = current.get("StreamSpecification", {"StreamEnabled": False})
    if actual != expected:
        ddb.update_table(TableName=table["name"], StreamSpecification=expected)
        return False
    pitr = ddb.describe_continuous_backups(TableName=table["name"])["ContinuousBackupsDescription"]
    if pitr.get("PointInTimeRecoveryDescription", {}).get("PointInTimeRecoveryStatus") != "ENABLED":
        ddb.update_continuous_backups(
            TableName=table["name"],
            PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
        )
    ttl = ddb.describe_time_to_live(TableName=table["name"])["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus") != "DISABLED":
        raise FleetManagerError(
            "Restored TTL must remain disabled until integrity review completes"
        )
    return True


def _get_bytes(s3, bucket, key, account_id, *, version=None):
    response = s3.get_object(
        Bucket=bucket,
        Key=key,
        ExpectedBucketOwner=account_id,
        **({"VersionId": version} if version else {}),
    )
    try:
        body = response["Body"].read(MAX_OBJECT_BYTES + 1)
    finally:
        response["Body"].close()
    if len(body) > MAX_OBJECT_BYTES:
        raise FleetManagerError("Snapshot object exceeds the declared byte bound")
    return body, response


def capture_objects(s3, source, destination, account_id, *, prefix="", run_prefix):
    """Capture current immutable operational objects, with exact version receipts.

    Raw ingestion history is outside this snapshot. A caller selecting the feed
    payload prefix must use the separate runtime payload bucket on restoration.
    This never claims to recreate original provider-assigned S3 version IDs.
    """
    for bucket in (source, destination):
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account_id)
    if source == destination:
        raise FleetManagerError("Snapshot object destination must be separate from its source")
    token, records = None, []
    while True:
        page = s3.list_objects_v2(
            Bucket=source,
            Prefix=prefix,
            ExpectedBucketOwner=account_id,
            **({"ContinuationToken": token} if token else {}),
        )
        for item in page.get("Contents", []):
            raw, response = _get_bytes(s3, source, item["Key"], account_id)
            if response["ETag"] != item["ETag"] or response["ContentLength"] != item["Size"]:
                raise FleetManagerError("Source object changed during snapshot inventory")
            digest = hashlib.sha256(raw).hexdigest()
            backup_key = run_prefix.rstrip("/") + "/objects/" + digest
            try:
                s3.put_object(
                    Bucket=destination,
                    Key=backup_key,
                    Body=raw,
                    IfNoneMatch="*",
                    ExpectedBucketOwner=account_id,
                    ServerSideEncryption="AES256",
                    ContentType="application/octet-stream",
                    Metadata={"sha256": digest},
                )
            except ClientError as error:
                if error.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                    raise
                old, _ = _get_bytes(s3, destination, backup_key, account_id)
                if hashlib.sha256(old).hexdigest() != digest:
                    raise FleetManagerError(
                        "Existing snapshot object has different bytes"
                    ) from None
            records.append(
                {
                    "key": item["Key"],
                    "version_id": response.get("VersionId"),
                    "etag": response["ETag"],
                    "bytes": len(raw),
                    "sha256": digest,
                    "backup_bucket": destination,
                    "backup_key": backup_key,
                }
            )
            if len(records) > MAX_OBJECTS:
                raise FleetManagerError("Snapshot object inventory exceeds its declared bound")
        if not page.get("IsTruncated"):
            break
        following = page["NextContinuationToken"]
        if following == token:
            raise FleetManagerError("Snapshot object inventory did not advance")
        token = following
    return {
        "source_bucket": source,
        "prefix": prefix,
        "objects": records,
        "bytes": sum(r["bytes"] for r in records),
        "sha256": sha256(records),
        "coverage": "current_objects_only_original_version_ids_recorded",
    }


def verify_source_objects(s3, snapshot, account_id):
    observed, token = {}, None
    while True:
        page = s3.list_objects_v2(
            Bucket=snapshot["source_bucket"],
            Prefix=snapshot["prefix"],
            ExpectedBucketOwner=account_id,
            **({"ContinuationToken": token} if token else {}),
        )
        observed.update({r["Key"]: (r["ETag"], r["Size"]) for r in page.get("Contents", [])})
        if not page.get("IsTruncated"):
            break
        token = page["NextContinuationToken"]
        if len(observed) > MAX_OBJECTS:
            raise FleetManagerError("Source object inventory exceeded its bound")
    expected = {r["key"]: (r["etag"], r["bytes"]) for r in snapshot["objects"]}
    if observed != expected:
        raise FleetManagerError("Source object set changed while the snapshot was captured")
    for row in snapshot["objects"]:
        current = s3.head_object(
            Bucket=snapshot["source_bucket"],
            Key=row["key"],
            ExpectedBucketOwner=account_id,
        )
        if current.get("VersionId") != row["version_id"]:
            raise FleetManagerError("Source object version changed after capture")


def restore_objects(s3, snapshot, target, account_id, region, snapshot_sha256):
    """Create a private, retained destination; verify each restored object hash."""
    if target in {snapshot["source_bucket"], *(r["backup_bucket"] for r in snapshot["objects"])}:
        raise FleetManagerError("Restored evidence needs a separate destination")
    try:
        s3.head_bucket(Bucket=target, ExpectedBucketOwner=account_id)
    except ClientError as error:
        if error.response["Error"]["Code"] not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        s3.create_bucket(
            Bucket=target,
            ObjectOwnership="BucketOwnerEnforced",
            **(
                {"CreateBucketConfiguration": {"LocationConstraint": region}}
                if region != "us-east-1"
                else {}
            ),
        )
        s3.put_bucket_tagging(
            Bucket=target,
            ExpectedBucketOwner=account_id,
            Tagging={"TagSet": [{"Key": "OpenCDRSnapshot", "Value": snapshot_sha256}]},
        )
    tags = s3.get_bucket_tagging(Bucket=target, ExpectedBucketOwner=account_id)["TagSet"]
    if {r["Key"]: r["Value"] for r in tags}.get("OpenCDRSnapshot") != snapshot_sha256:
        raise FleetManagerError("Existing bucket does not belong to this recovery snapshot")
    s3.put_public_access_block(
        Bucket=target,
        ExpectedBucketOwner=account_id,
        PublicAccessBlockConfiguration=dict.fromkeys(
            ["BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"],
            True,
        ),
    )
    s3.put_bucket_encryption(
        Bucket=target,
        ExpectedBucketOwner=account_id,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    s3.put_bucket_versioning(
        Bucket=target, ExpectedBucketOwner=account_id, VersioningConfiguration={"Status": "Enabled"}
    )
    s3.put_bucket_policy(
        Bucket=target,
        ExpectedBucketOwner=account_id,
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "s3:*",
                        "Resource": ["arn:aws:s3:::" + target, "arn:aws:s3:::" + target + "/*"],
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    }
                ],
            }
        ),
    )
    versions = []
    for row in snapshot["objects"]:
        raw, _ = _get_bytes(s3, row["backup_bucket"], row["backup_key"], account_id)
        if hashlib.sha256(raw).hexdigest() != row["sha256"] or len(raw) != row["bytes"]:
            raise FleetManagerError("Backup object integrity check failed")
        try:
            response = s3.put_object(
                Bucket=target,
                Key=row["key"],
                Body=raw,
                ExpectedBucketOwner=account_id,
                IfNoneMatch="*",
                ServerSideEncryption="AES256",
                ContentType="application/json",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                raise
            response = s3.head_object(Bucket=target, Key=row["key"], ExpectedBucketOwner=account_id)
        restored, _ = _get_bytes(s3, target, row["key"], account_id)
        if hashlib.sha256(restored).hexdigest() != row["sha256"]:
            raise FleetManagerError("Restored evidence bytes differ from the snapshot")
        versions.append(
            {
                "key": row["key"],
                "source_version_id": row["version_id"],
                "restored_version_id": response.get("VersionId"),
                "sha256": row["sha256"],
            }
        )
    return {
        "type": BUCKET_TYPE,
        "source_name": snapshot["source_bucket"],
        "source_id": snapshot["source_bucket"],
        "name": target,
        "id": target,
        "stream_arn": None,
        "versions": versions,
        "completed_at": datetime.now(UTC).isoformat(),
    }
