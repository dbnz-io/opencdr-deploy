from opencdr_deploy import artifacts


def test_catalog_comes_from_verified_portable_artifact_and_hides_secrets(monkeypatch):
    calls = []

    def load(path, digest):
        calls.append((path, digest))
        return {"product": "opencdr", "version": "2.0.0"}, {
            "Parameters": {
                "ArchiveEnabled": {
                    "Type": "String",
                    "AllowedValues": ["true", "false"],
                    "Default": "false",
                },
                "PrivateCredential": {"Type": "String", "NoEcho": True, "Default": "must-not-leak"},
                "RetentionDays": {"Type": "Number", "MinValue": 7, "MaxValue": 365, "Default": 90},
            }
        }

    monkeypatch.setattr(artifacts, "load_release", load)
    result = artifacts.flag_catalog("pinned-release", "a" * 64)
    assert calls == [("pinned-release", "a" * 64)]
    assert result["application"] == "reviewed_deploy_plan" and result["changes_aws"] is False
    values = {v["id"]: v for v in result["parameters"]}
    assert values["ArchiveEnabled"]["type"] == "boolean"
    assert "default" not in values["PrivateCredential"]
    assert values["RetentionDays"]["constraints"] == {"MinValue": 7, "MaxValue": 365}
