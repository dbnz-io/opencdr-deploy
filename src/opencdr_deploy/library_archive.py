"""Read-only native library export with checked references and retained source bytes.

This adapter never freezes a writer, imports a live row, or activates an owner.
A repeatable read of active storage is not an attested migration snapshot.
"""

import base64
import json
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer

from .migration_archive import MAX_BYTES, MAX_RECORDS, canonical, export, require, sha

FORMAT = "opencdr.native-library/1"


def identity(pk, sk):
    return sha(canonical([pk, sk]))


def decoded(item):
    return {key: TypeDeserializer().deserialize(value) for key, value in item.items()}


def native_source(owner, items, objects):
    """Validate native relationships without rewriting historical actors or hashes."""
    require(len(items) <= MAX_RECORDS, "Library row limit exceeded")
    rows, wires = {}, {}
    for item in items:
        row = decoded(item)
        key = (row.get("pk"), row.get("sk"))
        require(all(isinstance(k, str) and k for k in key), "Invalid library storage key")
        require(key not in rows, "Duplicate library storage key")
        rows[key], wires[key] = row, item
    records, unresolved, object_records = [], [], {}

    def ref(pk, sk):
        require((pk, sk) in rows, "Missing native library relationship")
        return {"family": "library", "id": identity(pk, sk)}

    def version(reference):
        require(isinstance(reference, dict), "Invalid native version reference")
        number = reference.get("version")
        require(
            isinstance(number, (int, Decimal))
            and not isinstance(number, bool)
            and number >= 1
            and number == int(number),
            "Invalid native version number",
        )
        key = ("VERSION#" + reference["content_id"], f"{int(number):016d}")
        value = ref(*key)
        require(
            rows[key].get("artifact_sha256") == reference.get("artifact_sha256"),
            "Native version reference checksum mismatch",
        )
        return value

    for key, row in sorted(rows.items()):
        pk, sk = key
        if pk == "OWNERSHIP":
            continue  # Captured as source metadata; never imported as authority.
        references, artifact = [], None
        if "artifact_key" in row:
            object_key = row["artifact_key"]
            require(
                isinstance(object_key, str) and object_key.startswith("library/"),
                "Library artifact key escapes its prefix",
            )
            require(object_key in objects, "Missing native library object")
            raw = objects[object_key]
            require(isinstance(raw, bytes) and len(raw) <= 131072, "Invalid library object size")
            require(sha(raw) == row.get("artifact_sha256"), "Native artifact checksum mismatch")
            artifact = json.loads(raw)
            require(isinstance(artifact, dict), "Invalid native artifact")
            oid = sha(object_key.encode())
            object_records[oid] = {
                "family": "evidence",
                "id": oid,
                "references": [],
                "payload": {
                    "format": FORMAT,
                    "key": object_key,
                    "sha256": sha(raw),
                    "body_base64": base64.b64encode(raw).decode(),
                },
            }
            references.append({"family": "evidence", "id": oid})
        if pk == "ITEM":
            require(
                row.get("content_id") == sk
                and row.get("kind") in {"signal", "correlation", "list"},
                "Invalid native content identity",
            )
            references.append(ref("IDENTITY#" + row["kind"], row["runtime_id"]))
            latest = row.get("latest_version")
            require(
                isinstance(latest, (int, Decimal))
                and not isinstance(latest, bool)
                and 0 <= latest <= MAX_RECORDS
                and latest == int(latest),
                "Invalid version head",
            )
            for number in range(1, int(latest) + 1):
                references.append(ref("VERSION#" + sk, f"{number:016d}"))
        elif pk.startswith("IDENTITY#"):
            references.append(ref("ITEM", row["content_id"]))
            head = rows[("ITEM", row["content_id"])]
            require(
                pk == "IDENTITY#" + head["kind"] and sk == head["runtime_id"],
                "Native runtime identity mismatch",
            )
        elif pk.startswith("VERSION#"):
            require(
                artifact is not None
                and pk == "VERSION#" + row["content_id"]
                and sk == f"{int(row['version']):016d}",
                "Invalid native version identity",
            )
            references.append(ref("ITEM", row["content_id"]))
            require(
                all(
                    artifact.get(k) == row.get(k)
                    for k in [
                        "content_id",
                        "version",
                        "kind",
                        "runtime_id",
                        "dependencies",
                        "provenance",
                    ]
                ),
                "Native artifact metadata mismatch",
            )
            require(
                sha(canonical(artifact["document"])) == row["document_sha256"],
                "Native document checksum mismatch",
            )
            references.extend(version(r) for r in row["dependencies"])
        elif pk == "RELEASE":
            require(
                artifact is not None
                and artifact.get("release_id") == sk
                and row.get("release_id") == sk,
                "Invalid native release identity",
            )
            references.extend(version(r) for r in [*artifact["roots"], *artifact["items"]])
        elif pk.startswith("AUDIT#"):
            references.append(ref("ITEM", pk.split("#", 1)[1]))
        elif pk.startswith("TEST#"):
            require(
                artifact is not None and artifact.get("reference") == row.get("reference"),
                "Native test reference mismatch",
            )
            references.append(version(row["reference"]))
            references.extend(version(r) for r in artifact["dependencies"])
        elif pk.startswith(("COMPILE#", "MAPPING#")):
            release = pk.split("#", 1)[1]
            references.append(ref("RELEASE", release))
            require(
                artifact is not None
                and artifact.get("release_id") == release
                and row.get("release_id") == release,
                "Native compilation/release mismatch",
            )
            if pk.startswith("COMPILE#"):
                require(
                    artifact.get("source_artifact_sha256")
                    == rows[("RELEASE", release)]["artifact_sha256"],
                    "Native compilation source mismatch",
                )
            else:
                compile_key = ("COMPILE#" + release, row["compilation_id"])
                references.append(ref(*compile_key))
                require(
                    artifact.get("compilation_id") == row["compilation_id"]
                    and artifact.get("compilation_sha256") == rows[compile_key]["artifact_sha256"],
                    "Native legacy mapping checksum mismatch",
                )
        elif not pk.startswith("REQUEST#"):
            unresolved.append({"reason": "unknown_native_library_partition", "item": wires[key]})
            continue
        records.append(
            {
                "family": "library",
                "id": identity(*key),
                "payload": {"format": FORMAT, "item": wires[key]},
                "references": sorted({canonical(r): r for r in references}.values(), key=canonical),
            }
        )
    records.extend(object_records.values())
    result = {
        "source": {
            **owner,
            "format": FORMAT,
            "writer_fence_attested": False,
            "ownership_observation": wires.get(("OWNERSHIP", "STATE")),
            "ownership_observations": {sk: wires[(pk, sk)] for pk, sk in rows if pk == "OWNERSHIP"},
            "object_scope": "referenced_library_objects_only",
        },
        "identity_map": {sk: sk for pk, sk in rows if pk == "ITEM"},
        "records": records,
        "unresolved": unresolved,
    }
    require(len(canonical(result)) <= MAX_BYTES, "Native archive total byte limit exceeded")
    return result


