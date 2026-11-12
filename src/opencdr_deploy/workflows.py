"""Guided owner reviews and jobs over the existing verified IAM transports."""

import json
import re
from pathlib import Path
from urllib.parse import urlencode

from .core_api import CoreAPI
from .fleet_api import FleetAPI, FleetManagerError

READS = {
    "inbox",
    "review",
    "receipt",
    "verify",
    "activation",
    "groups",
    "group",
    "job",
    "policy",
    "flags",
    "ownership",
    "grant",
    "indicators",
    "provider-history",
}
ACTIONS = sorted(
    READS
    | {
        "configure-policy",
        "configure-grant",
        "curate-indicator",
        "import-provider-history",
        "propose",
        "approve",
        "reject",
        "withdraw",
        "execute",
        "cancel",
        "activate",
        "drain",
        "tuning-draft",
        "resolve-group",
        "plan-distribution",
        "prepare-distribution",
        "apply-distribution",
        "cancel-distribution",
        "reconcile-target",
    }
)


def configure(commands):
    parser = commands.add_parser(
        "workflow", help="owner reviews, group selections and durable jobs; no automatic retries"
    )
    parser.add_argument(
        "--connection",
        required=True,
        type=Path,
        help="IAM Core connection or Fleet connection JSON",
    )
    parser.add_argument("--mode", required=True, choices=["core", "fleet"])
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--id", help="exact review, group or job ID")
    parser.add_argument(
        "--subject", help="exact stable AWS or registered human principal for grant administration"
    )
    parser.add_argument("--agent", help="exact agent ID for read-only reconciliation")
    parser.add_argument(
        "--file", type=Path, help="exact JSON request; retain unchanged for reconciliation"
    )
    parser.add_argument("--after", help="owner-issued cursor for inbox or groups")
    parser.add_argument("--limit", type=int, default=20)


def load(path):
    with path.open("rb") as stream:
        data = stream.read(262145)
    if len(data) > 262144:
        raise FleetManagerError("Workflow JSON exceeds 256 KiB")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise FleetManagerError("Workflow JSON must be an object")
    return value


def identifier(value, kind="review"):
    pattern = {
        "review": r"[a-f0-9]{32}",
        "owner": r"[0-9]{12}:[a-z0-9-]+:[A-Za-z0-9_-]+",
        "group": r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}",
        "agent": r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
        "job": r"(operator|responder|administrator)-[a-f0-9]{32}",
    }[kind]
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise FleetManagerError("An exact " + kind + " ID is required")
    return value


def fields(body, required, *, initial=False):
    if not isinstance(body, dict) or set(body) != set(required):
        raise FleetManagerError("Request fields must be exactly: " + ", ".join(sorted(required)))
    if "request_id" in body:
        identifier(body["request_id"])
        if not isinstance(body["reason"], str) or not 1 <= len(body["reason"].strip()) <= 1000:
            raise FleetManagerError("A nonblank reason up to 1000 characters is required")
    if "expected_rev" in body and (
        type(body["expected_rev"]) is not int or body["expected_rev"] < (0 if initial else 1)
    ):
        raise FleetManagerError("An exact positive revision is required")


