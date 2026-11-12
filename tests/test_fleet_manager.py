from __future__ import annotations

import json
import subprocess
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest
from botocore.credentials import Credentials

from opencdr_deploy import cli
from opencdr_deploy import fleet_api as transport
from opencdr_deploy import fleet_manager as fleet
from opencdr_deploy.fleet_api import FleetAPI, FleetManagerError, response_ok

CONFIG = {
    "name": "security-prod",
    "security_account_id": "111111111111",
    "region": "us-east-1",
    "stage": "prod",
    "aws_profile": "security-admin",
    "api_url": "https://abcdefghij.execute-api.us-east-1.amazonaws.com/prod",
}


def test_poll_intervals_are_validated_and_forwarded_to_source_package():
    config = {
        **CONFIG,
        "poll_interval_minutes": 5,
        "full_refresh_minutes": 120,
        "security_profile": "standard",
        "snapshot_retention_hours": 24,
        "polling_enabled": True,
        "allowed_agent_hosts": "",
    }
    fleet._validate(config)
    assert "--param=pollIntervalMinutes=5" in fleet._parameters(config)
    assert "--param=fullRefreshMinutes=120" in fleet._parameters(config)
    with pytest.raises(FleetManagerError, match="Poll interval"):
        fleet._validate({**config, "full_refresh_minutes": 1})


STATUS = {
    "product": "OpenCDR Fleet Manager",
    "version": "0.1.1",
    "fleet_contract_version": "1.0.0",
    "deployment": {
        "account_id": CONFIG["security_account_id"],
        "home_region": CONFIG["region"],
        "stage": CONFIG["stage"],
    },
}
AGENT = {
    "agent_id": "member-prod",
    "account_id": "222222222222",
    "name": "Member",
    "region": "us-east-1",
    "stage": "prod",
    "api_url": "https://coreabcd.execute-api.us-east-1.amazonaws.com/prod",
    "enabled": True,
    "allow_writes": False,
    "settings_ids": ["global"],
    "integration_ids": [],
}


class Response(BytesIO):
    def __init__(self, body, code=200):
        super().__init__(json.dumps(body).encode())
        self.code = code


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "deployments.json"
    cli.save_registry(
        path,
        {
            "version": 1,
            "deployments": {"core-existing": {"preserved": True}},
            "fleet_managers": {CONFIG["name"]: CONFIG.copy()},
        },
    )
    return path


@pytest.fixture
def invoke(registry):
    def call(*args):
        return cli.main(["--registry", str(registry), "fleet", *args])

    return call


@pytest.fixture
def api(monkeypatch):
    value = Mock()
    value.verify.return_value = STATUS
    value.request.return_value = (200, {})
    monkeypatch.setattr(fleet, "FleetAPI", lambda config: value)
    return value


def test_local_inventory_and_help_need_no_profile_source_or_aws(invoke, monkeypatch, capsys):
    monkeypatch.setattr(fleet, "aws_session", lambda *_: pytest.fail("AWS called"))
    assert invoke("list") == 0
    assert json.loads(capsys.readouterr().out)[CONFIG["name"]]["aws_profile"] == "security-admin"
    with pytest.raises(SystemExit) as result:
        cli.main(["fleet", "agents", "--help"])
    assert result.value.code == 0
    assert "register" in capsys.readouterr().out


def test_reviewed_detection_reconciliation_preserves_body_and_acceptance(invoke, api, tmp_path):
    body = {
        "expected_generation": 12,
        "expected_registration_rev": 3,
        "backfill_job_id": "repair",
        "plan_digest": "a" * 64,
        "reason": "reviewed",
        "accept_unrecoverable": True,
        "request_id": "b" * 32,
    }
    path = tmp_path / "reconcile.json"
    path.write_text(json.dumps(body))
    api.request.return_value = (202, {"state": "accepted", "request_id": body["request_id"]})
    assert (
        invoke(
            "request", "POST", "/v1/agents/member-prod/detections/reconcile", "--file", str(path)
        )
        == 0
    )
    api.verify.assert_called_once()
    api.request.assert_called_once_with("POST", "/v1/agents/member-prod/detections/reconcile", body)


