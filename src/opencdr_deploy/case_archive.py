"""Inert export of native cases and retained execution/authority history.

No exported grant, connector, policy, job or permanent claim becomes authority.
Native imports and ownership transfer require their own reviewed controller.
"""

import base64
import json

from .library_archive import decoded, identity
from .migration_archive import MAX_BYTES, MAX_RECORDS, canonical, require, sha

FORMAT = "opencdr.native-cases/1"


def native_source(owner, items, objects):
    require(len(items) <= MAX_RECORDS, "Case row limit exceeded")
    rows, wires = {}, {}
    for item in items:
        row = decoded(item)
        key = (row.get("pk"), row.get("sk"))
        require(all(isinstance(k, str) and k for k in key), "Invalid case storage key")
        require(key not in rows, "Duplicate case storage key")
        rows[key], wires[key] = row, item
    heads = {sk: row for (pk, sk), row in rows.items() if pk == "CASE"}
    expected_locks, blobs, edges = {}, {}, {}

    def case_ref(case_id):
        require(case_id in heads, "Missing related case")
        return {"family": "case", "id": identity("CASE", case_id)}

    for case_id, head in heads.items():
        require(
            head.get("case_id") == case_id
            and isinstance(head.get("agent_ids"), list)
            and head["agent_ids"],
            "Invalid native case identity",
        )
        refs = []
        revision = head.get("rev")
        require(
            revision is not None and 1 <= revision <= MAX_RECORDS and revision == int(revision),
            "Invalid native case revision",
        )
        for rev in range(1, int(revision) + 1):
            key = ("AUDIT#" + case_id, f"{rev:012d}")
            require(key in rows and rows[key].get("rev") == rev, "Native case audit gap")
            refs.append({"family": "history", "id": identity(*key)})
        attached = set()
        for attachment in head.get("attachments", []):
            aid = attachment["attachment_id"]
            require(aid not in attached, "Duplicate native attachment")
            attached.add(aid)
            require(
                attachment["agent_id"] in head["agent_ids"]
                and aid
                == sha(
                    canonical(
                        {
                            "agent_id": attachment["agent_id"],
                            "detection_id": attachment["detection_id"],
                        }
                    )
                ),
                "Native attachment identity mismatch",
            )
            key, expected = attachment["evidence_key"], attachment["evidence_sha256"]
            require(key.startswith("cases/") and key in objects, "Missing case evidence")
            raw = objects[key]
            require(
                isinstance(raw, bytes) and len(raw) <= 1_000_000 and sha(raw) == expected,
                "Native case evidence checksum mismatch",
            )
            body = json.loads(raw)
            require(
                body.get("detection_id") == attachment["detection_id"],
                "Native case evidence identity mismatch",
            )
            bid = sha(key.encode())
            blobs[bid] = {
                "family": "evidence",
                "id": bid,
                "references": [],
                "payload": {
                    "format": FORMAT,
                    "key": key,
                    "sha256": expected,
                    "body_base64": base64.b64encode(raw).decode(),
                },
            }
            refs.append({"family": "evidence", "id": bid})
            if attachment["attached"] and head["status"] != "closed":
                lock = ("LINK#" + aid, "ACTIVE")
                require(lock not in expected_locks, "Multiple active cases for one detection")
                expected_locks[lock] = case_id
                require(
                    lock in rows and rows[lock].get("case_id") == case_id,
                    "Missing or incorrect active-case lock",
                )
                refs.append({"family": "history", "id": identity(*lock)})
            if attachment.get("origin_case_id"):
                refs.append(case_ref(attachment["origin_case_id"]))
        for field in ["merged_into", "duplicate_of"]:
            if head.get(field):
                refs.append(case_ref(head[field]))
        refs.extend(case_ref(r["case_id"]) for r in head.get("related_cases", []))
        edges[("CASE", case_id)] = refs
    for key, row in rows.items():
        if key[0].startswith("LINK#"):
            require(expected_locks.get(key) == row.get("case_id"), "Unexpected active-case lock")
    records, unresolved = [], []
    for key, row in sorted(rows.items()):
        pk, sk = key
        refs = list(edges.get(key, []))
        if pk == "OWNERSHIP":
            continue
        if pk == "CASE":
            family = "case"
        elif pk.startswith(("AUDIT#", "REQUEST#", "LINK#", "EXTENSION#")):
            family = "history"
            cid = row.get("case_id")
            if pk.startswith(("AUDIT#", "REQUEST#")):
                cid = pk.split("#", 1)[1]
            if pk.startswith("EXTENSION#"):
                cid = pk.split("#")[1]
                require(row.get("case_id") == cid, "Native extension case mismatch")
                source = row.get("source", {})
                if source.get("kind") == "attachment":
                    require(
                        cid in heads
                        and any(
                            a["attachment_id"] == source["attachment_id"]
                            for a in heads[cid]["attachments"]
                        ),
                        "Native extension attachment missing",
                    )
            refs.append(case_ref(cid))
        elif pk.startswith(
            ("OUTCOME", "EFFECT#", "REPLAY_EFFECT#", "REVIEW_EXECUTION#", "REVIEW#")
        ):
            family = "job"
        elif pk in {"ACCESS", "INTEGRATION", "IDENTITY_AUTHORITY"} or pk.startswith(
            ("REVIEW_", "ACCESS_", "INTEGRATION_", "ROUTING", "IDENTITY_")
        ):
            family = "policy"
        else:
            unresolved.append({"reason": "unknown_native_case_partition", "item": wires[key]})
            continue
        records.append(
            {
                "family": family,
                "id": identity(*key),
                "payload": {"format": FORMAT, "item": wires[key], "execution_authorized": False},
                "references": sorted({canonical(r): r for r in refs}.values(), key=canonical),
            }
        )
    result = {
        "source": {
            **owner,
            "format": FORMAT,
            "writer_fence_attested": False,
            "ownership_observations": {sk: wires[(pk, sk)] for pk, sk in rows if pk == "OWNERSHIP"},
            "object_scope": "current_case_attachment_objects_only",
            "authority_import": "forbidden",
            "worker_replay": "forbidden",
        },
        "identity_map": {cid: cid for cid in heads},
        "records": [*records, *blobs.values()],
        "unresolved": unresolved,
    }
    require(len(canonical(result)) <= MAX_BYTES, "Native case archive byte limit exceeded")
    return result
