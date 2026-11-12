import json
from unittest.mock import MagicMock

import pytest

from opencdr_deploy import cli, organizations


def test_discovery_reads_one_page_keeps_empty_continuation_and_never_registers(monkeypatch, capsys):
    session = MagicMock()
    client = MagicMock()
    client.list_accounts.return_value = {"Accounts": [], "NextToken": "next"}
    monkeypatch.setattr(organizations.boto3, "Session", lambda **_: session)
    verify = MagicMock()
    monkeypatch.setattr(organizations, "verify_account", verify)
    monkeypatch.setattr(
        organizations,
        "aws_client",
        lambda _, service: client if service == "organizations" else None,
    )
    args = cli.build_parser().parse_args(
        ["organizations", "--profile", "reader", "--account-id", "111111111111", "--limit", "10"]
    )
    assert organizations.run(args) == 0
    verify.assert_called_once_with(session, "111111111111")
    client.list_accounts.assert_called_once_with(MaxResults=10)
    value = json.loads(capsys.readouterr().out)
    assert (
        value["next_after"] == "next" and value["registered"] is False and value["accounts"] == []
    )


def test_discovery_requires_expected_account_before_read(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(organizations.boto3, "Session", lambda **_: session)
    monkeypatch.setattr(
        organizations,
        "verify_account",
        MagicMock(side_effect=organizations.FleetManagerError("mismatch")),
    )
    call = MagicMock()
    monkeypatch.setattr(organizations, "aws_client", call)
    args = cli.build_parser().parse_args(
        ["organizations", "--profile", "reader", "--account-id", "111111111111"]
    )
    with pytest.raises(organizations.FleetManagerError, match="mismatch"):
        organizations.run(args)
    call.assert_not_called()
