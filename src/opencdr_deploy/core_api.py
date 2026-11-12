"""IAM Core connection. Never retry a management mutation or fall back to API keys."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from .fleet_api import FleetManagerError, endpoint, open_request, relative_path


class CoreAPI:
    def __init__(self, config, session=None):
        self.config = config
        identity = config.get("deployment", {})
        if set(identity) != {"account_id", "home_region", "stage"} or not re.fullmatch(
            r"\d{12}", str(identity.get("account_id", ""))
        ):
            raise FleetManagerError("IAM Core requires account_id, home_region and stage")
        self.region = identity["home_region"]
        self.base = endpoint(config["url"], self.region, identity["stage"])
        self.access = config.get("access", "operator")
        if self.access not in {
            "observer",
            "delivery",
            "integration",
            "triage",
            "operator",
            "responder",
            "administrator",
        }:
            raise FleetManagerError("Invalid Core management access")
        self.session = session or boto3.Session(
            profile_name=config.get("aws_profile"), region_name=self.region
        )

    def request(self, method, path, body=None):
        relative_path(path, "/")
        if path.startswith("//") or method not in {"GET", "POST", "PUT", "DELETE"}:
            raise FleetManagerError("Invalid Core request")
        if method in {"GET", "DELETE"} and body is not None:
            raise FleetManagerError("GET and DELETE cannot carry a body")
        from .delegation import delegated_request

        try:
            method, path, body = delegated_request(
                method, path, body, self.config.get("identity"), self.config["deployment"]
            )
        except ValueError as error:
            raise FleetManagerError(str(error)) from error
        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        if raw and len(raw) > 2_000_000:
            raise FleetManagerError("Core request exceeds 2 MB")
        credentials = self.session.get_credentials()
        if credentials is None:
            raise FleetManagerError(
                "AWS credentials unavailable; sign in to the configured profile"
            )
        signed = AWSRequest(
            method=method,
            url=f"{self.base}/management/{self.access}{path}",
            data=raw,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(credentials.get_frozen_credentials(), "execute-api", self.region).add_auth(signed)
        request = Request(signed.url, data=raw, method=method, headers=dict(signed.headers))
        try:
            try:
                response = open_request(request, timeout=30)
            except HTTPError as error:
                response = error
            with response:
                payload = response.read(2_000_001)
                if len(payload) > 2_000_000:
                    raise FleetManagerError("Core response exceeds 2 MB")
                return response.code, json.loads(payload) if payload else None
        except (URLError, OSError, ValueError) as error:
            outcome = (
                "; write outcome unknown, inspect state before retrying" if method != "GET" else ""
            )
            raise FleetManagerError("Core request failed" + outcome) from error

    def verify(self):
        code, report = self.request("GET", "/status")
        if (
            code != 200
            or not isinstance(report, dict)
            or report.get("product") != "OpenCDR Core"
            or report.get("deployment") != self.config["deployment"]
            or str(report.get("management_contract_version", "")).split(".")[0] != "2"
            or not report.get("capabilities", {}).get("iam_management")
        ):
            raise FleetManagerError("Core identity or IAM contract mismatch; no operation sent")
        return report
