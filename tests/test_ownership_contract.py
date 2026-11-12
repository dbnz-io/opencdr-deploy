import io
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from opencdr_deploy import ownership_contract as c
from opencdr_deploy.fleet_api import FleetManagerError


def runtime(monkeypatch, product="opencdr"):
    content = b"reviewed fixture module"
    monkeypatch.setattr(c, "MODULE_SHA256", c.sha(content))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(c.PATHS[product], content)
    return out.getvalue()


def template(raw, product="opencdr"):
    return {
        "Metadata": {c.KEY: c.runtime_contract(raw, product, required=True)},
        "Resources": {
            "Api": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Code": {"S3Bucket": {"Ref": "ArtifactBucket"}, "S3Key": {"Ref": "ArtifactKey"}}
                },
            }
        },
    }


def test_actual_module_pin_matches_both_engine_sources_when_present():
    root = Path(__file__).resolve().parents[2]
    core = root / "opencdr-internal/src/management/case_store.py"
    fleet = root / "opencdr-fleet-manager/src/opencdr_fleet/_contracts/case_store.py"
    if not core.exists() or not fleet.exists():
        pytest.skip("Cross-repository packaging verification")
    assert c.sha(core.read_bytes()) == c.sha(fleet.read_bytes()) == c.MODULE_SHA256


@pytest.mark.parametrize("product", ["opencdr", "opencdr-fleet-manager"])
def test_marker_is_bound_to_module_and_entire_runtime(monkeypatch, product):
    raw = runtime(monkeypatch, product)
    doc = template(raw, product)
    c.verify_template(doc, raw, product)
    doc["Metadata"][c.KEY]["runtime_sha256"] = "0" * 64
    with pytest.raises(FleetManagerError, match="actual pinned"):
        c.verify_template(doc, raw, product)


def test_marker_cannot_hide_a_second_unpinned_function(monkeypatch):
    raw = runtime(monkeypatch)
    doc = template(raw)
    doc["Resources"]["Api"]["Properties"]["Code"] = {"ZipFile": "unfenced handler"}
    with pytest.raises(FleetManagerError, match="unpinned"):
        c.verify_template(doc, raw, "opencdr")


def test_upgrade_cannot_remove_or_downgrade_contract(monkeypatch):
    raw = runtime(monkeypatch)
    doc = template(raw)
    c.require_compatible({}, doc)
    c.require_compatible(doc, doc)
    with pytest.raises(FleetManagerError, match="downgrade"):
        c.require_compatible(doc, {})
    old = template(raw)
    old["Metadata"][c.KEY]["schema"] = "unknown"
    with pytest.raises(FleetManagerError):
        c.require_compatible(old, doc)


def test_execute_checks_actual_s3_bytes_with_previous_parameters(monkeypatch):
    raw = runtime(monkeypatch)
    doc = template(raw)
    s3, cf, session = Mock(), Mock(), Mock()
    session.client.side_effect = lambda service, **kw: {"s3": s3, "cloudformation": cf}[service]
    cf.describe_stacks.return_value = {
        "Stacks": [
            {"Parameters": [{"ParameterKey": "ArtifactBucket", "ParameterValue": "owned-bucket"}]}
        ]
    }
    review = {
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/opencdr-test/id",
        "StackName": "opencdr-test",
        "Parameters": [
            {"ParameterKey": "ArtifactBucket", "UsePreviousValue": True},
            {"ParameterKey": "ArtifactKey", "ParameterValue": "pinned/runtime.zip"},
        ],
    }
    s3.get_object.return_value = {"Body": io.BytesIO(raw)}
    c.verify_change_runtime(session, review, doc)
    s3.get_object.assert_called_with(
        Bucket="owned-bucket", Key="pinned/runtime.zip", ExpectedBucketOwner="123456789012"
    )
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr(c.PATHS["opencdr"], b"older unguarded source")
    s3.get_object.return_value = {"Body": io.BytesIO(bad.getvalue())}
    with pytest.raises(FleetManagerError, match="unsupported"):
        c.verify_change_runtime(session, review, doc)