def request(args):
    action = args.action
    if action not in ACTIONS:
        raise FleetManagerError("Unknown workflow")
    fleet_only = {
        "groups",
        "group",
        "job",
        "resolve-group",
        "plan-distribution",
        "prepare-distribution",
        "apply-distribution",
        "cancel-distribution",
        "reconcile-target",
    }
    if action in fleet_only and args.mode != "fleet":
        raise FleetManagerError("This workflow requires Fleet Manager")
    if args.subject and action not in {"grant", "configure-grant"}:
        raise FleetManagerError("Only grant administration accepts a subject")
    if action in {"grant", "configure-grant"} and (
        not isinstance(args.subject, str)
        or not re.fullmatch(
            r"(?:human:[a-f0-9]{64}|aws(?:-us-gov|-cn)?:[0-9]{12}:(?:AROA|AIDA)[A-Z0-9]{12,64})",
            args.subject,
        )
    ):
        raise FleetManagerError("An exact stable grant subject is required")
    if args.after is not None and (
        action not in {"inbox", "groups", "indicators", "provider-history"}
        or not 1 <= len(args.after) <= 4096
    ):
        raise FleetManagerError("Only bounded owner list cursors are accepted")
    if args.agent and action != "reconcile-target":
        raise FleetManagerError("Only reconciliation accepts an agent selector")
    if args.id and action in {
        "inbox",
        "groups",
        "policy",
        "flags",
        "ownership",
        "propose",
        "plan-distribution",
        "grant",
        "configure-policy",
        "configure-grant",
    }:
        raise FleetManagerError("This workflow does not accept a record ID")
    if not 1 <= args.limit <= 50:
        raise FleetManagerError("Page limit must be between 1 and 50")
    body = load(args.file) if args.file else None
    if action in READS:
        if body is not None:
            raise FleetManagerError("Read workflows do not accept a body")
        if action in {"inbox", "groups"}:
            path = (
                ("/reviews" if action == "inbox" else "/groups")
                + "?"
                + urlencode({"limit": args.limit, **({"after": args.after} if args.after else {})})
            )
        elif action in {"indicators", "provider-history"}:
            path = (
                "/cases/"
                + identifier(args.id)
                + "/extensions/"
                + ("indicators" if action == "indicators" else "provider-results")
            )
            if args.after:
                path += "?" + urlencode({"after": args.after})
        elif action == "grant":
            path = "/review-grants/" + args.subject
        elif action == "policy":
            path = "/review-policy"
        elif action in {"flags", "ownership"}:
            if action == "ownership" and args.mode != "core":
                raise FleetManagerError("Runtime content ownership is configured at Core")
            path = "/configuration/" + action
        elif action == "group":
            path = "/groups/" + identifier(args.id, "group")
        elif action == "job":
            path = "/rollouts/" + identifier(args.id, "job")
        else:
            path = (
                "/reviews/"
                + identifier(args.id)
                + (
                    {
                        "review": "",
                        "receipt": "/execution",
                        "verify": "/verification",
                        "activation": "/activation",
                    }[action]
                )
            )
        return "GET", path, None
    if action in {"configure-policy", "configure-grant"}:
        value = "policy" if action == "configure-policy" else "grant"
        fields(body, {value, "expected_rev", "request_id", "reason"}, initial=True)
        if not isinstance(body[value], dict):
            raise FleetManagerError("An exact owner " + value + " document is required")
        return (
            "PUT",
            "/review-policy" if value == "policy" else "/review-grants/" + args.subject,
            body,
        )
    if action in {"curate-indicator", "import-provider-history"}:
        extra = (
            {"indicator", "relationship", "note", "source"}
            if action == "curate-indicator"
            else {"provider", "reference", "source_sha256", "observed_at", "result", "indicator"}
        )
        fields(body, {"expected_rev", "request_id", "reason"} | extra)
        return (
            "POST",
            "/cases/"
            + identifier(args.id)
            + "/extensions/"
            + ("indicators" if action == "curate-indicator" else "provider-results"),
            body,
        )
    if action == "tuning-draft":
        fields(
            body,
            {
                "expected_rev",
                "request_id",
                "reason",
                "case_rev",
                "attachment_id",
                "source",
                "document",
            },
        )
        path = "/cases/" + identifier(args.id) + "/tuning-draft"
    elif action == "propose":
        fields(body, {"operation", "expires_at", "request_id", "reason"})
        if (
            not isinstance(body["operation"], dict)
            or type(body["expires_at"]) is not int
            or body["expires_at"] <= 0
        ):
            raise FleetManagerError("An operation object and explicit expiry are required")
        path = "/reviews"
    elif action in {"approve", "reject", "withdraw", "execute", "cancel", "activate", "drain"}:
        required = {"expected_rev", "request_id", "reason"}
        if action == "drain":
            required.add("agent_id")
            if isinstance(body, dict):
                identifier(body.get("agent_id"), "agent" if args.mode == "fleet" else "owner")
        if action in {"approve", "reject"}:
            required |= {"mode", "approve"}
        fields(body, required)
        if action in {"approve", "reject"} and (
            body["approve"] is not (action == "approve")
            or body["mode"] not in {"peer", "self_exception", "emergency"}
        ):
            raise FleetManagerError("Decision and mode must match the selected action")
        path = (
            "/reviews/"
            + identifier(args.id)
            + "/"
            + {"approve": "decisions", "reject": "decisions", "withdraw": "withdrawals"}.get(
                action, action
            )
        )
    elif action == "resolve-group":
        fields(body, {"expected_rev"})
        path = "/groups/" + identifier(args.id, "group") + "/resolve"
    elif action == "plan-distribution":
        required = {"targets", "request_id", "reason"}
        if isinstance(body, dict) and "allocation" in body:
            required.add("allocation")
            fields(body["allocation"], {"group_id", "expected_rev"})
            identifier(body["allocation"]["group_id"], "group")
        fields(body, required)
        if not isinstance(body["targets"], list) or not 1 <= len(body["targets"]) <= 100:
            raise FleetManagerError("Select 1 to 100 exact targets")
        for target in body["targets"]:
            fields(target, {"agent_id", "approval_id", "expected_rev"})
            identifier(target["agent_id"], "agent")
            identifier(target["approval_id"])
        path = "/rollouts/plan"
    else:
        path = (
            "/rollouts/"
            + identifier(args.id, "job")
            + "/"
            + {
                "prepare-distribution": "prepare",
                "apply-distribution": "apply",
                "cancel-distribution": "cancel",
                "reconcile-target": "reconcile",
            }[action]
        )
        if action == "apply-distribution":
            fields(body, {"approval_id", "expected_rev", "request_id", "reason"})
            identifier(body["approval_id"])
        elif action == "cancel-distribution":
            fields(body, {"review_digest", "request_id", "reason"})
            if not isinstance(body["review_digest"], str) or not re.fullmatch(
                r"[a-f0-9]{64}", body["review_digest"]
            ):
                raise FleetManagerError("An exact reviewed digest is required")
        else:
            body = {} if body is None else body
            fields(body, set())
            if action == "reconcile-target":
                path += "/" + identifier(args.agent, "agent")
    return "POST", path, body


