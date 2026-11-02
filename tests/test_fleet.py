from __future__ import annotations

from pathlib import Path

import pytest

from opencdr_deploy import cli
from opencdr_deploy.fleet import FleetConfigError, load_fleet_config


def write_config(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def test_fleet_config_merges_defaults_features_and_multiple_accounts(tmp_path):
    config = write_config(
        tmp_path / "fleet.toml",
        """
version = 1

[defaults]
core_version = "v1.2.1"
region = "us-east-1"

[defaults.features]
archive = true
xray = false
security_profile = "hardened"

[defaults.parameters]
alarmEmail = "operations@example.com"

[[deployments]]
name = "account-a"
stage = "prod"
aws_profile = "account-a"
expected_account_id = "111111111111"

[[deployments]]
name = "account-b"
stage = "prod"
region = "eu-west-1"
aws_profile = "account-b"
expected_account_id = "222222222222"

[deployments.features]
archive = false
""",
    )

    deployments = load_fleet_config(config)

    assert [item["name"] for item in deployments] == ["account-a", "account-b"]
    assert deployments[0]["core_version"] == "v1.2.1"
    assert deployments[0]["parameters"] == {
        "alarmEmail": "operations@example.com",
        "archiveEnabled": "true",
        "xrayEnabled": "false",
        "securityProfile": "hardened",
    }
    assert deployments[1]["region"] == "eu-west-1"
    assert deployments[1]["parameters"]["archiveEnabled"] == "false"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('unexpected = "value"', "Unknown deployment"),
        ('expected_account_id = "123"', "12 digits"),
        ('repository = "/tmp/core"', "exactly one"),
    ],
)
def test_fleet_config_rejects_invalid_or_ambiguous_entries(tmp_path, extra, message):
    config = write_config(
        tmp_path / "fleet.toml",
        f"""
version = 1

[[deployments]]
name = "prod"
stage = "prod"
region = "us-east-1"
core_version = "v1.2.1"
{extra}
""",
    )
    with pytest.raises(FleetConfigError, match=message):
        load_fleet_config(config)


def test_fleet_config_rejects_duplicate_names(tmp_path):
    config = write_config(
        tmp_path / "fleet.toml",
        """
version = 1

[[deployments]]
name = "prod"
stage = "prod"
region = "us-east-1"
core_version = "v1.2.1"

[[deployments]]
name = "prod"
stage = "staging"
region = "us-east-1"
core_version = "v1.2.1"
""",
    )
    with pytest.raises(FleetConfigError, match="Duplicate deployment"):
        load_fleet_config(config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("stage", "prod"),
        ("region", "eu-west-1"),
        ("expected_account_id", "222222222222"),
        ("expected_account_id", None),
    ],
)
def test_fleet_apply_cannot_redirect_an_existing_name(field, value):
    identity = {
        "name": "dev",
        "stage": "dev",
        "region": "us-east-1",
        "expected_account_id": "111111111111",
    }
    registry = {"version": 1, "deployments": {"dev": dict(identity)}}
    changed = {**identity, field: value}
    with pytest.raises(cli.DeployError, match="different AWS account, region or stage"):
        cli.check_fleet_registry_bindings([changed], registry)
    cli.check_fleet_registry_bindings([identity], registry)


def test_fleet_apply_reuses_registered_name_instead_of_duplicate_identity():
    existing = {"stage": "dev", "region": "us-east-1", "expected_account_id": "111111111111"}
    registry = {"version": 1, "deployments": {"original-dev": existing}}
    with pytest.raises(cli.DeployError, match="already registered as"):
        cli.check_fleet_registry_bindings([{**existing, "name": "new-dev"}], registry)
    cli.check_fleet_registry_bindings([{**existing, "name": "original-dev"}], registry)


def test_fleet_name_conflict_stops_before_source_download_or_registry_writes(tmp_path, monkeypatch):
    config = write_config(
        tmp_path / "fleet.toml",
        """
version = 1
[[deployments]]
name = "dev"
stage = "dev"
region = "us-east-1"
expected_account_id = "111111111111"
core_version = "v1.2.1"
""",
    )
    registry_path = tmp_path / "registry.json"
    registry = {
        "version": 1,
        "deployments": {
            "dev": {
                "stage": "prod",
                "region": "us-east-1",
                "expected_account_id": "111111111111",
            }
        },
    }
    monkeypatch.setattr(cli, "build_deployment", lambda **_kwargs: pytest.fail("source changed"))
    args = cli.build_parser().parse_args(["apply", "--file", str(config)])
    with pytest.raises(cli.DeployError):
        cli.cmd_apply(args, registry, registry_path)
    assert not registry_path.exists()