def test_connect_verifies_before_saving_and_preserves_core_registry(invoke, api, registry):
    assert (
        invoke(
            "connect",
            "another",
            "--url",
            CONFIG["api_url"],
            "--profile",
            "reader",
            "--security-account-id",
            CONFIG["security_account_id"],
        )
        == 0
    )
    api.verify.assert_called_once()
    data = cli.load_registry(registry)
    assert data["deployments"] == {"core-existing": {"preserved": True}}
    assert data["fleet_managers"]["another"]["aws_profile"] == "reader"
    assert registry.stat().st_mode & 0o777 == 0o600


def test_reviewed_handoff_body_is_forwarded_exactly(invoke, api, tmp_path):
    body = {
        "expected_generation": 8,
        "expected_registration_rev": 2,
        "handoff_id": "replace",
        "plan_digest": "a" * 64,
        "target_owner": "aws:111111111111:AROAQRSTUVWXYZABCDEF",
        "reason": "reviewed durable storage",
        "request_id": "b" * 32,
    }
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(body))
    api.request.return_value = (202, {"state": "accepted"})
    assert (
        invoke("request", "POST", "/v1/agents/member/detections/handoff", "--file", str(path)) == 0
    )
    api.request.assert_called_once_with("POST", "/v1/agents/member/detections/handoff", body)


def test_connect_failure_never_overwrites_saved_target(invoke, api, registry):
    before = registry.read_text()
    api.verify.side_effect = FleetManagerError("identity mismatch")
    assert (
        invoke(
            "connect",
            CONFIG["name"],
            "--url",
            CONFIG["api_url"],
            "--profile",
            "reader",
            "--security-account-id",
            CONFIG["security_account_id"],
        )
        == 1
    )
    assert registry.read_text() == before


def test_named_fleet_cannot_be_silently_retargeted(invoke, api, registry):
    before = registry.read_text()
    assert (
        invoke(
            "connect",
            CONFIG["name"],
            "--url",
            CONFIG["api_url"],
            "--profile",
            "reader",
            "--security-account-id",
            "999999999999",
        )
        == 1
    )
    api.verify.assert_not_called()
    assert registry.read_text() == before


def test_multiple_fleets_require_explicit_selection(invoke, registry, api):
    data = cli.load_registry(registry)
    data["fleet_managers"]["other"] = {**CONFIG, "name": "other"}
    cli.save_registry(registry, data)
    assert invoke("status") == 1
    assert invoke("status", "--fleet", "other") == 0
    assert api.verify.call_count == 1


def test_identity_is_checked_before_forwarding_and_manager_failures_never_call_core(
    invoke,
    api,
    monkeypatch,
):
    monkeypatch.setattr(cli, "api_environment", lambda *_: pytest.fail("direct Core fallback"))
    api.verify.side_effect = FleetManagerError("manager unavailable")
    assert invoke("agents", "request", "member-prod", "DELETE", "/rules/r1?rule_kind=signal") == 1
    api.request.assert_not_called()


def test_register_only_prepares_keys_when_explicitly_requested(invoke, api, monkeypatch, tmp_path):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(AGENT))
    bootstrap = Mock()
    monkeypatch.setattr(fleet, "_bootstrap", bootstrap)
    api.request.return_value = (201, {"agent": AGENT})
    assert invoke("agents", "register", "--file", str(path)) == 0
    bootstrap.assert_not_called()
    assert invoke("agents", "register", "--file", str(path), "--core-profile", "member-admin") == 0
    bootstrap.assert_called_once()
    api.request.assert_called_with("POST", "/v1/agents", AGENT)


def test_register_rejects_secrets_in_documents(invoke, api, tmp_path):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({**AGENT, "api_key": "must-not-store"}))
    assert invoke("agents", "register", "--file", str(path)) == 1
    api.request.assert_not_called()


