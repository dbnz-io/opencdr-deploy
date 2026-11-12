import json
from unittest.mock import MagicMock

import pytest

from opencdr_deploy import cli, workflows

OWNER = {"account_id": "111111111111", "home_region": "us-east-1", "stage": "dev"}


def args(tmp_path, action="execute", mode="core", body=None, **options):
    connection = tmp_path / "connection.json"
    connection.write_text(json.dumps({"auth_mode": "iam", "deployment": OWNER}))
    argv = ["workflow", "--mode", mode, "--connection", str(connection), action]
    if body is not None:
        payload = tmp_path / "request.json"
        payload.write_text(json.dumps(body))
        argv += ["--file", str(payload)]
    for key, value in options.items():
        argv += ["--" + key.replace("_", "-"), str(value)]
    return cli.build_parser().parse_args(argv)


def command(**extra):
    return {"expected_rev": 2, "request_id": "a" * 32, "reason": "Exact request", **extra}


def test_guided_execution_uses_verified_owner_once(tmp_path, monkeypatch, capsys):
    client = MagicMock()
    client.verify.return_value = {"deployment": OWNER}
    client.request.return_value = (
        202,
        {
            "deployment": OWNER,
            "execution": {
                "state": "queued",
                "approval_id": "b" * 32,
                "request_id": "a" * 32,
                "approval_rev": 2,
            },
        },
    )
    monkeypatch.setattr(workflows, "CoreAPI", lambda _: client)
    assert workflows.run(args(tmp_path, body=command(), id="b" * 32)) == 0
    client.verify.assert_called_once()
    client.request.assert_called_once_with("POST", "/reviews/" + "b" * 32 + "/execute", command())
    assert "queued" in capsys.readouterr().out


@pytest.mark.parametrize(
    "change", [{"actor": "forged"}, {"expected_rev": True}, {"request_id": "../"}]
)
def test_invalid_command_stops_before_transport(tmp_path, monkeypatch, change):
    construct = MagicMock()
    monkeypatch.setattr(workflows, "CoreAPI", construct)
    with pytest.raises(Exception):
        workflows.run(args(tmp_path, body=command(**change), id="b" * 32))
    construct.assert_not_called()


def test_fleet_workflow_never_uses_core_and_uncertainty_is_not_retried(tmp_path, monkeypatch):
    client = MagicMock()
    client.verify.return_value = {"deployment": OWNER}
    client.request.side_effect = workflows.FleetManagerError("unknown outcome")
    monkeypatch.setattr(workflows, "FleetAPI", lambda _: client)
    monkeypatch.setattr(
        workflows, "CoreAPI", MagicMock(side_effect=AssertionError("No Core fallback"))
    )
    with pytest.raises(workflows.FleetManagerError, match="unknown"):
        workflows.run(
            args(
                tmp_path,
                action="resolve-group",
                mode="fleet",
                body={"expected_rev": 3},
                id="canary",
            )
        )
    client.request.assert_called_once_with("POST", "/v1/groups/canary/resolve", {"expected_rev": 3})


def test_wrong_owner_cannot_confirm_a_write(tmp_path, monkeypatch):
    client = MagicMock()
    client.verify.return_value = {"deployment": OWNER}
    client.request.return_value = (200, {"deployment": {**OWNER, "stage": "elsewhere"}})
    monkeypatch.setattr(workflows, "CoreAPI", lambda _: client)
    with pytest.raises(workflows.FleetManagerError, match="identity mismatch"):
        workflows.run(args(tmp_path, body=command(), id="b" * 32))
    assert client.request.call_count == 1


@pytest.mark.parametrize(
    "change", [{"approval_id": "c" * 32}, {"request_id": "d" * 32}, {"approval_rev": 1}]
)
def test_matching_owner_cannot_confirm_another_execution(tmp_path, monkeypatch, change):
    client = MagicMock()
    client.verify.return_value = {"deployment": OWNER}
    client.request.return_value = (
        200,
        {
            "deployment": OWNER,
            "execution": {
                "approval_id": "b" * 32,
                "request_id": "a" * 32,
                "approval_rev": 2,
                **change,
            },
        },
    )
    monkeypatch.setattr(workflows, "CoreAPI", lambda _: client)
    with pytest.raises(workflows.FleetManagerError, match="receipt mismatch"):
        workflows.run(args(tmp_path, body=command(), id="b" * 32))
    assert client.request.call_count == 1


def test_policy_bootstrap_is_explicit_direct_administrator_only(tmp_path, monkeypatch):
    selected = args(
        tmp_path, action="configure-policy", body=command(expected_rev=0, policy={"reviewers": []})
    )
    client = MagicMock()
    monkeypatch.setattr(workflows, "CoreAPI", lambda _: client)
    with pytest.raises(workflows.FleetManagerError, match="direct IAM administrator"):
        workflows.run(selected)
    selected.connection.write_text(
        json.dumps({"auth_mode": "iam", "access": "administrator", "deployment": OWNER})
    )
    client.verify.return_value = {"deployment": OWNER}
    client.request.return_value = (200, {"deployment": OWNER, "policy": {"rev": 1}})
    assert workflows.run(selected) == 0
    assert client.request.call_args.args[:2] == ("PUT", "/review-policy")


def test_grant_subject_cannot_escape_owner_route(tmp_path, monkeypatch):
    construct = MagicMock()
    monkeypatch.setattr(workflows, "CoreAPI", construct)
    with pytest.raises(workflows.FleetManagerError, match="stable grant subject"):
        workflows.run(args(tmp_path, action="grant", subject="../reviews"))
    construct.assert_not_called()


def test_extension_write_requires_exact_case_receipt(tmp_path, monkeypatch):
    client = MagicMock()
    client.verify.return_value = {"deployment": OWNER}
    client.request.return_value = (200, {"deployment": OWNER, "case": {"case_id": "b" * 32, "rev": 3}})
    monkeypatch.setattr(workflows, "CoreAPI", lambda _: client)
    body = command(indicator={"type": "domain", "value": "example.com"}, relationship="unconfirmed", note="Fixture", source={"kind": "analyst"})
    command_args = args(tmp_path, action="curate-indicator", id="b" * 32, body=body)
    assert workflows.run(command_args) == 0
    client.request.assert_called_once_with("POST", "/cases/" + "b" * 32 + "/extensions/indicators", body)
    with pytest.raises(workflows.FleetManagerError, match="receipt mismatch"):
        workflows.validate_result(command_args, body, {"case": {"case_id": "c" * 32, "rev": 3}})
