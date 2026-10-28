from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import opencdr_deploy
from opencdr_deploy import cli as deploy

ROOT = Path(__file__).parent / "fixtures" / "core"


def test_package_version_matches_project_metadata():
    import tomllib

    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert opencdr_deploy.__version__ == project["project"]["version"]


def test_cli_reports_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        deploy.build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip().endswith(f" {opencdr_deploy.__version__}")


def test_mcp_install_uses_current_python_and_exact_version(monkeypatch, capsys):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deploy, "run", fake_run)
    monkeypatch.setattr(deploy, "installed_mcp_version", lambda: "0.1.0")
    monkeypatch.setattr(deploy, "mcp_executable", lambda: "/venv/bin/opencdr-mcp")

    assert deploy.main(["mcp", "install", "--version", "0.1.0", "--upgrade"]) == 0
    command, kwargs = calls[0]
    assert command == [
        deploy.sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--upgrade",
        "opencdr-mcp==0.1.0",
    ]
    assert kwargs["cwd"] == Path.cwd()
    assert "Installed opencdr-mcp 0.1.0" in capsys.readouterr().out


def test_mcp_install_dry_run_does_not_verify_install(monkeypatch, capsys):
    monkeypatch.setattr(
        deploy,
        "installed_mcp_version",
        lambda: pytest.fail("dry-run must not inspect an installation"),
    )
    assert deploy.main(["mcp", "install", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert f"{deploy.sys.executable} -m pip install" in output
    assert output.rstrip().endswith("opencdr-mcp")


def test_mcp_install_rejects_unsafe_version():
    with pytest.raises(deploy.DeployError, match="unsupported characters"):
        deploy.mcp_package_spec("0.1.0 /tmp/package")


def test_mcp_status_reports_absent_package(monkeypatch, capsys):
    monkeypatch.setattr(deploy, "installed_mcp_version", lambda: None)
    assert deploy.main(["mcp", "status"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "executable": None,
        "installed": False,
        "python": deploy.sys.executable,
        "version": None,
    }


def test_mcp_status_reports_package_and_executable(monkeypatch, capsys):
    monkeypatch.setattr(deploy, "installed_mcp_version", lambda: "0.1.0")
    monkeypatch.setattr(deploy, "mcp_executable", lambda: "/venv/bin/opencdr-mcp")
    assert deploy.main(["mcp", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["installed"] is True
    assert status["version"] == "0.1.0"
    assert status["executable"] == "/venv/bin/opencdr-mcp"


def deployment(repo: Path = ROOT):
    return {
        "repository": str(repo),
        "stage": "prod",
        "region": "eu-west-1",
        "aws_profile": "customer-a",
        "parameters": {
            "archiveEnabled": "false",
            "securityProfile": "hardened",
            "xrayEnabled": "true",
        },
    }


def test_registry_supports_multiple_repositories_and_is_private(tmp_path):
    path = tmp_path / "deployments.json"
    data = {
        "version": 1,
        "deployments": {"customer-a": deployment(), "customer-b": deployment(tmp_path)},
    }
    deploy.save_registry(path, data)
    assert deploy.load_registry(path) == data
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("key", "value"),
    [("archiveEnabled", "maybe"), ("securityProfile", "maximum"), ("apiKey", "x")],
)
def test_parameter_validation_rejects_unsafe_values(key, value):
    with pytest.raises(deploy.DeployError):
        deploy.validate_parameter(key, value)


def test_future_non_secret_feature_flags_are_supported():
    assert deploy.parse_assignment("newFeature=enabled") == ("newFeature", "enabled")


def test_parameter_arguments_are_stable():
    assert deploy.parameter_args(deployment()) == [
        "--param=archiveEnabled=false",
        "--param=securityProfile=hardened",
        "--param=xrayEnabled=true",
    ]


def test_environment_isolated_by_profile_and_region():
    env = deploy.deployment_env(deployment())
    assert env["AWS_PROFILE"] == "customer-a"
    assert env["AWS_DEFAULT_PROFILE"] == "customer-a"
    assert env["AWS_REGION"] == "eu-west-1"
    assert env["AWS_DEFAULT_REGION"] == "eu-west-1"


def test_expected_account_guard_fails_closed(monkeypatch):
    item = deployment()
    item["expected_account_id"] = "111111111111"
    monkeypatch.setattr(
        deploy,
        "aws_json",
        lambda *_args, **_kwargs: {"Account": "222222222222"},
    )
    with pytest.raises(deploy.DeployError, match="AWS account mismatch"):
        deploy.verify_expected_account(item)


def test_apply_registers_multiple_local_installations(tmp_path):
    registry = tmp_path / "registry.json"
    config = tmp_path / "fleet.toml"
    config.write_text(
        f"""
version = 1

[defaults]
repository = "{ROOT}"
region = "us-east-1"

[[deployments]]
name = "account-a"
stage = "prod"
aws_profile = "account-a"
expected_account_id = "111111111111"

[[deployments]]
name = "account-b"
stage = "staging"
aws_profile = "account-b"
expected_account_id = "222222222222"

[deployments.features]
archive = false
"""
    )

    assert deploy.main(["--registry", str(registry), "apply", "--file", str(config)]) == 0
    stored = deploy.load_registry(registry)["deployments"]
    assert set(stored) == {"account-a", "account-b"}
    assert stored["account-a"]["aws_profile"] == "account-a"
    assert stored["account-b"]["parameters"]["archiveEnabled"] == "false"


def test_apply_dry_run_does_not_write_registry(tmp_path):
    registry = tmp_path / "registry.json"
    config = tmp_path / "fleet.toml"
    config.write_text(
        f"""
version = 1

[[deployments]]
name = "production"
stage = "prod"
region = "us-east-1"
repository = "{ROOT}"
"""
    )
    assert (
        deploy.main(["--registry", str(registry), "apply", "--file", str(config), "--dry-run"]) == 0
    )
    assert not registry.exists()


def test_deployment_validation_rejects_invalid_region():
    item = deployment()
    item["region"] = "not a region"
    with pytest.raises(deploy.DeployError, match="Invalid AWS region"):
        deploy.validate_deployment(item)


def test_operational_script_context_is_injected_but_can_be_overridden():
    actual = deploy.context_arguments("load_rules.sh", deployment(), ["--dry-run"])
    assert actual == ["--stage", "prod", "--region", "eu-west-1", "--dry-run"]
    overridden = deploy.context_arguments(
        "load_rules.sh", deployment(), ["--stage", "staging", "--dry-run"]
    )
    assert overridden == ["--region", "eu-west-1", "--stage", "staging", "--dry-run"]


def test_script_resolution_cannot_escape_scripts_directory():
    with pytest.raises(deploy.DeployError):
        deploy.resolve_script(ROOT, "../serverless.yml")


def test_artifact_manifest_detects_changes(tmp_path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    template = artifact / "cloudformation-template-update-stack.json"
    template.write_text("{}")
    manifest = {
        "schema": 1,
        "deployment": "customer-a",
        "stage": "prod",
        "region": "eu-west-1",
        "parameters": deployment()["parameters"],
        "artifact_sha256": deploy.hash_directory(artifact),
    }
    (artifact / "manifest.json").write_text(json.dumps(manifest))
    assert deploy.read_manifest(artifact, "customer-a", deployment()) == manifest
    template.write_text('{"changed": true}')
    with pytest.raises(deploy.DeployError, match="changed after packaging"):
        deploy.read_manifest(artifact, "customer-a", deployment())


def test_package_uses_serverless_flags_and_writes_manifest(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, "abc123\n", "")
        if command[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["npx", "serverless", "package"]:
            (artifact / "template.json").write_text("{}")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deploy, "run", fake_run)
    result = deploy.package_deployment("customer-a", deployment(), artifact, tmp_path / "cache")
    assert result == artifact
    command = next(call for call, _kwargs in calls if call[:3] == ["npx", "serverless", "package"])
    assert command[:3] == ["npx", "serverless", "package"]
    assert "--param=archiveEnabled=false" in command
    assert json.loads((artifact / "manifest.json").read_text())["git_revision"] == "abc123"
    assert artifact.stat().st_mode & 0o777 == 0o700
    assert (artifact / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_package_refuses_dirty_source_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "git_status", lambda _repo: " M serverless.yml\n")
    with pytest.raises(deploy.DeployError, match="uncommitted changes"):
        deploy.package_deployment(
            "customer-a", deployment(), tmp_path / "artifact", tmp_path / "cache"
        )


def test_package_dry_run_does_not_create_artifact_directory(tmp_path, monkeypatch):
    output = tmp_path / "missing" / "artifact"
    monkeypatch.setattr(
        deploy, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0)
    )
    assert (
        deploy.package_deployment(
            "customer-a", deployment(), output, tmp_path / "cache", dry_run=True
        )
        == output
    )
    assert not output.exists()


def test_deploy_validates_manifest_before_transition(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "template.json").write_text("{}")
    (artifact / "manifest.json").write_text(
        json.dumps(
            {
                "deployment": "another-deployment",
                "stage": "prod",
                "region": "eu-west-1",
                "parameters": deployment()["parameters"],
                "artifact_sha256": deploy.hash_directory(artifact),
            }
        )
    )
    monkeypatch.setattr(deploy, "transition_check", lambda *args, **kwargs: pytest.fail())
    with pytest.raises(deploy.DeployError, match="does not match"):
        deploy.deploy_artifact(
            "customer-a", deployment(), artifact, allow_downgrade=False, dry_run=False
        )


def test_available_scripts_contains_all_python_and_shell_entrypoints():
    expected = {path.name for path in (ROOT / "scripts").iterdir() if path.suffix in {".py", ".sh"}}
    assert set(deploy.available_scripts(ROOT)) == expected


def test_live_status_includes_feature_outputs(monkeypatch):
    responses = iter(
        [
            {
                "Stacks": [
                    {
                        "StackStatus": "UPDATE_COMPLETE",
                        "Outputs": [
                            {"OutputKey": "CoreSecurityProfile", "OutputValue": "hardened"},
                            {"OutputKey": "ArchiveEnabled", "OutputValue": "false"},
                            {"OutputKey": "XRayEnabled", "OutputValue": "true"},
                        ],
                    }
                ]
            },
            {"Account": "123456789012"},
        ]
    )
    monkeypatch.setattr(deploy, "aws_json", lambda *_args, **_kwargs: next(responses))
    status = deploy.deployment_status("customer-a", deployment())
    assert status["deployed_security_profile"] == "hardened"
    assert status["deployed_archive_enabled"] is False
    assert status["deployed_xray_enabled"] is True


def test_api_environment_reads_selected_deployment_ssm_parameters(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        value = (
            "https://api.example" if any("api-url" in part for part in command) else "private-key"
        )
        return subprocess.CompletedProcess(command, 0, value + "\n", "")

    monkeypatch.setattr(deploy, "run", fake_run)
    env = deploy.api_environment(deployment())

    assert env["OPENCDR_API_URL"] == "https://api.example"
    assert env["OPENCDR_API_KEY"] == "private-key"
    assert all("--region" in command and "eu-west-1" in command for command, _ in commands)
    assert "--with-decryption" not in commands[0][0]
    assert "--with-decryption" in commands[1][0]


def test_deployment_manager_core_cli_conceals_child_arguments(tmp_path, monkeypatch):
    registry = tmp_path / "deployments.json"
    deploy.save_registry(registry, {"version": 1, "deployments": {"customer-a": deployment()}})
    calls = []
    monkeypatch.setattr(deploy, "api_environment", lambda _deployment: {"API": "configured"})

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deploy, "run", fake_run)
    result = deploy.main(
        [
            "--registry",
            str(registry),
            "--deployment",
            "customer-a",
            "api",
            "core-cli",
            "--",
            "settings",
            "get",
        ]
    )

    assert result == 0
    command, kwargs = calls[0]
    assert command[-2:] == ["settings", "get"]
    assert kwargs["conceal_arguments"] is True


def test_manage_is_first_class_alias_for_core_management_cli(tmp_path, monkeypatch):
    registry = tmp_path / "deployments.json"
    deploy.save_registry(registry, {"version": 1, "deployments": {"customer-a": deployment()}})
    calls = []
    monkeypatch.setattr(deploy, "api_environment", lambda _deployment: {"API": "configured"})

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deploy, "run", fake_run)
    result = deploy.main(
        [
            "--registry",
            str(registry),
            "--deployment",
            "customer-a",
            "manage",
            "rules",
            "list",
            "--kind",
            "signal",
        ]
    )

    assert result == 0
    command, kwargs = calls[0]
    assert command[-4:] == ["rules", "list", "--kind", "signal"]
    assert kwargs["env"] == {"API": "configured", "OPENCDR_DEPLOY_WRAPPED": "1"}
    assert kwargs["conceal_arguments"] is True


def test_catalog_load_injects_selected_stage_and_region(tmp_path, monkeypatch):
    registry = tmp_path / "deployments.json"
    deploy.save_registry(registry, {"version": 1, "deployments": {"customer-a": deployment()}})
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deploy, "run", fake_run)
    assert (
        deploy.main(
            [
                "--registry",
                str(registry),
                "--deployment",
                "customer-a",
                "catalog",
                "load",
                "--dry-run",
            ]
        )
        == 0
    )
    command, kwargs = calls[0]
    assert command[-5:] == ["--stage", "prod", "--region", "eu-west-1", "--dry-run"]
    assert kwargs["conceal_arguments"] is True


def test_management_workflow_verifies_account_before_reading_api_secret(monkeypatch):
    order = []
    monkeypatch.setattr(deploy, "verify_expected_account", lambda _item: order.append("verify"))
    monkeypatch.setattr(
        deploy,
        "api_environment",
        lambda _item: order.append("credentials") or {},
    )
    monkeypatch.setattr(
        deploy,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )

    deploy.run_operational_workflow(deployment(), "opencdr.py", ["status"], needs_api=True)

    assert order == ["verify", "credentials"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--deployment", "x", "manage"],
        ["--deployment", "x", "catalog", "load"],
        ["--deployment", "x", "test", "local"],
        ["--deployment", "x", "test", "deployed"],
        ["--deployment", "x", "coverage", "regions"],
        ["--deployment", "x", "coverage", "accounts"],
        ["--deployment", "x", "cost"],
        ["--deployment", "x", "ingestion", "canary", "integration-id"],
    ],
)
def test_operator_workflow_commands_parse(argv):
    assert deploy.build_parser().parse_args(argv).command in {
        "manage",
        "catalog",
        "test",
        "coverage",
        "cost",
        "ingestion",
    }


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["--deployment", "x", "test", "local", "--event", "009", "--rule", "abc"],
            ["--event", "009", "--rule", "abc"],
        ),
        (
            ["--deployment", "x", "coverage", "regions", "--regions", "eu-west-1"],
            ["--regions", "eu-west-1"],
        ),
        (
            ["--deployment", "x", "cost", "--granularity", "MONTHLY"],
            ["--granularity", "MONTHLY"],
        ),
    ],
)
def test_operator_workflow_argument_order_is_preserved(argv, expected):
    parser = deploy.build_parser()
    args = parser.parse_args(deploy.normalize_operator_argv(argv))
    assert deploy.child_arguments(args.arguments) == expected