def test_update_and_disable_keep_revision_guards(invoke, api, tmp_path):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(AGENT))
    assert (
        invoke("agents", "update", AGENT["agent_id"], "--file", str(path), "--expected-rev", "7")
        == 0
    )
    api.request.assert_called_with("PUT", "/v1/agents/member-prod", {**AGENT, "expected_rev": 7})
    api.request.side_effect = [
        (200, {"agent": {**AGENT, "rev": 8, "created_at": "metadata"}}),
        (200, {"agent": {**AGENT, "enabled": False, "rev": 9}}),
    ]
    assert invoke("agents", "disable", "member-prod", "--expected-rev", "8") == 0
    api.request.assert_called_with(
        "PUT", "/v1/agents/member-prod", {**AGENT, "enabled": False, "expected_rev": 8}
    )


def test_stale_registration_revision_sends_no_write(invoke, api):
    api.request.return_value = (200, {"agent": {**AGENT, "rev": 9}})
    assert invoke("agents", "disable", "member-prod", "--expected-rev", "8") == 1
    assert api.request.call_count == 1


def test_response_envelopes_preserve_unknown_outcomes_partial_results_and_exit_failure(
    invoke,
    api,
    tmp_path,
    capsys,
):
    document = tmp_path / "body.json"
    document.write_text(json.dumps({"expected_rev": 4, "enabled": False}))
    response = {
        "agent_id": "member-prod",
        "status_code": 409,
        "attempted": True,
        "body": {"completed": ["r1"], "message": "revision conflict"},
    }
    api.request.return_value = (409, response)
    assert (
        invoke("agents", "request", "member-prod", "PUT", "/rules/r1", "--file", str(document)) == 1
    )
    api.request.assert_called_once_with(
        "PUT", "/v1/agents/member-prod/api/rules/r1", {"expected_rev": 4, "enabled": False}
    )
    assert json.loads(capsys.readouterr().out) == response


def test_credential_input_is_hidden_and_only_parameter_name_is_printed(
    invoke, api, monkeypatch, capsys
):
    monkeypatch.setattr(fleet.getpass, "getpass", lambda *_: "private-key-value")
    verify = Mock()
    monkeypatch.setattr(fleet, "verify_account", verify)
    ssm = Mock()
    monkeypatch.setattr(fleet, "aws_client", lambda *_: ssm)
    assert invoke("agents", "credentials", "member-prod") == 0
    ssm.put_parameter.assert_called_once_with(
        Name="/opencdr-fleet-manager/prod/agents/member-prod/read-key",
        Value="private-key-value",
        Type="SecureString",
        Overwrite=True,
    )
    assert "private-key-value" not in capsys.readouterr().out


def test_signed_requests_include_session_token_and_exact_query_body(monkeypatch):
    session = Mock()
    session.get_credentials.return_value = Credentials("AKIATEST", "secret", "temporary-token")
    requests = []

    def send(request, timeout):
        requests.append(request)
        assert timeout == 30
        return Response({"ok": True})

    monkeypatch.setattr(transport, "open_request", send)
    api = FleetAPI(CONFIG, session)
    assert api.request(
        "PUT", "/v1/agents/member-prod/api/rules/r1?rule_kind=signal", {"expected_rev": 4}
    ) == (200, {"ok": True})
    request = requests[0]
    assert (
        request.full_url
        == CONFIG["api_url"] + "/v1/agents/member-prod/api/rules/r1?rule_kind=signal"
    )
    assert request.get_header("X-amz-security-token") == "temporary-token"
    assert "/us-east-1/execute-api/aws4_request" in request.get_header("Authorization")
    assert json.loads(request.data) == {"expected_rev": 4}
    assert request.get_header("X-api-key") is None


