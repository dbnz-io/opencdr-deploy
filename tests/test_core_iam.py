from io import BytesIO
from unittest.mock import Mock

import pytest
from botocore.credentials import Credentials

from opencdr_deploy.core_api import CoreAPI
from opencdr_deploy.fleet_api import FleetManagerError


def config():
    return {
        "url": "https://abc.execute-api.us-east-1.amazonaws.com/dev",
        "auth_mode": "iam",
        "access": "operator",
        "deployment": {"account_id": "123456789012", "home_region": "us-east-1", "stage": "dev"},
    }


def test_signs_selected_iam_path_and_preserves_conflict_without_retry(monkeypatch):
    session = Mock()
    session.get_credentials.return_value = Credentials("test-id", "test-secret", "test-session")
    response = BytesIO(b'{"completed":["one"]}')
    response.code = 409
    transport = Mock(return_value=response)
    monkeypatch.setattr("opencdr_deploy.core_api.open_request", transport)
    client = CoreAPI(config(), session=session)
    assert client.request("POST", "/rule-releases/apply", {}) == (409, {"completed": ["one"]})
    request = transport.call_args.args[0]
    assert request.full_url.endswith("/management/operator/rule-releases/apply")
    assert request.get_header("Authorization").startswith("AWS4-HMAC-SHA256")
    assert not request.has_header("X-api-key")
    transport.assert_called_once()
    with pytest.raises(FleetManagerError):
        client.request("GET", "/%2e%2e/status")


def test_identity_failure_stops_before_mutation():
    client = CoreAPI(config(), session=Mock())
    client.request = Mock(return_value=(200, {"deployment": {"account_id": "999999999999"}}))
    with pytest.raises(FleetManagerError, match="identity"):
        client.verify()


def test_delivery_connection_signs_receipt_route_without_broadening_access(monkeypatch):
    session = Mock()
    session.get_credentials.return_value = Credentials("id", "secret", "session")
    response = BytesIO(b'{"rev":2}')
    response.code = 200
    send = Mock(return_value=response)
    monkeypatch.setattr("opencdr_deploy.core_api.open_request", send)
    client = CoreAPI({**config(), "access": "delivery"}, session=session)
    assert client.request("POST", "/detections/consumers/test/read", {"expected_rev": 1}) == (
        200,
        {"rev": 2},
    )
    request = send.call_args.args[0]
    assert request.full_url.endswith("/management/delivery/detections/consumers/test/read")
    assert b'"expected_rev":1' in request.data
    send.assert_called_once()


def test_release_connection_can_create_a_verified_delivery_connection(monkeypatch, tmp_path):
    import json
    import stat

    from opencdr_deploy.cli import main

    observed = []
    monkeypatch.setattr("opencdr_deploy.core_api.boto3.Session", Mock())
    monkeypatch.setattr(CoreAPI, "verify", lambda self: observed.append(self.config.copy()))
    connection = tmp_path / "delivery.json"
    assert main(
        [
            "release", "connection", str(connection),
            "--url", config()["url"], "--profile", "customer-delivery",
            "--account-id", "123456789012", "--region", "us-east-1", "--stage", "dev",
            "--access", "delivery",
        ]
    ) == 0
    saved = json.loads(connection.read_text())
    assert observed == [saved]
    assert saved["access"] == "delivery" and saved["auth_mode"] == "iam"
    assert saved["aws_profile"] == "customer-delivery"
    assert stat.S_IMODE(connection.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "access,path,body",
    [
        (
            "administrator",
            "/detections/recoveries/restore",
            {"storage_digest": "a" * 64, "reason": "reviewed", "accept_unrecoverable": True},
        ),
        ("administrator", "/detections/recoveries/restore/apply", {"plan_digest": "b" * 64}),
        (
            "delivery",
            "/detections/consumers/recovery-" + "c" * 32 + "/recover",
            {
                "backfill_job_id": "history",
                "plan_digest": "a" * 64,
                "target_epoch": "b" * 32,
                "target_owner": "aws:111111111111:AROAABCDEFGHIJKLMNOP",
                "previous_checkpoint": {
                    "cursor": "old",
                    "sequence": 8,
                    "receipt_id": None,
                    "principal": None,
                },
                "request_id": "c" * 32,
                "reason": "reviewed",
                "accept_unrecoverable": True,
            },
        ),
        (
            "administrator",
            "/detections/handoffs/replace",
            {
                "consumer_id": "fleet",
                "previous_owner": "aws:111111111111:AROAABCDEFGHIJKLMNOP",
                "target_owner": "aws:111111111111:AROAQRSTUVWXYZABCDEF",
                "checkpoint": {"cursor": "reviewed", "sequence": 5, "receipt_id": "a" * 32},
                "reason": "reviewed",
            },
        ),
        (
            "delivery",
            "/detections/consumers/fleet/handoff",
            {"handoff_id": "replace", "plan_digest": "c" * 64},
        ),
        (
            "administrator",
            "/detections/backfills/repair/step",
            {"expected_rev": 3, "plan_digest": "a" * 64, "limit": 10},
        ),
        (
            "delivery",
            "/detections/consumers/test/reconcile",
            {
                "expected_rev": 8,
                "backfill_job_id": "repair",
                "plan_digest": "a" * 64,
                "reason": "reviewed",
                "accept_unrecoverable": True,
                "request_id": "b" * 32,
            },
        ),
    ],
)
def test_maintenance_preserves_exact_review_body_and_never_retries_conflicts(
    monkeypatch, access, path, body
):
    import json

    session = Mock()
    session.get_credentials.return_value = Credentials("id", "secret", "session")
    response = BytesIO(b'{"code":"revision_conflict"}')
    response.code = 409
    send = Mock(return_value=response)
    monkeypatch.setattr("opencdr_deploy.core_api.open_request", send)
    client = CoreAPI({**config(), "access": access}, session=session)
    assert client.request("POST", path, body)[0] == 409
    request = send.call_args.args[0]
    assert request.full_url.endswith("/management/" + access + path)
    assert json.loads(request.data) == body
    send.assert_called_once()


