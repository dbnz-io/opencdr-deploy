"""Declarative multi-installation configuration parsing."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


class FleetConfigError(RuntimeError):
    """A fleet configuration file is invalid."""


TOP_LEVEL_KEYS = {"version", "defaults", "deployments"}
DEPLOYMENT_KEYS = {
    "name",
    "stage",
    "region",
    "aws_profile",
    "expected_account_id",
    "repository",
    "core_version",
    "core_repository_url",
    "source_root",
    "features",
    "parameters",
}
DEFAULT_KEYS = DEPLOYMENT_KEYS - {"name", "stage", "aws_profile", "expected_account_id"}
FEATURE_KEYS = {"archive", "xray", "security_profile"}


def _unknown(mapping: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise FleetConfigError(f"Unknown {context} fields: {', '.join(unknown)}")


def _string(value: Any, field: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise FleetConfigError(f"{field} must be a non-empty string")
    return value


def _parameters(value: Any, context: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FleetConfigError(f"{context} parameters must be a table")
    result = {}
    for key, item in value.items():
        if not isinstance(item, (str, int, float, bool)):
            raise FleetConfigError(f"{context} parameter {key} must be a scalar")
        if isinstance(item, bool):
            result[key] = str(item).lower()
        else:
            result[key] = str(item)
    return result


def _features(value: Any, context: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FleetConfigError(f"{context} features must be a table")
    _unknown(value, FEATURE_KEYS, f"{context} feature")
    result = {}
    for key, parameter in (("archive", "archiveEnabled"), ("xray", "xrayEnabled")):
        if key in value:
            if not isinstance(value[key], bool):
                raise FleetConfigError(f"{context} feature {key} must be boolean")
            result[parameter] = str(value[key]).lower()
    if "security_profile" in value:
        profile = value["security_profile"]
        if profile not in {"standard", "hardened"}:
            raise FleetConfigError(f"{context} security_profile must be standard or hardened")
        result["securityProfile"] = profile
    return result


def load_fleet_config(path: Path) -> list[dict[str, Any]]:
    try:
        document = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FleetConfigError(f"Cannot read fleet configuration {path}: {exc}") from exc
    _unknown(document, TOP_LEVEL_KEYS, "top-level")
    if document.get("version") != 1:
        raise FleetConfigError("Fleet configuration version must be 1")
    defaults = document.get("defaults", {})
    if not isinstance(defaults, dict):
        raise FleetConfigError("defaults must be a table")
    _unknown(defaults, DEFAULT_KEYS, "default")
    deployments = document.get("deployments")
    if not isinstance(deployments, list) or not deployments:
        raise FleetConfigError("deployments must contain at least one [[deployments]] entry")

    result = []
    names = set()
    for index, raw in enumerate(deployments, start=1):
        if not isinstance(raw, dict):
            raise FleetConfigError(f"deployment {index} must be a table")
        _unknown(raw, DEPLOYMENT_KEYS, f"deployment {index}")
        merged = {**defaults, **raw}
        name = _string(merged.get("name"), f"deployment {index} name", required=True)
        if name in names:
            raise FleetConfigError(f"Duplicate deployment name: {name}")
        names.add(name)

        default_parameters = _parameters(defaults.get("parameters"), "default")
        parameters = {
            **default_parameters,
            **_features(defaults.get("features"), "default"),
            **_parameters(raw.get("parameters"), name),
            **_features(raw.get("features"), name),
        }
        repository = merged.get("repository")
        core_version = merged.get("core_version")
        if bool(repository) == bool(core_version):
            raise FleetConfigError(f"{name} must define exactly one of repository or core_version")
        expected_account_id = merged.get("expected_account_id")
        if expected_account_id is not None:
            expected_account_id = str(expected_account_id)
            if len(expected_account_id) != 12 or not expected_account_id.isdigit():
                raise FleetConfigError(f"{name} expected_account_id must be 12 digits")

        result.append(
            {
                "name": name,
                "stage": _string(merged.get("stage"), f"{name} stage", required=True),
                "region": _string(merged.get("region"), f"{name} region", required=True),
                "aws_profile": _string(merged.get("aws_profile"), f"{name} aws_profile"),
                "expected_account_id": expected_account_id,
                "repository": _string(repository, f"{name} repository"),
                "core_version": _string(core_version, f"{name} core_version"),
                "core_repository_url": _string(
                    merged.get("core_repository_url"), f"{name} core_repository_url"
                ),
                "source_root": _string(merged.get("source_root"), f"{name} source_root"),
                "parameters": parameters,
            }
        )
    return result