def test_failed_write_is_not_retried(monkeypatch):
    session = Mock()
    session.get_credentials.return_value = Credentials("AKIATEST", "secret", "token")
    send = Mock(side_effect=URLError("timeout"))
    monkeypatch.setattr(transport, "open_request", send)
    with pytest.raises(FleetManagerError, match="outcome is unknown"):
        FleetAPI(CONFIG, session).request("POST", "/v1/agents/member-prod/api/ir/actions", {})
    send.assert_called_once()


def test_aws_http_error_is_preserved_without_retry(monkeypatch):
    session = Mock()
    session.get_credentials.return_value = Credentials("AKIATEST", "secret", "token")
    body = {"status_code": 409, "attempted": True, "body": {"completed": ["r1"]}}
    send = Mock(
        side_effect=HTTPError(
            CONFIG["api_url"], 409, "Conflict", {}, BytesIO(json.dumps(body).encode())
        )
    )
    monkeypatch.setattr(transport, "open_request", send)
    assert FleetAPI(CONFIG, session).request("PUT", "/v1/agents/member-prod/api/rules/r1", {}) == (
        409,
        body,
    )
    send.assert_called_once()


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.test/v1/agents",
        "//evil/v1/agents",
        "/v1/../foo",
        "/v1/%2e%2e/foo",
        "/v1/%252e%252e/foo",
        "/v1/agents#fragment",
    ],
)
def test_relative_paths_cannot_redirect_credentials(path):
    with pytest.raises(FleetManagerError):
        transport.relative_path(path)


@pytest.mark.parametrize(
    "url",
    [
        "http://bad.example/prod",
        "https://bad.example/prod",
        CONFIG["api_url"] + "?key=secret",
        CONFIG["api_url"] + "/nested",
        "https://abcdefghij.execute-api.us-west-2.amazonaws.com/prod",
    ],
)
def test_signing_endpoint_is_bound_to_region_and_stage(url):
    with pytest.raises(FleetManagerError):
        transport.endpoint(url, "us-east-1", "prod")


@pytest.mark.parametrize(
    "status",
    [
        {**STATUS, "product": "OpenCDR Core"},
        {**STATUS, "fleet_contract_version": "2.0.0"},
        {**STATUS, "deployment": {**STATUS["deployment"], "stage": "dev"}},
    ],
)
def test_manager_identity_must_match_expected_target(status):
    api = FleetAPI(CONFIG, Mock())
    api.request = Mock(return_value=(200, status))
    with pytest.raises(FleetManagerError, match="identity or contract"):
        api.verify()


@pytest.mark.parametrize("identity", [None, "invalid", []])
def test_malformed_manager_identity_is_a_clear_error(identity):
    api = FleetAPI(CONFIG, Mock())
    api.request = Mock(return_value=(200, {**STATUS, "deployment": identity}))
    with pytest.raises(FleetManagerError, match="invalid deployment identity"):
        api.verify()


def test_paginated_inventory_is_complete_or_fails_without_partial_output():
    api = FleetAPI(CONFIG, Mock())
    api.request = Mock(
        side_effect=[
            (200, {"items": [AGENT], "next_token": "cursor/2"}),
            (200, {"items": [], "next_token": None}),
        ]
    )
    assert api.agents() == {"items": [AGENT], "next_token": None}
    assert "cursor%2F2" in api.request.call_args.args[1]
    api.request = Mock(side_effect=[(200, {"items": [AGENT], "next_token": "more"}), (503, {})])
    with pytest.raises(FleetManagerError, match="no partial list"):
        api.agents()
    api.request = Mock(return_value=(200, {"items": [], "next_token": "repeat"}))
    with pytest.raises(FleetManagerError, match="repeated"):
        api.agents()


@pytest.mark.parametrize(
    "body",
    [
        {"results": [{"status_code": 403, "attempted": False}]},
        {"results": [{"status_code": 200, "attempted": True, "body_omitted": True}]},
        {"status_code": 504, "attempted": True, "outcome": "unknown"},
        {"status_code": "invalid", "attempted": True},
    ],
)
def test_partial_and_ambiguous_results_are_not_success(body):
    assert not response_ok(207, body)