def test_triage_request_preserves_idempotency_and_revision_without_widening_access(monkeypatch):
    import json

    session = Mock()
    session.get_credentials.return_value = Credentials("id", "secret", "session")
    response = BytesIO(b'{"handling":{"rev":1},"idempotent":true}')
    response.code = 200
    send = Mock(return_value=response)
    monkeypatch.setattr("opencdr_deploy.core_api.open_request", send)
    client = CoreAPI({**config(), "access": "triage"}, session=session)
    body = {
        "expected_rev": 0,
        "request_id": "a" * 32,
        "reason": "reviewed",
        "changes": {"status": "acknowledged"},
    }
    assert client.request("PUT", "/detections/handling?detection_id=id%2Fone", body)[0] == 200
    request = send.call_args.args[0]
    assert request.full_url.endswith("/management/triage/detections/handling?detection_id=id%2Fone")
    assert json.loads(request.data) == body
    assert not request.has_header("X-api-key")
    send.assert_called_once()


@pytest.mark.parametrize(
    "method,path,body",
    [
        (
            "POST",
            "/cases",
            {
                "request_id": "a" * 32,
                "reason": "test",
                "title": "Case",
                "description": "",
                "severity": "high",
                "agent_ids": ["111111111111:us-east-1:dev"],
            },
        ),
        (
            "POST",
            "/cases/" + "b" * 32 + "/detections",
            {
                "expected_rev": 1,
                "request_id": "c" * 32,
                "reason": "Verified",
                "agent_id": "a",
                "detection_id": "d",
            },
        ),
    ],
)
def test_deploy_is_sufficient_to_call_case_apis_without_extra_client(
    monkeypatch, method, path, body
):
    import json

    session = Mock()
    session.get_credentials.return_value = Credentials("id", "secret", "session")
    response = BytesIO(b'{"case":{"rev":2}}')
    response.code = 200
    send = Mock(return_value=response)
    monkeypatch.setattr("opencdr_deploy.core_api.open_request", send)
    client = CoreAPI({**config(), "access": "triage"}, session=session)
    assert client.request(method, path, body)[0] == 200
    request = send.call_args.args[0]
    assert request.full_url.endswith("/management/triage" + path)
    assert json.loads(request.data) == body
    send.assert_called_once()


def test_dedicated_connector_connection_signs_report_without_api_key(monkeypatch):
    session = Mock()
    session.get_credentials.return_value = Credentials("id", "secret", "session")
    response = BytesIO(b'{"rev":2}')
    response.code = 200
    send = Mock(return_value=response)
    monkeypatch.setattr("opencdr_deploy.core_api.open_request", send)
    body = {"integration_id": "siem", "sequence": 1, "request_id": "a" * 32}
    client = CoreAPI({**config(), "access": "integration"}, session=session)
    assert client.request("POST", "/cases/" + "b" * 32 + "/external-status", body)[0] == 200
    request = send.call_args.args[0]
    assert "/management/integration/cases/" in request.full_url
    assert not request.has_header("X-api-key")
    send.assert_called_once()


@pytest.mark.parametrize(
    "path", ["/library/items", "/library/releases", "/library/items/" + "a" * 32 + "/versions"]
)
def test_signed_library_requests_preserve_body_and_never_use_api_keys(monkeypatch, path):
    import json

    session = Mock()
    session.get_credentials.return_value = Credentials("id", "secret", "session")
    response = BytesIO(b'{"idempotent":true}')
    response.code = 200
    send = Mock(return_value=response)
    monkeypatch.setattr("opencdr_deploy.core_api.open_request", send)
    client = CoreAPI({**config(), "access": "operator"}, session=session)
    body = {"request_id": "a" * 32, "reason": "authored", "expected_rev": 3}
    assert client.request("POST", path, body)[0] == 200
    request = send.call_args.args[0]
    assert request.full_url.endswith("/management/operator" + path)
    assert json.loads(request.data) == body
    assert not request.has_header("X-api-key")
    assert "/execute-api/aws4_request" in request.get_header("Authorization")
    send.assert_called_once()