class _Response:
    def __init__(self, body: str):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body.encode()


def test_native_api_request_uses_selected_credentials_and_encodes_query(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        deploy,
        "api_environment",
        lambda _deployment: {
            "OPENCDR_API_URL": "https://api.example/base",
            "OPENCDR_API_KEY": "private-key",
        },
    )

    def fake_urlopen(request, timeout):
        captured.update(request=request, timeout=timeout)
        return _Response('{"ok": true}')

    monkeypatch.setattr(deploy, "urlopen", fake_urlopen)
    args = deploy.build_parser().parse_args(
        [
            "--deployment",
            "customer-a",
            "api",
            "request",
            "GET",
            "/rules?rule_kind=signal",
            "--query",
            "next_token=a value",
        ]
    )
    assert deploy.api_request(args, deployment()) == 0
    assert captured["request"].full_url == (
        "https://api.example/base/rules?rule_kind=signal&next_token=a+value"
    )
    assert captured["request"].get_header("X-api-key") == "private-key"
    assert json.loads(capsys.readouterr().out) == {"ok": True}


@pytest.mark.parametrize(
    "path", ["rules", "https://other.example/rules", "//other.example/rules", "/rules#x"]
)
def test_native_api_rejects_paths_outside_selected_deployment(path, monkeypatch):
    monkeypatch.setattr(deploy, "api_environment", lambda _deployment: pytest.fail())
    args = deploy.build_parser().parse_args(
        ["--deployment", "customer-a", "api", "request", "GET", path]
    )
    with pytest.raises(deploy.DeployError):
        deploy.api_request(args, deployment())


def test_native_api_reads_json_file(monkeypatch, tmp_path):
    body = tmp_path / "body.json"
    body.write_text('{"rule": {"rule_id": "example"}}')
    captured = {}
    monkeypatch.setattr(
        deploy,
        "api_environment",
        lambda _deployment: {
            "OPENCDR_API_URL": "https://api.example",
            "OPENCDR_API_KEY": "private-key",
        },
    )

    def fake_urlopen(request, timeout):
        captured["data"] = json.loads(request.data)
        return _Response('{"valid": true}')

    monkeypatch.setattr(deploy, "urlopen", fake_urlopen)
    args = deploy.build_parser().parse_args(
        [
            "--deployment",
            "customer-a",
            "api",
            "request",
            "POST",
            "/rules/validate",
            "--file",
            str(body),
        ]
    )
    assert deploy.api_request(args, deployment()) == 0
    assert captured["data"] == {"rule": {"rule_id": "example"}}