def test_deploy_dry_run_does_not_touch_aws_registry_or_install_tools(tmp_path, monkeypatch, capsys):
    path = tmp_path / "registry.json"
    monkeypatch.setattr(fleet, "aws_session", lambda *_: pytest.fail("AWS called"))
    assert (
        cli.main(
            [
                "--registry",
                str(path),
                "fleet",
                "deploy",
                "security-prod",
                "--profile",
                "security-admin",
                "--security-account-id",
                "111111111111",
                "--source-root",
                str(tmp_path / "sources"),
                "--dry-run",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert fleet.REPOSITORY in output and fleet.DEFAULT_VERSION in output
    assert "--param=allowedAgentHosts=" not in output
    assert "--param=pollConcurrency=2" in output
    assert not path.exists() and not (tmp_path / "sources").exists()


@pytest.fixture
def build_tools(tmp_path, monkeypatch):
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "build").mkdir()
    (repo / "build/fleet-manager.zip").write_bytes(b"reviewed zip")
    source = {"commit": "a" * 40}
    monkeypatch.setattr(fleet, "_source", lambda *a, **k: (repo, source))
    monkeypatch.setattr(fleet, "_prepare", lambda *a, **k: repo / ".venv/bin/python")
    session = Mock()
    monkeypatch.setattr(fleet, "aws_session", lambda *_: session)
    monkeypatch.setattr(fleet, "verify_account", Mock())
    monkeypatch.setattr(fleet, "existing_stack", lambda *a: {"StackStatus": "CREATE_COMPLETE"})
    monkeypatch.setattr(fleet, "retained_tables", lambda *a: {})
    cloudformation = Mock()
    session.client.return_value = cloudformation
    cloudformation.get_template.return_value = {"TemplateBody": {"Resources": {}}}
    cloudformation.describe_stacks.return_value = {
        "Stacks": [
            {
                "StackStatus": "UPDATE_COMPLETE",
                "Outputs": [{"OutputKey": "FleetManagerApiUrl", "OutputValue": CONFIG["api_url"]}],
            }
        ]
    }
    monkeypatch.setattr(fleet, "aws_client", lambda *a: cloudformation)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if len(command) > 1 and command[1] == "package":
            output = Path(command[command.index("--package") + 1])
            output.mkdir(parents=True)
            state = {
                "service": {
                    "initialServerlessConfig": {"package": {"artifact": "build/fleet-manager.zip"}}
                }
            }
            (output / "serverless-state.json").write_text(json.dumps(state))
            (output / "cloudformation-template-update-stack.json").write_text("{}")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cli, "run", runner)
    return repo, calls, cloudformation


def test_package_only_then_exact_artifact_deploy_and_hash_guard(
    invoke,
    registry,
    tmp_path,
    build_tools,
    capsys,
):
    repo, calls, cloudformation = build_tools
    artifact = tmp_path / "reviewed"
    assert invoke("deploy", CONFIG["name"], "--package-only", "--output", str(artifact)) == 0
    assert not any(call[0][1] == "deploy" for call in calls)
    assert "No AWS stack was created or updated" in capsys.readouterr().out
    assert (artifact / "fleet-manager.zip").read_bytes() == b"reviewed zip"
    # A later build must not change the ZIP used for this approved package.
    (repo / "build/fleet-manager.zip").write_bytes(b"a later build")
    assert invoke("deploy", CONFIG["name"], "--artifact", str(artifact)) == 0
    deploy = calls[-1][0]
    assert deploy[1] == "deploy" and deploy[deploy.index("--package") + 1] == str(artifact)
    assert (artifact / "fleet-manager.zip").read_bytes() == b"reviewed zip"
    state = json.loads((artifact / "serverless-state.json").read_text())
    assert state["service"]["initialServerlessConfig"]["package"]["artifact"] == str(
        artifact / "fleet-manager.zip"
    )
    assert (
        cli.load_registry(registry)["fleet_managers"][CONFIG["name"]]["api_url"]
        == CONFIG["api_url"]
    )
    (artifact / "fleet-manager.zip").write_bytes(b"tampered")
    count = len(calls)
    assert invoke("deploy", CONFIG["name"], "--artifact", str(artifact)) == 1
    assert len(calls) == count


def test_wrong_account_prevents_any_source_install_or_build(invoke, build_tools, monkeypatch):
    source = Mock(side_effect=pytest.fail)
    monkeypatch.setattr(fleet, "_source", source)
    monkeypatch.setattr(
        fleet, "verify_account", Mock(side_effect=FleetManagerError("wrong account"))
    )
    assert invoke("deploy", CONFIG["name"]) == 1
    source.assert_not_called()


def test_hardened_stack_requires_explicit_downgrade(invoke, tmp_path, build_tools):
    _, calls, cloudformation = build_tools
    cloudformation.get_template.return_value = {
        "TemplateBody": {
            "Conditions": {"Hardened": {"Fn::Equals": ["hardened", "hardened"]}},
            "Resources": {},
        }
    }
    assert invoke("deploy", CONFIG["name"], "--output", str(tmp_path / "artifact")) == 1
    assert not any(call[0][1] == "deploy" for call in calls)


def test_named_profile_removes_ambient_credentials(monkeypatch, tmp_path):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "wrong-account-credential")
    env = fleet._environment(CONFIG, tmp_path)
    assert env["AWS_PROFILE"] == "security-admin"
    assert not any(
        key in env for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
    )


