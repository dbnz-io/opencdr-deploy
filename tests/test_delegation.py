import json
from io import BytesIO
from unittest.mock import Mock
from urllib.error import URLError

import pytest
from botocore.credentials import Credentials

from opencdr_deploy.core_api import CoreAPI
from opencdr_deploy.delegation import delegated_request
from opencdr_deploy.fleet_api import FleetAPI, FleetManagerError

OWNER = {"account_id": "123456789012", "home_region": "us-east-1", "stage": "dev"}
IDENTITY = {"issuer": "opencdr-saas", "tenant": "org-a", "subject": "user-a"}
URL = "https://abc.execute-api.us-east-1.amazonaws.com/dev"


@pytest.mark.parametrize("mode", ["core", "fleet"])
@pytest.mark.parametrize("rejected", [False, True])
def test_entire_assertion_and_operation_are_signed_once_without_fallback(
    monkeypatch, mode, rejected
):
    session = Mock()
    session.get_credentials.return_value = Credentials("id", "secret", "session")
    config = {
        "url": URL,
        "api_url": URL,
        "access": "triage",
        "deployment": OWNER,
        "security_account_id": OWNER["account_id"],
        "region": "us-east-1",
        "stage": "dev",
        "identity": IDENTITY,
    }
    client = (CoreAPI if mode == "core" else FleetAPI)(config, session=session)
    prefix = "" if mode == "core" else "/v1"
    response = BytesIO(b'{"code":"identity_denied"}' if rejected else b'{"rev":2}')
    response.code = 403 if rejected else 200
    send = Mock(return_value=response)
    monkeypatch.setattr(f"opencdr_deploy.{mode}_api.open_request", send)
    monkeypatch.setattr("opencdr_deploy.delegation.time.time", lambda: 1000)
    body = {"expected_rev": 1, "request_id": "a" * 32, "reason": "Reviewed"}
    assert (
        client.request("PUT", prefix + "/detections/handling?detection_id=a%2Fb", body)[0]
        == response.code
    )
    request = send.call_args.args[0]
    assert request.full_url == URL + "/management/triage" + prefix + "/delegated"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization").startswith("AWS4-HMAC-SHA256")
    assert not request.has_header("X-api-key")
    assert json.loads(request.data) == {
        "identity": {**IDENTITY, "audience": OWNER, "issued_at": 1000, "expires_at": 1060},
        "operation": {
            "method": "PUT",
            "path": prefix + "/detections/handling",
            "query": {"detection_id": "a/b"},
            "body": body,
        },
    }
    send.assert_called_once()
    send.reset_mock(side_effect=True)
    send.side_effect = URLError("response lost")
    with pytest.raises(FleetManagerError, match="unknown"):
        client.request("POST", prefix + "/cases", body)
    send.assert_called_once()


@pytest.mark.parametrize(
    "identity", [{}, {**IDENTITY, "grant_rev": 3}, {**IDENTITY, "subject": "bad space"}]
)
def test_malformed_selection_is_rejected_before_signing(identity):
    with pytest.raises(ValueError, match="Identity"):
        delegated_request("GET", "/cases", None, identity, OWNER)


def test_status_and_legacy_are_explicit_and_repeated_queries_are_not_collapsed():
    assert delegated_request("GET", "/status", None, IDENTITY, OWNER) == ("GET", "/status", None)
    assert delegated_request("PUT", "/settings/global", {"rev": 1}, None, OWNER) == (
        "PUT",
        "/settings/global",
        {"rev": 1},
    )
    with pytest.raises(ValueError, match="Duplicate"):
        delegated_request("GET", "/cases?status=open&status=closed", None, IDENTITY, OWNER)
    with pytest.raises(ValueError, match="Nested"):
        delegated_request("POST", "/delegated", {}, IDENTITY, OWNER)
    with pytest.raises(ValueError, match="object"):
        delegated_request("POST", "/cases", [], IDENTITY, OWNER)
