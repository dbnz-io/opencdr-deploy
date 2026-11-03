"""API-only operations must not obtain AWS credentials or touch a Core checkout."""

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from opencdr_deploy import cli


class Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


@pytest.fixture
def connection(tmp_path):
    path = tmp_path / "core.json"
    path.write_text(json.dumps({"url": "https://core.example/dev", "key": "dedicated-key"}))
    path.chmod(0o600)
    return path


def test_api_only_needs_no_registry_aws_repo_or_environment_credentials(
    connection, monkeypatch, capsys,
):
    for name in ("load_registry", "run", "verify_expected_account", "require_deployment"):
        monkeypatch.setattr(cli, name, lambda *a, **k: pytest.fail("API-only called AWS/registry"))
    monkeypatch.setenv("OPENCDR_API_URL", "https://wrong.example")
    monkeypatch.setenv("OPENCDR_API_KEY", "wrong-key")
    requests = []

    def request(req, timeout):
        requests.append(req)
        return Response({"status": "ok"})

    monkeypatch.setattr(cli, "urlopen", request)
    assert cli.main(["api", "request", "GET", "/status", "--connection", str(connection)]) == 0
    assert len(requests) == 1
    assert requests[0].full_url == "https://core.example/dev/status"
    assert requests[0].get_header("X-api-key") == "dedicated-key"
    assert "dedicated-key" not in capsys.readouterr().out


@pytest.mark.parametrize("matching", [True, False])
def test_expected_identity_is_verified_before_a_mutation(connection, monkeypatch, matching):
    identity = {"account_id": "111111111111", "home_region": "us-east-1", "stage": "dev"}
    config = json.loads(connection.read_text())
    config["deployment"] = identity
    connection.write_text(json.dumps(config))
    requests = []

    def request(req, timeout):
        requests.append(req)
        if req.full_url.endswith("/status"):
            return Response({"deployment": {**identity, "stage": "dev" if matching else "prod"}})
        return Response({"rev": 2})

    monkeypatch.setattr(cli, "urlopen", request)
    result = cli.main([
        "api", "request", "PUT", "/rules/example", "--connection", str(connection),
        "--data", '{"expected_rev": 1}',
    ])
    assert result == (0 if matching else 1)
    assert len(requests) == (2 if matching else 1)
    assert requests[0].get_method() == "GET"


def test_cannot_mix_an_explicit_api_target_with_an_aws_target(connection, monkeypatch):
    monkeypatch.setattr(cli, "urlopen", lambda *a, **k: pytest.fail("ambiguous target sent"))
    with pytest.raises(SystemExit) as error:
        cli.main([
            "--deployment", "different", "api", "request", "GET", "/status",
            "--connection", str(connection),
        ])
    assert error.value.code == 2


@pytest.mark.parametrize("url", [
    "http://insecure.example", "file:///private/key", "https://user:password@core.example",
    "https://core.example/?key=secret", "https://core.example/#fragment", "https://core.example:bad",
])
def test_invalid_connection_does_not_send_credentials(connection, monkeypatch, url):
    connection.write_text(json.dumps({"url": url, "key": "dedicated-key"}))
    monkeypatch.setattr(cli, "urlopen", lambda *a, **k: pytest.fail("credentials sent"))
    with pytest.raises(cli.DeployError, match="HTTPS"):
        cli.api_connection(str(connection))


def test_api_key_cannot_follow_a_redirect():
    assert cli._NoAPIRedirects().redirect_request(
        None, None, 302, "Found", {}, "https://different.example"
    ) is None


def test_partial_agent_result_survives_http_error(connection, monkeypatch, capsys):
    def request(req, timeout):
        raise HTTPError(req.full_url, 409, "Conflict", {}, BytesIO(json.dumps({
            "message": "conflict", "completed": [{"rule_id": "already-applied"}],
        }).encode()))

    monkeypatch.setattr(cli, "urlopen", request)
    assert cli.main([
        "api", "request", "POST", "/rule-releases/apply", "--connection", str(connection),
        "--data", "{}",
    ]) == 1
    assert "already-applied" in capsys.readouterr().err


def test_typo_in_identity_guard_is_rejected(connection):
    connection.write_text(json.dumps({
        "url": "https://core.example", "key": "key", "deployment": {"account": "111111111111"},
    }))
    with pytest.raises(cli.DeployError, match="account_id"):
        cli.api_connection(str(connection))