def test_existing_retained_tables_fail_before_attempting_another_deploy(
    invoke, build_tools, monkeypatch
):
    _, calls, _ = build_tools
    monkeypatch.setattr(fleet, "existing_stack", lambda *a: None)
    monkeypatch.setattr(fleet, "retained_tables", lambda *a: {"RegistryTable": {}})
    assert invoke("deploy", CONFIG["name"]) == 1
    assert calls == []


def test_recovery_defaults_to_plan_only(invoke, monkeypatch):
    monkeypatch.setattr(fleet, "aws_session", Mock())
    monkeypatch.setattr(fleet, "verify_account", Mock())
    monkeypatch.setattr(
        fleet,
        "recovery_plan",
        lambda *a, **k: {"deletes_data": False, "template": {"Resources": {}}},
    )
    execute = Mock()
    monkeypatch.setattr(fleet, "apply_recovery", execute)
    assert invoke("recover", CONFIG["name"]) == 0
    execute.assert_not_called()


@pytest.mark.parametrize("invalid", [None, "foreign_account", "role_changed"])
def test_worker_identity_uses_verified_stack_role_without_mutation(
    invoke, registry, monkeypatch, capsys, invalid
):
    before = registry.read_bytes()
    arn = "arn:aws:iam::111111111111:role/path/detection-worker"
    cf, iam = Mock(), Mock()
    cf.describe_stacks.return_value = {
        "Stacks": [
            {
                "Outputs": [
                    {
                        "OutputKey": "DetectionPollRoleArn",
                        "OutputValue": arn
                        if invalid != "foreign_account"
                        else arn.replace("111111111111", "999999999999"),
                    }
                ]
            }
        ]
    }
    iam.get_role.return_value = {
        "Role": {
            "Arn": arn if invalid != "role_changed" else arn + "-different",
            "RoleId": "AROAABCDEFGHIJKLMNOP",
        }
    }
    monkeypatch.setattr(fleet, "aws_session", Mock())
    monkeypatch.setattr(fleet, "verify_account", Mock())
    monkeypatch.setattr(
        fleet, "aws_client", lambda s, name: {"cloudformation": cf, "iam": iam}[name]
    )
    assert invoke("worker-identity", CONFIG["name"]) == (1 if invalid else 0)
    if not invalid:
        assert (
            json.loads(capsys.readouterr().out)["principal"]
            == "aws:111111111111:AROAABCDEFGHIJKLMNOP"
        )
        iam.get_role.assert_called_once_with(RoleName="detection-worker")
    assert registry.read_bytes() == before


