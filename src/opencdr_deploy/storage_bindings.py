"""Apply a reviewed physical storage binding without discarding old resources.

Recovery keeps the original CloudFormation storage resources retained. Runtime
references move to independently restored resources whose physical IDs are in
the recovery receipt. The receipt is not authorization to resume workers.
"""

from __future__ import annotations

import copy
import hashlib
import re

from .fleet_api import FleetManagerError

METADATA_KEY = "OpenCDRStorageRecovery"
SCHEMA = "opencdr.storage-binding/1"
TABLE_TYPE = "AWS::DynamoDB::Table"
BUCKET_TYPE = "AWS::S3::Bucket"


def current_binding(template):
    binding = template.get("Metadata", {}).get(METADATA_KEY)
    if binding is None:
        return None
    # CloudFormation rejects JSON null even inside Metadata. Absence represents
    # a resource without a stream; receipts keep the explicit nullable field.
    binding = copy.deepcopy(binding)
    if isinstance(binding, dict) and isinstance(binding.get("resources"), dict):
        for row in binding["resources"].values():
            if isinstance(row, dict):
                row.setdefault("stream_arn", None)
    return validate(binding)


def verify_physical(session, binding):
    """A stored logical mapping never substitutes for AWS physical identity."""
    from .fleet_api import aws_client, verify_account

    validate(binding)
    verify_account(session, binding["account_id"])
    ddb, s3 = aws_client(session, "dynamodb"), aws_client(session, "s3")
    for row in binding["resources"].values():
        if row["type"] == TABLE_TYPE:
            table = ddb.describe_table(TableName=row["name"])["Table"]
            if (
                table.get("TableId") != row["id"]
                or table.get("TableStatus") != "ACTIVE"
                or table.get("LatestStreamArn") != row["stream_arn"]
            ):
                raise FleetManagerError("Recovered physical table or stream identity changed")
        else:
            s3.head_bucket(Bucket=row["name"], ExpectedBucketOwner=binding["account_id"])
            tags = s3.get_bucket_tagging(
                Bucket=row["name"], ExpectedBucketOwner=binding["account_id"]
            )
            if {r["Key"]: r["Value"] for r in tags["TagSet"]}.get("OpenCDRSnapshot") != binding[
                "snapshot_sha256"
            ]:
                raise FleetManagerError("Recovered evidence bucket ownership marker changed")


def bound_ids(ids, template):
    binding = current_binding(template)
    if not binding:
        return ids
    return {
        **ids,
        **{
            logical: row["name"]
            for logical, row in binding["resources"].items()
            if row.get("usage") != "detection_payload"
        },
    }


