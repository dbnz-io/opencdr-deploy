"""Bounded customer-runner wake-ups for an already reviewed owner routing policy.

SQLite persists the pending request before transport. Unknown results retain that
request; a later explicit invocation reconciles it through the owner's receipt.
No source cursor, event payload, schedule, grant or deployment is edited here.
"""

import json
import sqlite3
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from .core_api import CoreAPI
from .fleet_api import FleetAPI, FleetManagerError
from .workflows import identifier, load, validate_result


def configure(commands):
    parser = commands.add_parser(
        "routing-runner",
        help="Process bounded case-owner routing pages with durable local retry state",
    )
    parser.add_argument("--connection", type=Path, required=True)
    parser.add_argument("--mode", choices=["core", "fleet"], required=True)
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument(
        "--state",
        type=Path,
        required=True,
        help="Persistent SQLite state; preserve across cron/customer-runner invocations",
    )
    parser.add_argument("--max-batches", type=int, default=4)
    parser.add_argument("--reason", required=True)


def drain(client, *, mode, owner, approval_id, revision, agent, state, reason, max_batches=4):
    identifier(approval_id)
    identifier(agent, "agent" if mode == "fleet" else "owner")
    if (
        type(revision) is not int
        or revision < 1
        or type(max_batches) is not int
        or not 1 <= max_batches <= 10
        or not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= 1000
    ):
        raise FleetManagerError("Exact routing revision, reason and 1–10 batch bound required")
    binding = {
        "owner": owner,
        "mode": mode,
        "approval_id": approval_id,
        "revision": revision,
        "agent": agent,
        "reason": reason,
    }
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    connection = sqlite3.connect(state, timeout=0, isolation_level=None)
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), "
            "binding TEXT NOT NULL, pending TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS receipts "
            "(request_id TEXT PRIMARY KEY, result TEXT NOT NULL)"
        )

        def next_request():
            return json.dumps(
                {
                    "expected_rev": revision,
                    "request_id": uuid.uuid4().hex,
                    "reason": reason,
                    "agent_id": agent,
                },
                sort_keys=True,
            )

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO state VALUES (1, ?, ?)", (encoded, next_request())
        )
        actual = connection.execute("SELECT binding FROM state WHERE id=1").fetchone()[0]
        if actual != encoded:
            raise FleetManagerError(
                "Runner state belongs to another owner, policy or revision; "
                "preserve it and select another state file"
            )
        connection.execute("COMMIT")
        results = []
        deadline = time.monotonic() + 45
        for _ in range(max_batches):
            if time.monotonic() >= deadline:
                break
            connection.execute("BEGIN IMMEDIATE")
            request = json.loads(
                connection.execute("SELECT pending FROM state WHERE id=1").fetchone()[0]
            )
            # The pending request is already committed. Lock serializes concurrent
            # local runners; server receipts also prevent duplicated case effects.
            code, result = client.request(
                "POST",
                ("/v1" if mode == "fleet" else "") + "/reviews/" + approval_id + "/drain",
                request,
            )
            if code != 200:
                raise FleetManagerError(
                    f"Routing owner returned HTTP {code}; pending request retained; "
                    "no automatic retry"
                )
            if not isinstance(result, dict) or result.get("deployment") != owner:
                raise FleetManagerError("Routing owner identity mismatch; pending request retained")
            validate_result(SimpleNamespace(action="drain", id=approval_id), request, result)
            connection.execute(
                "INSERT INTO receipts VALUES (?,?)",
                (request["request_id"], json.dumps(result, sort_keys=True)),
            )
            connection.execute("UPDATE state SET pending=? WHERE id=1", (next_request(),))
            connection.execute("COMMIT")
            results.append(result["delivery"])
            if not result["delivery"]["has_more"]:
                break
        return {
            "owner": owner,
            "approval_id": approval_id,
            "agent_id": agent,
            "batches": len(results),
            "consumed": sum(x["consumed"] for x in results),
            "has_more": results[-1]["has_more"] if results else True,
            "state": str(state),
            "receipts": results,
        }
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


def run(args):
    config = load(args.connection)
    if args.mode == "core" and config.get("auth_mode") != "iam":
        raise FleetManagerError("Routing runner requires IAM")
    client = (CoreAPI if args.mode == "core" else FleetAPI)(config)
    owner = client.verify().get("deployment")
    if not isinstance(owner, dict):
        raise FleetManagerError("Owner identity unavailable")
    result = drain(
        client,
        mode=args.mode,
        owner=owner,
        approval_id=args.approval_id,
        revision=args.revision,
        agent=args.agent,
        state=args.state,
        reason=args.reason,
        max_batches=args.max_batches,
    )
    print(json.dumps(result, indent=2))
    return 0