def test_recovery_plan_file_cannot_overwrite_reviewed_input(invoke, monkeypatch, tmp_path):
    monkeypatch.setattr(fleet, "aws_session", Mock())
    monkeypatch.setattr(fleet, "verify_account", Mock())
    value = {"plan_digest": "a" * 64}
    monkeypatch.setattr(fleet, "recovery_plan", lambda *a, **k: value)
    path = tmp_path / "reviewed.json"
    assert invoke("recover", CONFIG["name"], "--out", str(path)) == 0
    assert json.loads(path.read_text()) == value
    assert path.stat().st_mode & 0o777 == 0o600
    assert invoke("recover", CONFIG["name"], "--out", str(path)) == 1
    assert json.loads(path.read_text()) == value


@pytest.mark.parametrize("import_only", [False, True])
def test_registry_loss_cannot_bypass_cloud_recovery_binding(
    invoke, tmp_path, build_tools, import_only
):
    _, calls, cf = build_tools
    template = {
        "Resources": {"DetectionBucket": {"Properties": {"BucketName": "retained-evidence"}}}
    }
    if import_only:
        template["Description"] = fleet.IMPORT_DESCRIPTION
    cf.get_template.return_value = {"TemplateBody": template}
    assert invoke("deploy", CONFIG["name"], "--output", str(tmp_path / "artifact")) == 1
    assert not any(call[0][1] == "deploy" for call in calls)


def test_package_only_checks_recovered_storage_before_ready(
    invoke, registry, tmp_path, build_tools
):
    repo, calls, _ = build_tools
    (repo / "serverless.yml").write_text("detectionEvidenceBucketName: ''")
    value = cli.load_registry(registry)
    value["fleet_managers"][CONFIG["name"]]["recovered_detection_bucket"] = "retained-evidence"
    cli.save_registry(registry, value)
    assert invoke("package", CONFIG["name"], "--output", str(tmp_path / "artifact")) == 1
    assert not any(call[0][1] == "deploy" for call in calls)


def test_detection_polling_options_validate_and_forward_independently():
    config = {
        **CONFIG,
        "security_profile": "standard",
        "snapshot_retention_hours": 24,
        "polling_enabled": False,
        "allowed_agent_hosts": "",
        "detection_poll_interval_minutes": 2,
        "detection_retention_days": 180,
        "detection_polling_enabled": True,
    }
    fleet._validate(config)
    values = fleet._parameters(config)
    assert "--param=pollingEnabled=false" in values
    assert "--param=detectionPollingEnabled=true" in values
    assert "--param=detectionPollIntervalMinutes=2" in values
    assert "--param=detectionRetentionDays=180" in values
    for key, value in (
        ("detection_poll_interval_minutes", 0),
        ("detection_retention_days", 366),
        ("detection_poll_concurrency", 1),
        ("detection_polling_enabled", "false"),
    ):
        with pytest.raises(FleetManagerError):
            fleet._validate({**config, key: value})


def test_reviewed_feed_recovery_preserves_exact_body_and_async_receipt(invoke, api, tmp_path):
    body = {
        "expected_generation": 8,
        "expected_registration_rev": 2,
        "backfill_job_id": "history",
        "plan_digest": "a" * 64,
        "target_epoch": "b" * 32,
        "target_owner": "aws:111111111111:AROAABCDEFGHIJKLMNOP",
        "reason": "reviewed",
        "request_id": "c" * 32,
        "accept_unrecoverable": True,
    }
    path = tmp_path / "recovery.json"
    path.write_text(json.dumps(body))
    api.request.return_value = (202, {"state": "accepted", "refresh_queued": False})
    assert (
        invoke("request", "POST", "/v1/agents/member/detections/recover", "--file", str(path)) == 0
    )
    api.request.assert_called_once_with("POST", "/v1/agents/member/detections/recover", body)