def validate_result(args, body, result):
    """A matching owner alone cannot confirm a different review or request."""
    action = args.action

    def confirmed(condition):
        if not condition:
            raise ValueError("receipt mismatch")

    try:
        if action in {"configure-policy", "configure-grant"}:
            value = "policy" if action == "configure-policy" else "grant"
            revision = "rev" if value == "policy" else "revision"
            confirmed(result[value][revision] == body["expected_rev"] + 1)
        elif action in {"review", "approve", "reject", "withdraw"}:
            row = result["review"]
            confirmed(row["approval_id"] == args.id)
            if action != "review":
                confirmed(row["rev"] == body["expected_rev"] + 1)
        elif action in {"execute", "cancel", "receipt"}:
            row = result["execution"]
            if row is not None or action != "receipt":
                confirmed(row["approval_id"] == args.id)
                if action == "execute":
                    confirmed(row["request_id"] == body["request_id"])
                    confirmed(row["approval_rev"] == body["expected_rev"])
                if action == "cancel":
                    confirmed(row["state"] == "cancelled")
        elif action == "activate":
            row = result["activation"]
            confirmed(row["request_id"] == body["request_id"])
            identifier(row["policy_id"])
            confirmed(type(row["revision"]) is int and row["revision"] > 0)
            confirmed(type(result["worker_connected"]) is bool)
        elif action == "drain":
            row = result["delivery"]
            confirmed(row["approval_id"] == args.id and row["request_id"] == body["request_id"])
            confirmed(row["agent_id"] == body["agent_id"])
            confirmed(type(row["consumed"]) is int and 0 <= row["consumed"] <= 20)
            confirmed(type(row["has_more"]) is bool)
        elif action in {"curate-indicator", "import-provider-history"}:
            confirmed(
                result["case"]["case_id"] == args.id
                and result["case"]["rev"] == body["expected_rev"] + 1
            )
        elif action in {"indicators", "provider-history"}:
            confirmed(result["case_id"] == args.id and result["execution_authorized"] is False)
            confirmed(isinstance(result["items"], list) and len(result["items"]) <= 20)
        elif action == "tuning-draft":
            confirmed(result["case"]["case_id"] == args.id)
            confirmed(result["case"]["rev"] == body["case_rev"] + 1)
            confirmed(result["draft_enabled"] is False and result["runtime_changed"] is False)
        elif action == "resolve-group":
            confirmed(result["group_id"] == args.id and result["group_rev"] == body["expected_rev"])
            confirmed(result["dispatch_allowed"] is False)
        elif action == "group":
            confirmed(result["group"]["group_id"] == args.id)
    except (ValueError, KeyError, TypeError, FleetManagerError):
        raise FleetManagerError(
            "Owner response receipt mismatch; inspect the retained request before retrying"
        ) from None


def run(args):
    method, path, body = request(args)  # Validate every local field before any network call.
    config = load(args.connection)
    if args.mode == "core" and config.get("auth_mode") != "iam":
        raise FleetManagerError("Owner workflows require an IAM Core connection")
    if args.action in {"policy", "grant", "configure-policy", "configure-grant"} and (
        config.get("access") != "administrator" or config.get("identity") is not None
    ):
        raise FleetManagerError(
            "Policy and grants require an explicit direct IAM administrator connection; "
            "delegated web identities cannot administer them"
        )
    client = CoreAPI(config) if args.mode == "core" else FleetAPI(config)
    status = client.verify()
    code, result = client.request(method, ("/v1" if args.mode == "fleet" else "") + path, body)
    if 200 <= code < 300 and (
        not isinstance(result, dict) or result.get("deployment") != status.get("deployment")
    ):
        raise FleetManagerError(
            "Owner response identity mismatch; inspect the retained receipt before retrying"
        )
    if 200 <= code < 300:
        validate_result(args, body, result)
    print(json.dumps({"http_status": code, "result": result}, indent=2, default=str))
    return 0 if 200 <= code < 300 else 1