def capture(session, account, region, stack, directory, *, family="library", migration_guard=None):
    from .detection_activation import resource_ids, template_body
    from .fleet_api import aws_client, verify_account
    from .storage_bindings import bound_ids
    from .storage_quarantine import require_unlocked

    verify_account(session, account)
    cf = aws_client(session, "cloudformation")
    current = cf.describe_stacks(StackName=stack)["Stacks"][0]
    require(
        current["StackStatus"] in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}, "Engine stack unsettled"
    )
    if migration_guard is None:
        require_unlocked(session, current)
    else:
        migration_guard()
    template = template_body(cf, StackName=stack)
    ids = bound_ids(resource_ids(cf, stack, template), template)
    require(family in {"library", "cases"}, "Unsupported native archive family")
    table, bucket = (
        (ids["LibraryTable"], ids["LibraryBucket"])
        if family == "library"
        else (ids["CaseTable"], ids["CaseBucket"])
    )
    object_limit = 131072 if family == "library" else 1_000_000
    object_prefix = "library/" if family == "library" else "cases/"
    ddb, s3 = aws_client(session, "dynamodb"), aws_client(session, "s3")
    description = ddb.describe_table(TableName=table)["Table"]
    require(description["TableStatus"] == "ACTIVE", "Library table unavailable")

    def scan():
        rows, cursor, seen, total = [], None, set(), 0
        while True:
            page = ddb.scan(
                TableName=table,
                ConsistentRead=True,
                Limit=100,
                **({"ExclusiveStartKey": cursor} if cursor else {}),
            )
            for row in page.get("Items", []):
                total += len(canonical(row))
                require(total <= MAX_BYTES, "Library source byte limit exceeded")
                rows.append(row)
            require(len(rows) <= MAX_RECORDS, "Library row limit exceeded")
            cursor = page.get("LastEvaluatedKey")
            if not cursor:
                return sorted(rows, key=canonical)
            marker = canonical(cursor)
            require(marker not in seen, "Library scan did not advance")
            seen.add(marker)

    items, objects, total = scan(), {}, 0
    keys = set()
    for item in items:
        row = decoded(item)
        if family == "library" and row.get("artifact_key"):
            keys.add(row["artifact_key"])
        elif family == "cases" and row.get("pk") == "CASE":
            keys.update(a["evidence_key"] for a in row.get("attachments", []))
    for key in sorted(keys):
        require(key.startswith(object_prefix), "Native object prefix mismatch")
        response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
        try:
            raw = response["Body"].read(object_limit + 1)
        finally:
            response["Body"].close()
        require(len(raw) <= object_limit, "Native object byte limit exceeded")
        total += len(raw)
        require(total <= MAX_BYTES, "Native object aggregate limit exceeded")
        objects[key] = raw
    after_items = scan()
    if migration_guard is None:
        require(items == after_items, "Library changed during capture; no archive published")
    else:
        from .ownership_transfer import movable

        migration_guard()
        require(
            [r for r in items if movable(r, family)]
            == [r for r in after_items if movable(r, family)],
            "Native family changed behind the migration fence",
        )
    after = ddb.describe_table(TableName=table)["Table"]
    require(
        after["TableId"] == description["TableId"] and after["TableStatus"] == "ACTIVE",
        "Library physical identity changed",
    )
    final = cf.describe_stacks(StackName=stack)["Stacks"][0]
    require(
        all(final.get(k) == current.get(k) for k in ["StackId", "StackStatus", "ChangeSetId"]),
        "Engine deployment changed during capture",
    )
    adapter = native_source
    if family == "cases":
        from .case_archive import native_source as adapter
    source = adapter(
        {
            "account_id": account,
            "region": region,
            "stack_id": current["StackId"],
            "change_set_id": current.get("ChangeSetId"),
            "table": table,
            "table_id": description["TableId"],
            "bucket": bucket,
            "inert_history_consistency": "observational" if migration_guard else "two_equal_reads",
        },
        items,
        objects,
    )
    return export(source, directory)