def test_triage_onboarding_requires_and_reviews_exact_new_core_trust_principals(
    monkeypatch, capsys
):
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    from opencdr_deploy import fleet_manager as fleet
    from opencdr_deploy.fleet_api import FleetManagerError

    role_names = [
        "PollRoleArn",
        "DetectionPollRoleArn",
        "ManagementObserverRoleArn",
        "ManagementOperatorRoleArn",
        "ManagementResponderRoleArn",
        "ManagementAdministratorRoleArn",
        "RolloutOperatorRoleArn",
        "RolloutResponderRoleArn",
        "RolloutAdministratorRoleArn",
    ]
    outputs = {name: "arn:aws:iam::123456789012:role/" + name for name in role_names}
    cloud = Mock()

    def response():
        return {
            "Stacks": [
                {"Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()]}
            ]
        }

    cloud.describe_stacks.side_effect = lambda **_: response()
    monkeypatch.setattr(fleet, "aws_session", Mock())
    monkeypatch.setattr(fleet, "aws_client", lambda *args: cloud)
    monkeypatch.setattr(fleet, "verify_account", Mock())
    monkeypatch.setattr(
        fleet,
        "_registration",
        lambda _: {
            "auth_mode": "iam",
            "allow_triage": True,
            "account_id": "111111111111",
            "region": "us-east-1",
        },
    )
    args = SimpleNamespace(file="reviewed.json", core_profile="core")
    config = {"security_account_id": "123456789012", "stage": "test"}
    with pytest.raises(FleetManagerError, match="Upgrade Fleet"):
        fleet._bootstrap(args, config, Mock())
    for name in ["ManagementTriageRoleArn", "ApiRoleArn"]:
        outputs[name] = "arn:aws:iam::123456789012:role/" + name
    assert fleet._bootstrap(args, config, Mock()) is False
    plan = json.loads(capsys.readouterr().out)
    assert plan["credentials_created"] is False
    assert outputs["ManagementTriageRoleArn"] in plan["principal_arns"]
    assert outputs["ApiRoleArn"] in plan["principal_arns"]


@pytest.mark.parametrize(
    "path",
    [
        "/v1/cases",
        "/v1/case-access",
        "/v1/library/items",
        "/v1/library/releases",
        "/v1/library/items/" + "a" * 32 + "/versions",
    ],
)
def test_deploy_signed_fleet_case_operations_stay_at_fleet_authority(monkeypatch, path):
    session = Mock()
    session.get_credentials.return_value = Credentials("id", "secret", "token")
    send = Mock(return_value=Response({"ok": True}))
    monkeypatch.setattr(transport, "open_request", send)
    body = {"expected_rev": 1, "request_id": "a" * 32, "reason": "reviewed"}
    FleetAPI(CONFIG, session).request("PUT" if path.endswith("access") else "POST", path, body)
    request = send.call_args.args[0]
    assert request.full_url == CONFIG["api_url"] + path
    assert json.loads(request.data) == body
    assert request.get_header("X-api-key") is None
    send.assert_called_once()


def test_dedicated_fleet_connector_connection_is_signed_and_stays_central(monkeypatch):
    session = Mock()
    session.get_credentials.return_value = Credentials("id", "secret", "token")
    send = Mock(return_value=Response({"rev": 2}))
    monkeypatch.setattr(transport, "open_request", send)
    path = "/v1/cases/" + "b" * 32 + "/external-status"
    FleetAPI({**CONFIG, "access": "integration"}, session).request("POST", path, {"sequence": 1})
    request = send.call_args.args[0]
    assert request.full_url == CONFIG["api_url"] + "/management/integration" + path
    assert request.get_header("X-api-key") is None
    send.assert_called_once()


def test_legacy_deploy_cannot_remove_operational_ownership_storage(invoke, tmp_path, build_tools):
    _, calls, cf = build_tools
    cf.get_template.return_value = {"TemplateBody": {"Resources": {"CaseTable": {}}}}
    assert invoke("deploy", CONFIG["name"], "--output", str(tmp_path / "artifact")) == 1
    assert not any("deploy" in command for command, _ in calls)
