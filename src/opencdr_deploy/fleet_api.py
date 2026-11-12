"""IAM-authenticated Fleet Manager API access, independent of local Core sources."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config


class FleetManagerError(RuntimeError):
    """A safe operator-facing Fleet Manager error."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


open_request = build_opener(NoRedirect()).open


def aws_session(config: dict[str, Any]):
    return boto3.Session(profile_name=config["aws_profile"], region_name=config["region"])


def aws_client(session, service: str):
    return session.client(
        service,
        config=Config(
            connect_timeout=5,
            read_timeout=20,
            retries={"total_max_attempts": 1},
        ),
    )


def verify_account(session, expected: str) -> None:
    actual = aws_client(session, "sts").get_caller_identity().get("Account")
    if actual != expected:
        raise FleetManagerError(f"AWS profile account mismatch: expected {expected}, got {actual}")


def endpoint(value: str, region: str, stage: str) -> str:
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "https"
            and not url.username
            and not url.password
            and not url.port
            and not url.query
            and not url.fragment
            and re.fullmatch(
                rf"[a-z0-9]+\.execute-api\.{re.escape(region)}\.amazonaws\.com", url.hostname or ""
            )
            and url.path.rstrip("/") == f"/{stage}"
        )
    except ValueError:
        valid = False
    if not valid:
        raise FleetManagerError("Use the regional HTTPS FleetManagerApiUrl, including its stage")
    return value.rstrip("/")


def relative_path(value: str, prefix: str = "/v1/") -> str:
    parsed = urlsplit(value)
    decoded = unquote(unquote(parsed.path))
    if (
        not value.startswith(prefix)
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or "\\" in decoded
        or any(part in {".", ".."} for part in decoded.split("/"))
        or any(ord(char) < 32 for char in value)
    ):
        raise FleetManagerError(f"Use a relative {prefix} path without traversal or a fragment")
    return value


def agent_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
        raise FleetManagerError("Agent IDs must contain 1–63 lowercase letters, digits or hyphens")
    return value


def response_ok(status: int, body: Any) -> bool:
    if not isinstance(status, int) or not 200 <= status < 300:
        return False
    if not isinstance(body, dict):
        return True
    if body.get("error_code") or body.get("outcome") or body.get("body_omitted"):
        return False
    if "status_code" in body:
        code = body["status_code"]
        if not isinstance(code, int) or not 200 <= code < 300 or not body.get("attempted"):
            return False
    results = body.get("results")
    return results is None or (
        isinstance(results, list)
        and all(
            isinstance(row, dict) and "status_code" in row and response_ok(row["status_code"], row)
            for row in results
        )
    )


class FleetAPI:
    def __init__(self, config: dict[str, Any], session=None):
        self.config = config
        self.url = endpoint(config["api_url"], config["region"], config["stage"])
        access = config.get("access")
        if access:
            if access not in {
                "observer",
                "triage",
                "operator",
                "responder",
                "administrator",
                "integration",
            }:
                raise FleetManagerError("Invalid Fleet management access")
            self.url += "/management/" + access
        self.session = session or aws_session(config)

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        relative_path(path)
        if method not in {"GET", "POST", "PUT", "DELETE"}:
            raise FleetManagerError("Supported methods: GET, POST, PUT, DELETE")
        if method == "GET" and body is not None:
            raise FleetManagerError("GET requests cannot contain a body")
        from .delegation import delegated_request

        try:
            method, path, body = delegated_request(
                method,
                path,
                body,
                self.config.get("identity"),
                {
                    "account_id": self.config["security_account_id"],
                    "home_region": self.config["region"],
                    "stage": self.config["stage"],
                },
                prefix="/v1",
            )
        except ValueError as error:
            raise FleetManagerError(str(error)) from error
        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        if raw is not None and len(raw) > 2_000_000:
            raise FleetManagerError("Fleet API request body exceeds 2 MB")
        credentials = self.session.get_credentials()
        if credentials is None:
            raise FleetManagerError("AWS credentials unavailable; sign in to the saved profile")
        request = AWSRequest(
            method=method,
            url=self.url + path,
            data=raw,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        SigV4Auth(
            credentials.get_frozen_credentials(), "execute-api", self.config["region"]
        ).add_auth(request)
        prepared = request.prepare()
        req = Request(prepared.url, data=raw, method=method, headers=dict(prepared.headers))
        try:
            response = open_request(req, timeout=30)
        except HTTPError as error:
            response = error
        except (URLError, OSError) as error:
            message = "Fleet Manager is unavailable"
            if method != "GET":
                message += "; the write outcome is unknown. Inspect agent state before retrying"
            raise FleetManagerError(message) from error
        with response:
            try:
                payload = response.read(4_000_001)
            except (OSError, TimeoutError) as error:
                raise FleetManagerError(
                    "Fleet response was interrupted; inspect agent state before retrying a write"
                ) from error
            if len(payload) > 4_000_000:
                raise FleetManagerError("Fleet response exceeds 4 MB; use narrower pagination")
            try:
                value = json.loads(payload) if payload else None
            except (ValueError, UnicodeError) as error:
                raise FleetManagerError("Fleet Manager returned invalid JSON") from error
            return response.code, value

    def verify(self) -> dict[str, Any]:
        status, value = self.request("GET", "/v1/status")
        if status != 200 or not isinstance(value, dict):
            raise FleetManagerError(
                "Fleet access failed; check URL, AWS sign-in and invoke permissions"
            )
        identity = value.get("deployment", {})
        if not isinstance(identity, dict):
            raise FleetManagerError("Fleet Manager returned an invalid deployment identity")
        if (
            value.get("product") != "OpenCDR Fleet Manager"
            or not re.fullmatch(r"1\.\d+\.\d+", str(value.get("fleet_contract_version", "")))
            or identity.get("account_id") != self.config["security_account_id"]
            or identity.get("home_region") != self.config["region"]
            or identity.get("stage") != self.config["stage"]
        ):
            raise FleetManagerError(
                "Fleet identity or contract does not match the saved connection"
            )
        return value

    def agents(self) -> dict[str, Any]:
        items, seen = [], set()
        token = None
        for _ in range(100):
            query = urlencode({"limit": 50, **({"next_token": token} if token else {})})
            status, value = self.request("GET", f"/v1/agents?{query}")
            if (
                status != 200
                or not isinstance(value, dict)
                or not isinstance(value.get("items"), list)
            ):
                raise FleetManagerError(
                    "Unable to read complete fleet inventory; no partial list shown"
                )
            items.extend(value["items"])
            token = value.get("next_token")
            if not token:
                return {"items": items, "next_token": None}
            if not isinstance(token, str) or token in seen:
                raise FleetManagerError(
                    "Fleet inventory returned an invalid/repeated pagination cursor"
                )
            seen.add(token)
        raise FleetManagerError("Inventory exceeds 100 pages; use 'fleet request' with pagination")