def validate(binding):
    if not isinstance(binding, dict) or set(binding) != {
        "schema",
        "account_id",
        "region",
        "stage",
        "snapshot_sha256",
        "resources",
    }:
        raise FleetManagerError("Invalid recovery storage binding")
    if (
        binding["schema"] != SCHEMA
        or not re.fullmatch(r"\d{12}", binding["account_id"])
        or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", binding["region"])
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,17}", binding["stage"])
        or not re.fullmatch(r"[a-f0-9]{64}", binding["snapshot_sha256"])
        or not isinstance(binding["resources"], dict)
        or not binding["resources"]
    ):
        raise FleetManagerError("Invalid recovery storage binding identity")
    destinations = set()
    for logical, row in binding["resources"].items():
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", logical)
            or not isinstance(row, dict)
            or set(row) - {"usage", "original_name"}
            != {"type", "source_name", "source_id", "name", "id", "stream_arn"}
            or row.get("usage", "storage") not in {"storage", "detection_payload"}
        ):
            raise FleetManagerError("Invalid recovery resource binding")
        if row.get("usage") == "detection_payload" and (
            logical != "IngestionBucket" or row["type"] != BUCKET_TYPE
        ):
            raise FleetManagerError("Only the Core feed payload prefix may use a partial binding")
        if row["type"] == TABLE_TYPE:
            if any(
                not isinstance(row[key], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", row[key])
                for key in ("source_name", "name")
            ) or any(
                not isinstance(row[key], str) or not re.fullmatch(r"[a-f0-9-]{36}", row[key])
                for key in ("source_id", "id")
            ):
                raise FleetManagerError("Invalid restored table identity")
            if row["id"] == row["source_id"]:
                raise FleetManagerError("A restore must have a new physical table identity")
            stream = row["stream_arn"]
            prefix = (
                f"arn:aws:dynamodb:{binding['region']}:{binding['account_id']}:"
                f"table/{row['name']}/stream/"
            )
            if stream is not None and (
                not isinstance(stream, str)
                or not stream.startswith(prefix)
                or not re.fullmatch(r"[0-9T:.\-]+", stream[len(prefix) :])
            ):
                raise FleetManagerError("Restored stream belongs to another table")
        elif row["type"] == BUCKET_TYPE:
            if (
                any(
                    not isinstance(row[key], str)
                    or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", row[key])
                    for key in ("source_name", "name")
                )
                or row["source_id"] != row["source_name"]
                or row["id"] != row["name"]
            ):
                raise FleetManagerError("Invalid restored bucket identity")
            if row["stream_arn"] is not None:
                raise FleetManagerError("Buckets do not have a stream binding")
        else:
            raise FleetManagerError("Only tables and buckets may be rebound")
        if row["name"] == row["source_name"] or row["name"] in destinations:
            raise FleetManagerError("Recovery cannot overwrite or alias source resources")
        if "original_name" in row and (
            not isinstance(row["original_name"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", row["original_name"])
        ):
            raise FleetManagerError("Invalid original storage lineage")
        destinations.add(row["name"])
    if destinations & {
        name
        for row in binding["resources"].values()
        for name in [row["source_name"], row.get("original_name", row["source_name"])]
    }:
        raise FleetManagerError("Recovery destinations cannot be other source resources")
    return binding


def arn(binding, row, *, source=False):
    name = row["source_name"] if source else row["name"]
    if row["type"] == BUCKET_TYPE:
        return "arn:aws:s3:::" + name
    return f"arn:aws:dynamodb:{binding['region']}:{binding['account_id']}:table/{name}"


def apply_binding(template, binding, *, parameters=None):
    """Pure transformation. Callers must verify physical identities before apply.

    The old storage and bucket policies remain attached to their old resources.
    Dependent runtime/IAM/output references move together. A missing stream fails
    instead of creating a mapping against an unrelated or stale stream.
    """
    validate(binding)
    result = copy.deepcopy(template)
    resources = result.get("Resources", {})
    for logical, row in binding["resources"].items():
        if resources.get(logical, {}).get("Type") != row["type"]:
            raise FleetManagerError(f"Release no longer defines recovered resource {logical}")
    params = {
        key: str((parameters or {}).get(key, spec.get("Default", "")))
        for key, spec in template.get("Parameters", {}).items()
    }
    params.update(
        {
            "AWS::AccountId": binding["account_id"],
            "AWS::Region": binding["region"],
            "AWS::Partition": "aws",
            "Stage": binding["stage"],
        }
    )
    storage = {
        name: row
        for name, row in binding["resources"].items()
        if row.get("usage") != "detection_payload"
    }

    def substitute(text, local=None):
        # Resolve only known scalar parameters; preserve all other substitutions.
        for key, value in {**params, **(local or {})}.items():
            if isinstance(value, (str, int)):
                text = text.replace("${" + key + "}", str(value))
        for logical, row in storage.items():
            for suffix, value in (("", row["name"]), (".Arn", arn(binding, row))):
                text = text.replace("${" + logical + suffix + "}", value)
            if "${" + logical + ".StreamArn}" in text:
                if not row["stream_arn"]:
                    raise FleetManagerError(f"Restored {logical} stream is not ready")
                text = text.replace("${" + logical + ".StreamArn}", row["stream_arn"])
            # Old templates also embed physical table names in environment/IAM.
            for source_name in {row["source_name"], row.get("original_name", row["source_name"])}:
                text = re.sub(
                    r"(?<![A-Za-z0-9_.-])" + re.escape(source_name) + r"(?![A-Za-z0-9_.-])",
                    lambda _, target=row["name"]: target,
                    text,
                )
        return text

    def replace(value):
        if isinstance(value, dict):
            if set(value) == {"Ref"} and value["Ref"] in storage:
                return storage[value["Ref"]]["name"]
            if set(value) == {"Fn::GetAtt"}:
                attribute = value["Fn::GetAtt"]
                logical, prop = attribute.split(".", 1) if isinstance(attribute, str) else attribute
                if logical in storage:
                    row = storage[logical]
                    if prop == "Arn":
                        return arn(binding, row)
                    if prop == "StreamArn" and row["stream_arn"]:
                        return row["stream_arn"]
                    if prop == "DomainName" and row["type"] == BUCKET_TYPE:
                        return row["name"] + ".s3.amazonaws.com"
                    raise FleetManagerError(f"Unsupported recovered attribute {logical}.{prop}")
            if set(value) == {"Fn::Sub"}:
                expression = value["Fn::Sub"]
                if isinstance(expression, str):
                    return {"Fn::Sub": substitute(expression)}
                text, local = expression
                # Local substitutions shadow resource names, so do not silently
                # reinterpret a caller-defined alias as a physical binding.
                if set(local) & set(binding["resources"]):
                    raise FleetManagerError("Recovery cannot rewrite shadowed resource aliases")
                return {"Fn::Sub": [substitute(text, local), replace(local)]}
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, str):
            return substitute(value)
        return value

    def retain_stream_reads(original, updated):
        # Replaced stream mappings are retained for queued records. Their role
        # must retain read access to the original stream as well as the new one.
        # This never retains old table write permissions.
        stream_actions = {
            "dynamodb:DescribeStream",
            "dynamodb:GetRecords",
            "dynamodb:GetShardIterator",
            "dynamodb:ListStreams",
        }
        if isinstance(original, dict) and isinstance(updated, dict):
            actions = original.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            if original.get("Effect") == "Allow" and actions and set(actions) <= stream_actions:
                entries = original.get("Resource", [])
                entries = entries if isinstance(entries, list) else [entries]
                replaced = updated.get("Resource", [])
                replaced = replaced if isinstance(replaced, list) else [replaced]
                targets = []
                for entry in replaced:
                    if entry not in targets:
                        targets.append(entry)
                for entry in entries:
                    if entry not in targets:
                        targets.append(copy.deepcopy(entry))
                for row in storage.values():
                    if row["type"] != TABLE_TYPE or not row["stream_arn"]:
                        continue
                    if (
                        row["source_name"] != row.get("original_name", row["source_name"])
                        and row["stream_arn"] in targets
                    ):
                        previous = arn(binding, row, source=True) + "/stream/*"
                        if previous not in targets:
                            targets.append(previous)
                updated["Resource"] = targets
            for key, value in original.items():
                if key in updated:
                    retain_stream_reads(value, updated[key])
        elif isinstance(original, list) and isinstance(updated, list):
            for before, after in zip(original, updated, strict=False):
                retain_stream_reads(before, after)

    for logical, resource in list(resources.items()):
        if resource["Type"] in {
            TABLE_TYPE,
            BUCKET_TYPE,
            "AWS::S3::BucketPolicy",
            "AWS::SQS::Queue",
        }:
            # Rebinding never deletes the source, including during later upgrades.
            if logical in binding["resources"]:
                resource.update(DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
            continue
        resources[logical] = replace(resource)
        if resource["Type"] == "AWS::IAM::Role":
            retain_stream_reads(resource, resources[logical])
        if resource["Type"] == "AWS::Lambda::EventSourceMapping":
            # A replaced mapping may still own unprocessed old stream records.
            # The recovery controller must explicitly inventory/drain it.
            resources[logical].update(DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
            before_stream = resource["Properties"]["EventSourceArn"]
            after_stream = resources[logical]["Properties"]["EventSourceArn"]
            if (
                isinstance(after_stream, str)
                and before_stream != after_stream
                and after_stream
                in {row["stream_arn"] for row in storage.values() if row["stream_arn"]}
            ):
                # DisableRollback cannot replace a resource. Keep the original
                # mapping managed and add a distinct mapping for restored data.
                name = (
                    "RecoveredMapping"
                    + hashlib.sha256((logical + "\n" + after_stream).encode()).hexdigest()[:40]
                )
                restored = resources[logical]
                if name in resources and resources[name] != restored:
                    raise FleetManagerError("Recovered stream mapping logical ID collision")
                resources[name] = restored
                resources[logical] = copy.deepcopy(resource)
                resources[logical].update(DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
    result["Outputs"] = replace(result.get("Outputs", {}))
    payload = binding["resources"].get("IngestionBucket", {})
    if payload.get("usage") == "detection_payload":
        # Raw ingestion jobs and provider version IDs continue to use their
        # original bucket. Only the immutable feed payload namespace moves.
        for resource in resources.values():
            if resource["Type"] == "AWS::Lambda::Function":
                env = resource.get("Properties", {}).get("Environment", {}).get("Variables", {})
                if "DETECTION_FEED_TABLE_NAME" in env:
                    env["DETECTION_PAYLOAD_BUCKET"] = payload["name"]

        def payload_reference(value):
            if isinstance(value, dict):
                if value == {"Ref": "IngestionBucket"}:
                    return payload["name"]
                if value in (
                    {"Fn::GetAtt": ["IngestionBucket", "Arn"]},
                    {"Fn::GetAtt": "IngestionBucket.Arn"},
                ):
                    return arn(binding, payload)
                return {key: payload_reference(item) for key, item in value.items()}
            if isinstance(value, list):
                return [payload_reference(item) for item in value]
            if isinstance(value, str):
                value = value.replace("${IngestionBucket.Arn}", arn(binding, payload)).replace(
                    "${IngestionBucket}", payload["name"]
                )
                for name in {
                    payload["source_name"],
                    payload.get("original_name", payload["source_name"]),
                }:
                    value = re.sub(
                        re.escape("arn:aws:s3:::" + name) + r"(?=/|$)",
                        lambda _: arn(binding, payload),
                        value,
                    )
                return value
            return value

        def add_payload_permission(value):
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    if key == "Resource":
                        entries = item if isinstance(item, list) else [item]
                        expanded = []
                        for entry in entries:
                            expanded.append(entry)
                            translated = payload_reference(entry)
                            if translated != entry:
                                expanded.append(translated)
                        result[key] = expanded
                    else:
                        result[key] = add_payload_permission(item)
                return result
            if isinstance(value, list):
                return [add_payload_permission(item) for item in value]
            return value

        for logical, resource in list(resources.items()):
            if resource["Type"] == "AWS::IAM::Role":
                resources[logical] = add_payload_permission(resource)
        result["Outputs"]["RecoveredDetectionPayloadBucket"] = {"Value": payload["name"]}
    metadata = result.setdefault("Metadata", {})
    if METADATA_KEY in metadata and current_binding(result) != binding:
        raise FleetManagerError("Release already carries a different recovery binding")
    metadata[METADATA_KEY] = copy.deepcopy(binding)
    for row in metadata[METADATA_KEY]["resources"].values():
        if row["stream_arn"] is None:
            del row["stream_arn"]
    return result
