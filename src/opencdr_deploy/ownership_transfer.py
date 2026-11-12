"""Transactional native-owner handoff kernel.

AWS binding/runtime/lease verification is supplied by the deployment adapter on
EVERY operation. This module is not a standalone bypass or public mutation CLI.
It never copies authority, executes jobs, removes data stores or expires fences.
Cross-account activation is deliberately ordered: retire source permanently,
then activate target. An uncertain activation is reconciled, never undone.
"""

import base64
import copy
import re
from decimal import Decimal

from botocore.exceptions import ClientError

from . import native_import
from .library_archive import decoded
from .migration_archive import MAX_BYTES, MAX_RECORDS, canonical, require, sha, verify

SCHEMA = "opencdr.ownership-transfer/1"
OWNERSHIP = "opencdr.ownership/1"


def plain(value):
    if isinstance(value, Decimal):
        require(value == int(value), "Nonintegral migration state")
        return int(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def wire(row):
    return native_import.wire(row)


def key(row):
    return row["pk"]["S"], row["sk"]["S"]


def movable(row, family):
    pk = row["pk"]["S"]
    if family == "cases":
        return pk == "CASE" or pk.startswith(("AUDIT#", "REQUEST#", "LINK#", "EXTENSION#"))
    return pk != "OWNERSHIP"


def ordered(rows):
    return sorted(rows, key=canonical)


def state(family, status, revision, transfer):
    return wire(
        {
            "pk": "OWNERSHIP",
            "sk": family,
            "schema": OWNERSHIP,
            "state": status,
            "revision": revision,
            "transfer": transfer,
            "owner_transfer": transfer,
        }
    )


def condition(item):
    """Exact known attributes; table writes are additionally fenced by plan/epoch."""
    if item is None:
        return {"ConditionExpression": "attribute_not_exists(pk)"}
    names = {f"#v{i}": k for i, k in enumerate(sorted(item))}
    values = {f":v{i}": item[k] for i, k in enumerate(sorted(item))}
    return {
        "ConditionExpression": " AND ".join(f"#v{i} = :v{i}" for i in range(len(names))),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }


class Store:
    """Bound DynamoDB/S3 operations; guard must recheck physical pins and lease.

    Instantiation is internal. The public adapter must never accept a user-
    supplied Python callback or unverified resource names as migration authority.
    """

    def __init__(self, ddb, s3, pin, guard):
        self.ddb, self.s3, self.pin, self.guard = ddb, s3, copy.deepcopy(pin), guard
        self.table, self.bucket = pin["table"], pin["bucket"]

    def check(self):
        self.guard(self.pin)

    def get(self, pk, sk):
        return self.ddb.get_item(
            TableName=self.table, Key=wire({"pk": pk, "sk": sk}), ConsistentRead=True
        ).get("Item")

    def rows(self, family):
        result, cursor, markers, total = [], None, set(), 0
        while True:
            page = self.ddb.scan(
                TableName=self.table,
                ConsistentRead=True,
                Limit=100,
                **({"ExclusiveStartKey": cursor} if cursor else {}),
            )
            for row in page.get("Items", []):
                total += len(canonical(row))
                require(total <= MAX_BYTES, "Migration scan byte limit")
                if movable(row, family):
                    result.append(row)
            require(len(result) <= MAX_RECORDS, "Migration scan row limit")
            cursor = page.get("LastEvaluatedKey")
            if not cursor:
                return ordered(result)
            marker = canonical(cursor)
            require(marker not in markers, "Migration scan did not advance")
            markers.add(marker)

    def transaction(self, actions):
        self.check()
        self.ddb.transact_write_items(TransactItems=actions)

    def put(self, item, previous):
        return {"Put": {"TableName": self.table, "Item": item, **condition(previous)}}

    def fence(self, expected):
        return {
            "ConditionCheck": {
                "TableName": self.table,
                "Key": {k: expected[k] for k in ["pk", "sk"]},
                **condition(expected),
            }
        }

    def immutable_object(self, obj):
        self.check()
        body = base64.b64decode(obj["body_base64"], validate=True)
        require(sha(body) == obj["sha256"], "Object digest changed")
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=obj["key"],
                Body=body,
                ExpectedBucketOwner=self.pin["account_id"],
                IfNoneMatch="*",
                ServerSideEncryption="AES256",
                ContentType="application/json",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                raise
            self.verify_object(obj)

    def verify_object(self, obj):
        response = self.s3.get_object(
            Bucket=self.bucket, Key=obj["key"], ExpectedBucketOwner=self.pin["account_id"]
        )
        try:
            raw = response["Body"].read(1_000_001)
        finally:
            response["Body"].close()
        require(len(raw) <= 1_000_000 and sha(raw) == obj["sha256"], "Destination object conflict")


def _initial(value, expected_state):
    if value is None:
        require(expected_state == "active", "Retired owner marker is required")
        return 0
    v = decoded(value)
    require(
        v.get("schema") == OWNERSHIP
        and v.get("state") == expected_state
        and isinstance(v.get("revision"), (int, Decimal))
        and not isinstance(v["revision"], bool)
        and v["revision"] >= 1
        and v["revision"] == int(v["revision"]),
        "Invalid initial ownership marker",
    )
    return int(v["revision"])


def plan(source, target, family, agent_map, *, reason, retain_until, previous=None):
    require(family in {"cases", "library"}, "Unknown migration family")
    require(
        isinstance(reason, str) and 10 <= len(reason) <= 1000, "Concrete migration reason required"
    )
    require(type(retain_until) is int and retain_until > 0, "Explicit recovery retention required")
    require(source.pin["table_id"] != target.pin["table_id"], "Source and target must differ")
    source.check()
    target.check()
    si = source.get("OWNERSHIP", family)
    ti = target.get("OWNERSHIP", family)
    sr = _initial(si, "active")
    tr = _initial(ti, "retired" if previous else "active")
    target_rows = target.rows(family)
    if previous:
        require(
            previous["completion"]["phase"] == "complete" and previous["plan"]["family"] == family,
            "Recovery requires completed prior handoff",
        )
        parent = previous["plan"]
        parent_id = sha(canonical(parent))
        require(
            parent["source"] == target.pin and parent["target"] == source.pin,
            "Recovery owner pair differs",
        )
        require(
            decoded(si).get("owner_transfer") == parent_id
            and decoded(ti).get("owner_transfer") == parent_id
            and sr >= parent["target_revision"] + 2
            and tr >= parent["source_revision"] + 2,
            "Recovery epochs changed",
        )
        require(
            agent_map == {v: k for k, v in parent["agent_map"].items()}, "Recovery map must invert"
        )
        require(
            sha(canonical(target_rows)) == previous["completion"]["source_rows_sha256"],
            "Retained source data changed",
        )
        require(
            target.get("OWNERSHIP", "TRANSFER#" + parent_id) == wire(previous["completion"]),
            "Prior source completion is not authoritative",
        )
    else:
        require(not target_rows, "Destination family must be empty")
    return {
        "schema": SCHEMA,
        "source": source.pin,
        "target": target.pin,
        "family": family,
        "agent_map": agent_map,
        "reason": reason,
        "retain_until": retain_until,
        "source_initial": si,
        "target_initial": ti,
        "source_revision": sr,
        "target_revision": tr,
        "target_baseline": target_rows,
        "previous": sha(canonical(previous["plan"])) if previous else None,
    }


class Transfer:
    def __init__(self, source, target, reviewed_plan, digest):
        require(
            sha(canonical(reviewed_plan)) == digest and re.fullmatch("[0-9a-f]{64}", digest),
            "Migration plan digest mismatch",
        )
        require(
            reviewed_plan["schema"] == SCHEMA
            and source.pin == reviewed_plan["source"]
            and target.pin == reviewed_plan["target"],
            "Migration owner pins differ",
        )
        self.source, self.target, self.plan, self.id = (
            source,
            target,
            copy.deepcopy(reviewed_plan),
            digest,
        )
        self.family = reviewed_plan["family"]
        require(self.family in {"cases", "library"}, "Invalid family")
        self.sk = "TRANSFER#" + digest
        self.sf = state(self.family, "frozen", reviewed_plan["source_revision"] + 1, digest)
        self.tf = state(self.family, "frozen", reviewed_plan["target_revision"] + 1, digest)
        self.sr = state(self.family, "retired", reviewed_plan["source_revision"] + 2, digest)
        self.ta = state(self.family, "active", reviewed_plan["target_revision"] + 2, digest)

    def journal(self):
        value = self.source.get("OWNERSHIP", self.sk)
        if value is None:
            return None
        j = plain(decoded(value))
        require(j["plan_sha256"] == self.id, "Durable migration plan changed")
        return j

    def both(self):
        self.source.check()
        self.target.check()

    def frozen(self):
        self.both()
        require(self.source.get("OWNERSHIP", self.family) == self.sf, "Source is not frozen")
        require(self.target.get("OWNERSHIP", self.family) == self.tf, "Target is not frozen")

    def _advance(self, prior, phase, actions=(), **extra):
        updated = {**prior, "phase": phase, **extra}
        self.source.transaction([*actions, self.source.put(wire(updated), wire(prior))])
        return updated

    def freeze(self):
        self.both()
        j = self.journal()
        if j is None:
            j = {"pk": "OWNERSHIP", "sk": self.sk, "plan_sha256": self.id, "phase": "planned"}
            self.source.transaction([self.source.put(wire(j), None)])
        if j["phase"] == "planned":
            j = self._advance(
                j, "source_frozen", [self.source.put(self.sf, self.plan["source_initial"])]
            )
        if j["phase"] == "source_frozen":
            self.both()
            require(self.source.get("OWNERSHIP", self.family) == self.sf, "Source freeze changed")
            if self.target.get("OWNERSHIP", self.family) != self.tf:
                self.target.transaction([self.target.put(self.tf, self.plan["target_initial"])])
            # Recheck an initially empty family after atomically fencing its writers.
            require(
                self.target.rows(self.family) == self.plan["target_baseline"],
                "Destination changed before its fence; requires reviewed recovery",
            )
            j = self._advance(j, "frozen")
        require(j["phase"] in {"frozen", "staging", "source_retired", "complete"}, "Unknown phase")
        return j

    def cancel_before_import(self):
        """Cancel before staging only; increment both epochs to defeat late freezes."""
        self.both()
        j = self.journal()
        require(
            j and j["phase"] in {"planned", "source_frozen", "frozen", "cancelling", "cancelled"},
            "Once staging starts, finish the transfer and use reviewed reverse recovery",
        )
        if j["phase"] == "cancelled":
            return j
        if j["phase"] != "cancelling":
            j = self._advance(j, "cancelling")
        active_target = state(self.family, "active", self.plan["target_revision"] + 2, self.id)
        # A cancelled reverse transfer restores its previous retired destination.
        if self.plan["previous"]:
            active_target = state(self.family, "retired", self.plan["target_revision"] + 2, self.id)
        active_target["owner_transfer"] = (self.plan["target_initial"] or {}).get(
            "owner_transfer", {"NULL": True}
        )
        current = self.target.get("OWNERSHIP", self.family)
        if current != active_target:
            require(
                current in [self.plan["target_initial"], self.tf], "Cancellation target changed"
            )
            self.target.transaction([self.target.put(active_target, current)])
        active_source = state(self.family, "active", self.plan["source_revision"] + 2, self.id)
        active_source["owner_transfer"] = (self.plan["source_initial"] or {}).get(
            "owner_transfer", {"NULL": True}
        )
        current = self.source.get("OWNERSHIP", self.family)
        require(current in [self.plan["source_initial"], self.sf], "Cancellation source changed")
        return self._advance(j, "cancelled", [self.source.put(active_source, current)])

    def material(self, directory, manifest_sha256):
        preview = native_import.prepare(directory, manifest_sha256, self.plan["agent_map"])
        require(preview["family"] == self.family, "Archive family differs")
        manifest, records = verify(directory, manifest_sha256)
        src = manifest["source"]
        require(
            all(
                src.get(k) == self.source.pin.get(k)
                for k in ["account_id", "region", "stack_id", "table", "table_id", "bucket"]
            ),
            "Archive source identity differs",
        )
        observation = src.get("ownership_observations", {}).get(self.family)
        require(observation == self.sf, "Archive does not retain exact source family fence")
        raw = ordered(
            [
                r["payload"]["item"]
                for r in records
                if r["family"] != "evidence" and movable(r["payload"]["item"], self.family)
            ]
        )
        require(self.source.rows(self.family) == raw, "Source final delta changed")
        rows = [r["item"] for r in preview["rows"]]
        if self.plan["previous"]:
            _preserves_history(self.plan["target_baseline"], rows, self.family)
        return preview, raw

    def stage(self, directory, manifest_sha256):
        j = self.journal()
        require(j and j["phase"] in {"frozen", "staging"}, "Freeze before import")
        self.frozen()
        preview, raw = self.material(directory, manifest_sha256)
        material_sha = sha(canonical(preview))
        baseline = {key(r): r for r in self.plan["target_baseline"]}
        expected = {key(r["item"]): r["item"] for r in preview["rows"]}
        if j["phase"] == "frozen":
            j = self._advance(
                j,
                "staging",
                manifest_sha256=manifest_sha256,
                material_sha256=material_sha,
                source_rows_sha256=sha(canonical(raw)),
            )
        require(
            j["manifest_sha256"] == manifest_sha256 and j["material_sha256"] == material_sha,
            "Different archive cannot resume this import",
        )
        for obj in preview["objects"]:
            self.frozen()
            self.target.immutable_object(obj)
        actual = {key(r): r for r in self.target.rows(self.family)}
        require(set(actual) <= set(expected) | set(baseline), "Unexpected destination row")
        for k, row in actual.items():
            require(
                row == expected.get(k) or row == baseline.get(k),
                "Destination changed during staging",
            )
        actions = []
        for k in sorted(set(actual) | set(expected)):
            current, desired = actual.get(k), expected.get(k)
            if current == desired:
                continue
            if desired is None:
                require(k[0].startswith("LINK#"), "Recovery cannot discard retained history")
                actions.append(
                    {
                        "Delete": {
                            "TableName": self.target.table,
                            "Key": {f: current[f] for f in ["pk", "sk"]},
                            **condition(current),
                        }
                    }
                )
            else:
                actions.append(self.target.put(desired, current))
            if len(actions) == 25 or len(canonical(actions)) >= 2_500_000:
                self.frozen()
                self.target.transaction([self.target.fence(self.tf), *actions])
                actions = []
        if actions:
            self.frozen()
            self.target.transaction([self.target.fence(self.tf), *actions])
        self.frozen()
        require(
            self.target.rows(self.family) == ordered(expected.values()),
            "Imported row verification failed",
        )
        for obj in preview["objects"]:
            self.target.verify_object(obj)
        marker = wire(
            {
                "pk": "OWNERSHIP",
                "sk": self.sk,
                "material_sha256": material_sha,
                "phase": "verified",
                "source": self.source.pin,
                "retain_until": self.plan["retain_until"],
            }
        )
        prior = self.target.get("OWNERSHIP", self.sk)
        if prior != marker:
            require(prior is None, "Destination transfer marker conflicts")
            self.target.transaction([self.target.fence(self.tf), self.target.put(marker, None)])
        return {
            **j,
            "destination_verified": True,
            "imported_rows": len(expected),
            "objects": len(preview["objects"]),
        }

    def activate(self, directory, manifest_sha256):
        j = self.journal()
        require(
            j and j["phase"] in {"staging", "source_retired", "complete"},
            "Verified import required",
        )
        require(j["manifest_sha256"] == manifest_sha256, "Activation archive differs")
        material = native_import.prepare(directory, manifest_sha256, self.plan["agent_map"])
        require(sha(canonical(material)) == j["material_sha256"], "Activation material changed")
        if j["phase"] == "complete":
            return j
        if j["phase"] == "staging":
            self.stage(directory, manifest_sha256)
            self.frozen()
            j = self._advance(j, "source_retired", [self.source.put(self.sr, self.sf)])
        # There is deliberately NO path that un-retires the source after here.
        self.both()
        require(self.source.get("OWNERSHIP", self.family) == self.sr, "Source retirement changed")
        marker = self.target.get("OWNERSHIP", self.sk)
        require(
            marker
            and decoded(marker).get("material_sha256") == j["material_sha256"]
            and decoded(marker).get("phase") == "verified",
            "Verified destination marker missing",
        )
        if self.target.get("OWNERSHIP", self.family) != self.ta:
            self.target.transaction([self.target.fence(marker), self.target.put(self.ta, self.tf)])
        return self._advance(j, "complete")


def _preserves_history(baseline, rows, family):
    target = {key(r): r for r in rows}
    for old in baseline:
        k = key(old)
        new = target.get(k)
        if k[0].startswith("LINK#"):
            continue
        require(new is not None, "Recovery would discard retained history")
        if k[0] == "CASE":
            require(int(new["rev"]["N"]) >= int(old["rev"]["N"]), "Recovery rewinds case revision")
        elif k[0] == "ITEM":
            require(
                int(new["latest_version"]["N"]) >= int(old["latest_version"]["N"]),
                "Recovery rewinds library version",
            )
        elif k[0].startswith("EXTENSION#") and family == "cases":
            pass  # Mutable indicator/provider state; audit remains immutable.
        else:
            require(old == new, "Recovery rewrites immutable history")
