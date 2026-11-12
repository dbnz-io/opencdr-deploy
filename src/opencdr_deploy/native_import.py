"""Deterministic native import previews. No storage writes, grants or activation.

A preview is material, not permission to apply it. Preserve both its source
archive and the identity map; historical audit actors and text are not rewritten.
"""

import base64
import copy
import re

from boto3.dynamodb.types import TypeSerializer

from .library_archive import decoded, identity
from .migration_archive import canonical, require, sha, verify

SCHEMA = "opencdr.native-import-preview/1"


def wire(row):
    return {k: TypeSerializer().serialize(v) for k, v in row.items()}


def prepare(directory, manifest_sha256, agent_map):
    manifest, records = verify(directory, manifest_sha256)
    require(not manifest["unresolved"], "Resolve archive findings before preparing an import")
    kind = manifest["source"].get("format")
    require(
        kind in {"opencdr.native-library/1", "opencdr.native-cases/1"},
        "Native source adapter required",
    )
    require(
        isinstance(agent_map, dict)
        and all(
            isinstance(k, str)
            and isinstance(v, str)
            and k
            and v
            and len(k) <= 128
            and len(v) <= 128
            for k, v in agent_map.items()
        ),
        "Explicit valid agent mapping required",
    )
    require(len(set(agent_map.values())) == len(agent_map), "Agent mapping collision")
    require(kind != "opencdr.native-library/1" or not agent_map, "Library identity is owner-wide")
    # A manifest pin authenticates selected bytes, not their native semantics.
    # Re-run the adapter graph validation before producing mutation material.
    from . import case_archive, library_archive

    objects_by_key, native_items = {}, []
    for record in records:
        payload = record["payload"]
        require(payload.get("format") == kind, "Mixed native source formats")
        if record["family"] == "evidence":
            key = payload["key"]
            require(key not in objects_by_key, "Duplicate native object key")
            objects_by_key[key] = base64.b64decode(payload["body_base64"], validate=True)
        else:
            native_items.append(payload["item"])
    adapter = library_archive if kind == "opencdr.native-library/1" else case_archive
    rebuilt = adapter.native_source({}, native_items, objects_by_key)
    require(
        not rebuilt["unresolved"]
        and sorted(rebuilt["records"], key=canonical) == sorted(records, key=canonical),
        "Archive differs from validated native graph",
    )
    require(
        all(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", v)
            for pair in agent_map.items()
            for v in pair
        ),
        "Invalid case scope mapping",
    )
    attachments, selected_agents = {}, set()
    for record in records:
        if record["family"] != "case":
            continue
        row = decoded(record["payload"]["item"])
        require(row["pk"] == "CASE", "Unexpected native case partition")
        selected_agents.update(row["agent_ids"])
        require(set(row["agent_ids"]) <= set(agent_map), "Unmapped source case scope")
        for item in row["attachments"]:
            value = sha(
                canonical(
                    {"agent_id": agent_map[item["agent_id"]], "detection_id": item["detection_id"]}
                )
            )
            previous = attachments.setdefault(item["attachment_id"], value)
            require(previous == value, "Ambiguous attachment mapping")
    require(not selected_agents or selected_agents == set(agent_map), "Unused agent mapping")

    def case_view(value):
        value = copy.deepcopy(value)
        if "agent_ids" in value:
            require(set(value["agent_ids"]) <= set(agent_map), "Unmapped receipt scope")
            value["agent_ids"] = sorted(agent_map[a] for a in value["agent_ids"])
        for attachment in value.get("attachments", []):
            require(
                attachment["agent_id"] in agent_map and attachment["attachment_id"] in attachments,
                "Unmapped retained attachment",
            )
            attachment["agent_id"] = agent_map[attachment["agent_id"]]
            attachment["attachment_id"] = attachments[attachment["attachment_id"]]
        return value

    rows, objects, retained, keys = [], [], [], set()
    for record in records:
        payload, family = record["payload"], record["family"]
        require(payload.get("format") == kind, "Mixed native source formats")
        if family == "evidence":
            body = base64.b64decode(payload["body_base64"], validate=True)
            require(sha(body) == payload["sha256"], "Import object checksum mismatch")
            prefix = "library/" if kind == "opencdr.native-library/1" else "cases/"
            require(payload["key"].startswith(prefix), "Import object prefix mismatch")
            objects.append(copy.deepcopy(payload))
            continue
        row = decoded(payload["item"])
        require(row.get("pk") != "OWNERSHIP", "Source ownership metadata cannot be imported")
        if family in {"job", "policy"}:
            retained.append(
                {"family": family, "id": record["id"], "sha256": sha(canonical(payload["item"]))}
            )
            continue  # Never activate source grants, connector bindings or jobs.
        require(family in {"library", "case", "history"}, "Unsupported native import family")
        pk, sk = row["pk"], row["sk"]
        require(record["id"] == identity(pk, sk), "Native row identity mismatch")
        if kind == "opencdr.native-cases/1":
            if pk == "CASE":
                row = case_view(row)
            elif pk.startswith("LINK#"):
                require(pk[5:] in attachments, "Unmapped active-case link")
                row["pk"] = "LINK#" + attachments[pk[5:]]
            elif pk.startswith("REQUEST#") and isinstance(row.get("result"), dict):
                row["result"] = case_view(row["result"])
            elif pk.startswith("EXTENSION#") and row.get("source", {}).get("kind") == "attachment":
                old = row["source"]["attachment_id"]
                require(old in attachments, "Unmapped indicator attachment")
                row["source"]["attachment_id"] = attachments[old]
        target = wire(row)
        key = (row["pk"], row["sk"])
        require(key not in keys, "Mapped storage key collision")
        keys.add(key)
        rows.append(
            {
                "item": target,
                "source_id": record["id"],
                "source_sha256": sha(canonical(payload["item"])),
                "target_sha256": sha(canonical(target)),
            }
        )
    return {
        "schema": SCHEMA,
        "source_manifest_sha256": manifest_sha256,
        "source": manifest["source"],
        "family": "library" if kind.endswith("library/1") else "cases",
        "agent_map": agent_map,
        "attachment_map": attachments,
        "rows": sorted(rows, key=lambda x: canonical(x["item"])),
        "objects": sorted(objects, key=lambda x: x["key"]),
        "retained_inert_records": retained,
        "activation": "forbidden",
        "source_fence_verified": False,
        "destination_verified": False,
        "historical_audit": "preserved_verbatim_with_explicit_identity_mapping",
    }
