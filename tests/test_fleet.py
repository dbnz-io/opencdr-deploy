from __future__ import annotations

from pathlib import Path

import pytest

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
