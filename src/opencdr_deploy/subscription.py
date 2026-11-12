"""Explicit, immutable initialization of a NEW notification feed subscription.

No existing consumer reset, activation fence, lease or schedule is changed.
The caller must use the same service IAM principal as the notification worker.
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .core_api import CoreAPI
from .fleet_api import FleetAPI, FleetManagerError
from .workflows import identifier, load


def configure(commands):
    parser = commands.add_parser("subscription", help="Review/init a new notification subscription")
    parser.add_argument("action", choices=["plan", "apply"])
    parser.add_argument("--connection", type=Path, required=True)
    parser.add_argument("--mode", choices=["core", "fleet"], required=True)
    parser.add_argument("--agent")
    parser.add_argument(
        "--subscription", help="Exact organization-id:deployment-id used by hosted worker"
    )
    parser.add_argument(
        "--initial-cursor", help="Explicit reviewed source checkpoint; never inferred"
    )
    parser.add_argument(
        "--not-before", help="Explicit notification cutover timestamp, including timezone"
    )
    parser.add_argument("--reason")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument(
        "--sha256", help="Exact SHA256 of the reviewed plan bytes, required to apply"
    )


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(UTC)
    except (ValueError, AttributeError, TypeError) as error:
        raise FleetManagerError("A timezone-aware timestamp is required") from error


def binding(client, mode, agent):
    owner = client.verify()["deployment"]
    caller = client.session.client("sts").get_caller_identity()
    principal = caller["Account"] + ":" + caller["UserId"].split(":", 1)[0]
    if not re.fullmatch(r"[0-9]{12}:(AROA|AIDA)[A-Z0-9]{12,64}", principal):
        raise FleetManagerError("A stable service IAM principal is required")
    result = {"mode": mode, "owner": owner, "principal": principal}
    if mode == "fleet":
        identifier(agent, "agent")
        code, value = client.request("GET", "/v1/agents/" + agent)
        if code != 200 or value.get("agent", {}).get("agent_id") != agent:
            raise FleetManagerError("Fleet registration identity unavailable")
        registration = value["agent"]
        if not registration.get("enabled") or registration.get("auth_mode") != "iam":
            raise FleetManagerError("An enabled IAM registration is required")
        result.update(
            agent=agent,
            registration_rev=registration["rev"],
            source={
                "account_id": registration["account_id"],
                "home_region": registration["region"],
                "stage": registration["stage"],
            },
        )
    else:
        if agent:
            raise FleetManagerError("Core initialization cannot select another agent")
        result["source"] = owner
    return result


def route(selected, consumer):
    return (
        "/v1/agents/" + selected["agent"] + "/delivery/"
        if selected["mode"] == "fleet"
        else "/detections/consumers/"
    ) + consumer


def make_plan(client, mode, agent, subscription, cursor, not_before, reason):
    if (
        not isinstance(subscription, str)
        or not 1 <= len(subscription) <= 256
        or not isinstance(cursor, str)
        or not 1 <= len(cursor) <= 4096
        or not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= 1000
    ):
        raise FleetManagerError("Exact subscription, initial cursor and reason are required")
    cutover = timestamp(not_before)
    now = datetime.now(UTC)
    if cutover > now:
        raise FleetManagerError("Capture a cutover timestamp before preparing the plan")
    selected = binding(client, mode, agent)
    consumer = "notifications-" + hashlib.sha256(subscription.encode()).hexdigest()[:40]
    code, _ = client.request("GET", route(selected, consumer))
    if code != 404:
        raise FleetManagerError(
            "New consumer required; existing or unavailable consumer not changed"
        )
    return {
        "schema_version": "1.0.0",
        "kind": "notification_subscription_initialization",
        "binding": selected,
        "subscription": subscription,
        "consumer_id": consumer,
        "initial_cursor": cursor,
        "not_before": cutover.isoformat(),
        "reason": reason,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "history_complete": False,
        "schedules_changed": False,
    }


def apply_plan(client, mode, agent, plan, sha):
    if (
        plan.get("schema_version") != "1.0.0"
        or plan.get("kind") != "notification_subscription_initialization"
        or not re.fullmatch(r"[a-f0-9]{64}", sha or "")
    ):
        raise FleetManagerError("Invalid reviewed subscription plan")
    if timestamp(plan["expires_at"]) <= datetime.now(UTC):
        raise FleetManagerError("Subscription plan expired; no consumer changed")
    if binding(client, mode, agent) != plan["binding"]:
        raise FleetManagerError("Service principal, owner or registration changed; replan")
    expected = "notifications-" + hashlib.sha256(plan["subscription"].encode()).hexdigest()[:40]
    if expected != plan["consumer_id"]:
        raise FleetManagerError("Subscription identity mismatch")
    path = route(plan["binding"], expected)
    code, value = client.request("GET", path)
    if code == 404:
        code, value = client.request(
            "POST",
            path,
            {
                "initial_cursor": plan["initial_cursor"],
                "checkpoint_sha256": sha,
            },
        )
    if code != 200:
        raise FleetManagerError(f"Subscription initialization HTTP {code}; inspect before retrying")
    if (
        value.get("consumer_id") != expected
        or value.get("initial_cursor") != plan["initial_cursor"]
        or value.get("checkpoint_sha256") != sha
        or value.get("principal") != "aws:" + plan["binding"]["principal"]
    ):
        raise FleetManagerError("Subscription receipt mismatch; never reset this consumer")
    return {
        "applied": True,
        "consumer": value,
        "plan_sha256": sha,
        "not_before": plan["not_before"],
        "history_complete": False,
        "notification_delivery_enabled": False,
    }


def run(args):
    config = load(args.connection)
    if config.get("identity"):
        raise FleetManagerError("Initialization requires the notification service IAM principal")
    if args.mode == "core":
        if config.get("auth_mode") != "iam":
            raise FleetManagerError("Initialization requires IAM")
        config = {**config, "access": "delivery"}
    client = (CoreAPI if args.mode == "core" else FleetAPI)(config)
    if args.action == "plan":
        plan = make_plan(
            client,
            args.mode,
            args.agent,
            args.subscription,
            args.initial_cursor,
            args.not_before,
            args.reason,
        )
        with args.plan.open("x") as stream:
            stream.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        print(
            json.dumps(
                {
                    "plan": str(args.plan),
                    "sha256": hashlib.sha256(args.plan.read_bytes()).hexdigest(),
                    "applied": False,
                }
            )
        )
    else:
        raw = args.plan.read_bytes()
        if len(raw) > 262144 or hashlib.sha256(raw).hexdigest() != args.sha256:
            raise FleetManagerError("Reviewed plan SHA256 mismatch")
        plan = json.loads(raw)
        print(json.dumps(apply_plan(client, args.mode, args.agent, plan, args.sha256), indent=2))
    return 0
