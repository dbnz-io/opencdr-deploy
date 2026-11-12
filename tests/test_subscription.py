from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from opencdr_deploy.subscription import apply_plan, make_plan

OWNER = {"account_id": "123456789012", "home_region": "us-east-1", "stage": "test"}
PRINCIPAL = "AROAABCDEFGHIJKLMNOP"
SHA = "a" * 64


def client():
    value = MagicMock()
    value.verify.return_value = {"deployment": OWNER}
    value.session.client.return_value.get_caller_identity.return_value = {
        "Account": OWNER["account_id"],
        "UserId": PRINCIPAL + ":session",
    }
    value.request.return_value = (404, {})
    return value


def plan(value):
    return make_plan(
        value,
        "core",
        None,
        "tenant:deployment",
        "explicit-checkpoint",
        datetime.now(UTC).isoformat(),
        "Reviewed cutover",
    )


def receipt(p):
    return {
        "consumer_id": p["consumer_id"],
        "initial_cursor": p["initial_cursor"],
        "checkpoint_sha256": SHA,
        "principal": "aws:" + OWNER["account_id"] + ":" + PRINCIPAL,
        "acknowledged_sequence": 42,
        "rev": 1,
    }


def test_plan_is_read_only_and_apply_reconciles_existing_exact_receipt():
    api = client()
    p = plan(api)
    assert all(call.args[0] == "GET" for call in api.request.call_args_list)
    api.request.reset_mock()
    api.request.side_effect = [(404, {}), (200, receipt(p))]
    result = apply_plan(api, "core", None, p, SHA)
    assert result["applied"] and not result["notification_delivery_enabled"]
    assert api.request.call_args.args[2] == {
        "initial_cursor": "explicit-checkpoint",
        "checkpoint_sha256": SHA,
    }
    api.request.reset_mock()
    api.request.side_effect = None
    api.request.return_value = (200, {**receipt(p), "rev": 9, "acknowledged_sequence": 100})
    apply_plan(api, "core", None, p, SHA)
    assert api.request.call_count == 1 and api.request.call_args.args[0] == "GET"


def test_existing_foreign_checkpoint_never_mutates_or_resets():
    api = client()
    p = plan(api)
    api.request.return_value = (200, {**receipt(p), "checkpoint_sha256": "b" * 64})
    with pytest.raises(Exception, match="receipt mismatch"):
        apply_plan(api, "core", None, p, SHA)
    assert all(call.args[0] == "GET" for call in api.request.call_args_list)
    with pytest.raises(Exception, match="New consumer required"):
        plan(api)


@pytest.mark.parametrize("change", ["expired", "principal", "owner"])
def test_rechecks_finite_plan_and_exact_service_owner_before_write(change):
    api = client()
    p = plan(api)
    api.request.reset_mock()
    if change == "expired":
        p["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif change == "principal":
        api.session.client.return_value.get_caller_identity.return_value["UserId"] += "X"
        api.session.client.return_value.get_caller_identity.return_value["UserId"] = (
            "AROAABCDEFGHIJKLMNOPQ:s"
        )
    else:
        api.verify.return_value = {"deployment": {**OWNER, "stage": "different"}}
    with pytest.raises(Exception, match="expired|changed"):
        apply_plan(api, "core", None, p, SHA)
    api.request.assert_not_called()


def test_unknown_registration_result_is_not_automatically_retried():
    api = client()
    p = plan(api)
    api.request.reset_mock()
    api.request.side_effect = [(404, {}), TimeoutError("unknown write")]
    with pytest.raises(TimeoutError):
        apply_plan(api, "core", None, p, SHA)
    assert api.request.call_count == 2


def test_fleet_registration_change_rejects_before_subscription_mutation():
    api = client()
    registration = {
        "agent_id": "agent",
        "rev": 1,
        "enabled": True,
        "auth_mode": "iam",
        "account_id": OWNER["account_id"],
        "region": "us-east-1",
        "stage": "test",
    }
    api.request.side_effect = lambda method, path, *args: (
        (200, {"agent": registration}) if path == "/v1/agents/agent" else (404, {})
    )
    p = make_plan(
        api,
        "fleet",
        "agent",
        "tenant:deployment",
        "cursor",
        datetime.now(UTC).isoformat(),
        "Reviewed",
    )
    registration["rev"] = 2
    api.request.reset_mock()
    with pytest.raises(Exception, match="changed"):
        apply_plan(api, "fleet", "agent", p, SHA)
    assert api.request.call_count == 1 and api.request.call_args.args[0] == "GET"
