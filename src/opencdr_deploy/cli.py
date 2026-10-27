#!/usr/bin/env python3
"""Install and manage multiple named OpenCDR deployments from one computer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from . import __version__
from .fleet import FleetConfigError, load_fleet_config
from .sources import (
    DEFAULT_CORE_REPOSITORY,
    DEFAULT_SOURCE_ROOT,
    SourceError,
    install_core_source,
    installed_versions,
)

DEFAULT_REGISTRY = Path(
    os.environ.get("OPENCDR_DEPLOY_REGISTRY", "~/.config/opencdr/deployments.json")
).expanduser()
DEFAULT_ARTIFACT_ROOT = Path(
    os.environ.get("OPENCDR_ARTIFACT_ROOT", "~/.cache/opencdr/artifacts")
).expanduser()
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SECRET_RE = re.compile(r"(secret|password|token|credential|api.?key)", re.IGNORECASE)
KNOWN_FEATURES = {
    "archiveEnabled": {"true", "false"},
    "xrayEnabled": {"true", "false"},
    "securityProfile": {"standard", "hardened"},
}
SCRIPT_CONTEXT: dict[str, list[tuple[str, str]]] = {
    "load_rules.sh": [("--stage", "stage"), ("--region", "region")],
    "test_deployed.sh": [("--stage", "stage"), ("--region", "region")],
    "cost_report.sh": [("--stage", "stage"), ("--region", "region")],
    "setup_region_forwarding.sh": [("--stage", "stage"), ("--home-region", "region")],
    "setup_org_forwarding.sh": [("--stage", "stage"), ("--central-region", "region")],
    "check_security_profile_transition.sh": [
        ("--stage", "stage"),
        ("--region", "region"),
        ("--target", "securityProfile"),
    ],
}
API_SCRIPTS = {"opencdr.py", "check_s3_ingestion.py"}


class DeployError(RuntimeError):
    """A user-facing deployment-manager error."""


class APIResponseError(DeployError):
    """A deployed OpenCDR API request returned an error."""


def _path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "deployments": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DeployError(f"Cannot read deployment registry {path}: {exc}") from exc
    if data.get("version") != 1 or not isinstance(data.get("deployments"), dict):
        raise DeployError(f"Unsupported deployment registry format in {path}")
    return data


def save_registry(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def require_name(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise DeployError(
            "Deployment names must be 1-64 letters, numbers, dots, underscores or hyphens"
        )
    return name


def validate_parameter(key: str, value: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key):
        raise DeployError(f"Invalid Serverless parameter name: {key!r}")
    if SECRET_RE.search(key):
        raise DeployError(
            f"Refusing to store potentially sensitive parameter {key!r}; use environment/SSM"
        )
    allowed = KNOWN_FEATURES.get(key)
    if allowed and value not in allowed:
        raise DeployError(f"{key} must be one of: {', '.join(sorted(allowed))}")
    if "\n" in value or "\r" in value:
        raise DeployError(f"Parameter {key!r} cannot contain newlines")


def parse_assignment(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise DeployError(f"Expected NAME=VALUE, got {raw!r}")
    key, value = raw.split("=", 1)
    validate_parameter(key, value)
    return key, value


def validate_deployment(deployment: dict[str, Any], *, require_repository: bool = True) -> None:
    for field in ("repository", "stage", "region"):
        if not isinstance(deployment.get(field), str) or not deployment[field].strip():
            raise DeployError(f"Deployment field {field!r} is required")
    if not NAME_RE.fullmatch(deployment["stage"]):
        raise DeployError("Stage must contain only letters, numbers, dots, underscores or hyphens")
    if not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", deployment["region"]):
        raise DeployError(f"Invalid AWS region name: {deployment['region']!r}")
    if require_repository:
        repo = _path(deployment["repository"])
        if not (repo / "serverless.yml").is_file() or not (repo / "scripts").is_dir():
            raise DeployError(f"Not an OpenCDR repository: {repo}")
    expected_account_id = deployment.get("expected_account_id")
    if expected_account_id is not None and not re.fullmatch(r"\d{12}", expected_account_id):
        raise DeployError("expected_account_id must contain exactly 12 digits")
    parameters = deployment.setdefault("parameters", {})
    if not isinstance(parameters, dict):
        raise DeployError("Deployment parameters must be an object")
    for key, value in parameters.items():
        validate_parameter(key, str(value))


def require_deployment(registry: dict[str, Any], name: str) -> dict[str, Any]:
    try:
        deployment = registry["deployments"][name]
    except KeyError as exc:
        raise DeployError(f"Unknown deployment {name!r}; run 'installations list'") from exc
    validate_deployment(deployment)
    return deployment


def repository(deployment: dict[str, Any]) -> Path:
    return _path(deployment["repository"])


def stack_name(deployment: dict[str, Any]) -> str:
    return f"opencdr-{deployment['stage']}"


def deployment_env(deployment: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    profile = deployment.get("aws_profile")
    if profile:
        env["AWS_PROFILE"] = profile
        env["AWS_DEFAULT_PROFILE"] = profile
    env["AWS_REGION"] = deployment["region"]
    env["AWS_DEFAULT_REGION"] = deployment["region"]
    return env


def run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = False,
    dry_run: bool = False,
    conceal_arguments: bool = False,
) -> subprocess.CompletedProcess[str]:
    printable = command[0] + " [arguments concealed]" if conceal_arguments else " ".join(command)
    if dry_run:
        print(f"Would run in {cwd}: {printable}")
        return subprocess.CompletedProcess(command, 0, "", "")
    print(f"+ {printable}", flush=True)
    return subprocess.run(
        list(command), cwd=cwd, env=env, check=True, text=True, capture_output=capture
    )


def parameter_args(deployment: dict[str, Any]) -> list[str]:
    return [
        f"--param={key}={value}" for key, value in sorted(deployment.get("parameters", {}).items())
    ]


def git_revision(repo: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], cwd=repo, capture=True).stdout.strip()


def git_status(repo: Path) -> str:
    return run(["git", "status", "--porcelain"], cwd=repo, capture=True).stdout


def hash_directory(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(p for p in path.rglob("*") if p.is_file() and p.name != "manifest.json")
    for item in files:
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        with item.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def artifact_path(name: str, repo: Path, root: Path) -> Path:
    revision = git_revision(repo)[:12]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return root / name / f"{stamp}-{revision}"


def package_deployment(
    name: str,
    deployment: dict[str, Any],
    destination: Path | None,
    artifact_root: Path,
    *,
    dry_run: bool = False,
    allow_dirty: bool = False,
) -> Path:
    repo = repository(deployment)
    source_status = "" if dry_run else git_status(repo)
    if source_status and not allow_dirty:
        raise DeployError(
            "Repository has uncommitted changes; commit them or use --allow-dirty for a "
            "development-only artifact"
        )
    output = _path(destination or artifact_path(name, repo, artifact_root))
    if output.exists() and any(output.iterdir()):
        raise DeployError(f"Artifact directory is not empty: {output}")
    if not dry_run:
        output.mkdir(parents=True, exist_ok=True)
        os.chmod(output, 0o700)
    command = [
        "npx",
        "serverless",
        "package",
        "--stage",
        deployment["stage"],
        "--region",
        deployment["region"],
        "--package",
        str(output),
        *parameter_args(deployment),
    ]
    run(
        command,
        cwd=repo,
        env=deployment_env(deployment),
        dry_run=dry_run,
        conceal_arguments=True,
    )
    if dry_run:
        return output
    if git_status(repo) != source_status:
        raise DeployError("Repository changed while packaging; refusing an inconsistent artifact")
    os.chmod(output, 0o700)
    manifest = {
        "schema": 1,
        "deployment": name,
        "repository": str(repo),
        "stage": deployment["stage"],
        "region": deployment["region"],
        "parameters": deployment.get("parameters", {}),
        "git_revision": git_revision(repo),
        "source_dirty": bool(source_status),
        "source_status_sha256": (
            hashlib.sha256(source_status.encode()).hexdigest() if source_status else None
        ),
        "created_at": datetime.now(UTC).isoformat(),
        "artifact_sha256": hash_directory(output),
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.chmod(manifest_path, 0o600)
    print(f"Packaged {name!r} as {output}")
    return output


def read_manifest(path: Path, name: str, deployment: dict[str, Any]) -> dict[str, Any]:
    manifest_path = path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DeployError(f"Invalid or missing artifact manifest: {manifest_path}") from exc
    expected = {
        "deployment": name,
        "stage": deployment["stage"],
        "region": deployment["region"],
        "parameters": deployment.get("parameters", {}),
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise DeployError(
                f"Artifact {key} does not match deployment registry: "
                f"{manifest.get(key)!r} != {value!r}"
            )
    if manifest.get("artifact_sha256") != hash_directory(path):
        raise DeployError("Artifact contents changed after packaging; refusing deployment")
    return manifest


def transition_check(
    deployment: dict[str, Any], *, allow_downgrade: bool, dry_run: bool = False
) -> None:
    repo = repository(deployment)
    target = deployment.get("parameters", {}).get("securityProfile", "standard")
    command = [
        str(repo / "scripts" / "check_security_profile_transition.sh"),
        "--stage",
        deployment["stage"],
        "--region",
        deployment["region"],
        "--target",
        target,
    ]
    if allow_downgrade:
        command.append("--allow-downgrade")
    run(command, cwd=repo, env=deployment_env(deployment), dry_run=dry_run)


def deploy_artifact(
    name: str,
    deployment: dict[str, Any],
    artifact: Path,
    *,
    allow_downgrade: bool,
    dry_run: bool,
) -> None:
    repo = repository(deployment)
    artifact = _path(artifact)
    if not dry_run:
        manifest = read_manifest(artifact, name, deployment)
        print(
            f"Deploying immutable artifact for {manifest['git_revision'][:12]} "
            f"({manifest['artifact_sha256'][:12]})"
        )
    transition_check(deployment, allow_downgrade=allow_downgrade, dry_run=dry_run)
    run(
        [
            "npx",
            "serverless",
            "deploy",
            "--conceal",
            "--stage",
            deployment["stage"],
            "--region",
            deployment["region"],
            "--package",
            str(artifact),
        ],
        cwd=repo,
        env=deployment_env(deployment),
        dry_run=dry_run,
    )


def aws_json(deployment: dict[str, Any], arguments: list[str]) -> Any:
    result = run(
        ["aws", *arguments, "--region", deployment["region"], "--output", "json"],
        cwd=repository(deployment),
        env=deployment_env(deployment),
        capture=True,
    )
    return json.loads(result.stdout)


def verify_expected_account(deployment: dict[str, Any]) -> str:
    expected = deployment.get("expected_account_id")
    if not expected:
        return ""
    identity = aws_json(deployment, ["sts", "get-caller-identity"])
    actual = str(identity.get("Account", ""))
    if actual != expected:
        raise DeployError(
            f"AWS account mismatch for profile {deployment.get('aws_profile') or '<default>'}: "
            f"expected {expected}, got {actual or '<unknown>'}"
        )
    return actual


def prepare_deployment(
    deployment: dict[str, Any], *, runtime_only: bool, skip_submodules: bool, dry_run: bool
) -> None:
    repo = repository(deployment)
    if not skip_submodules:
        run(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=repo,
            env=deployment_env(deployment),
            dry_run=dry_run,
        )
    run(["npm", "ci"], cwd=repo, env=deployment_env(deployment), dry_run=dry_run)
    python = shutil.which("python3.12")
    if not python:
        raise DeployError("Python 3.12 was not found")
    run([python, "-m", "venv", ".venv"], cwd=repo, dry_run=dry_run)
    requirements = "requirements.txt" if runtime_only else "requirements-dev.txt"
    run(
        [
            str(repo / ".venv" / "bin" / "pip"),
            "install",
            "--require-hashes",
            "-r",
            requirements,
        ],
        cwd=repo,
        dry_run=dry_run,
    )


def build_deployment(
    *,
    name: str,
    stage: str,
    region: str,
    aws_profile: str | None,
    expected_account_id: str | None,
    parameters: dict[str, str],
    local_repository: str | None,
    core_version: str | None,
    core_repository_url: str,
    source_root: Path,
    dry_run: bool,
) -> dict[str, Any]:
    if local_repository and core_version:
        raise DeployError("Choose either --repository or --core-version, not both")
    source = None
    if core_version:
        managed_path, source = install_core_source(
            name=name,
            version=core_version,
            repository_url=core_repository_url,
            root=source_root,
            runner=run,
            dry_run=dry_run,
        )
        source["root"] = str(source_root.expanduser().resolve())
        repo = managed_path
    else:
        repo = _path(local_repository or Path.cwd())
    item: dict[str, Any] = {
        "repository": str(repo),
        "stage": stage,
        "region": region,
        "parameters": parameters,
    }
    if aws_profile:
        item["aws_profile"] = aws_profile
    if expected_account_id:
        item["expected_account_id"] = expected_account_id
    if source:
        item["source"] = source
    validate_deployment(item, require_repository=not (dry_run and core_version is not None))
    return item


def deployment_status(name: str, deployment: dict[str, Any]) -> dict[str, Any]:
    response = aws_json(
        deployment,
        ["cloudformation", "describe-stacks", "--stack-name", stack_name(deployment)],
    )
    stacks = response.get("Stacks", [])
    if not stacks:
        raise DeployError(f"Stack {stack_name(deployment)} was not found")
    stack = stacks[0]
    outputs = {item["OutputKey"]: item.get("OutputValue") for item in stack.get("Outputs", [])}
    return {
        "deployment": name,
        "account": aws_json(deployment, ["sts", "get-caller-identity"]).get("Account"),
        "stack": stack_name(deployment),
        "stack_status": stack.get("StackStatus"),
        "stage": deployment["stage"],
        "region": deployment["region"],
        "aws_profile": deployment.get("aws_profile"),
        "configured_parameters": deployment.get("parameters", {}),
        "deployed_security_profile": outputs.get("CoreSecurityProfile", "standard"),
        "deployed_archive_enabled": outputs.get("ArchiveEnabled", "true") == "true",
        "deployed_xray_enabled": outputs.get("XRayEnabled", "true") == "true",
        "service_endpoint": outputs.get("ServiceEndpoint"),
    }


def script_command(repo: Path, script: Path) -> list[str]:
    if script.suffix == ".py":
        candidate = repo / ".venv" / "bin" / "python"
        return [str(candidate if candidate.exists() else Path(sys.executable)), str(script)]
    if script.suffix == ".sh":
        return ["bash", str(script)]
    raise DeployError("Only .py and .sh files inside the repository scripts directory are allowed")


def available_scripts(repo: Path) -> list[str]:
    return sorted(
        path.name for path in (repo / "scripts").iterdir() if path.suffix in {".py", ".sh"}
    )


def resolve_script(repo: Path, name: str) -> Path:
    candidate = (repo / "scripts" / Path(name).name).resolve()
    scripts_dir = (repo / "scripts").resolve()
    if candidate.parent != scripts_dir or not candidate.is_file():
        raise DeployError(f"Unknown operational script {name!r}; run 'scripts list'")
    return candidate


def context_arguments(script: str, deployment: dict[str, Any], arguments: list[str]) -> list[str]:
    injected: list[str] = []
    context = dict(deployment)
    context["securityProfile"] = deployment.get("parameters", {}).get("securityProfile", "standard")
    for option, field in SCRIPT_CONTEXT.get(script, []):
        if option not in arguments:
            injected.extend([option, str(context[field])])
    return [*injected, *arguments]


def api_environment(deployment: dict[str, Any]) -> dict[str, str]:
    env = deployment_env(deployment)
    for suffix, variable, decrypt in (
        ("api-url", "OPENCDR_API_URL", False),
        ("api-key", "OPENCDR_API_KEY", True),
    ):
        command = [
            "aws",
            "ssm",
            "get-parameter",
            "--name",
            f"/{stack_name(deployment)}/{suffix}",
            "--query",
            "Parameter.Value",
            "--output",
            "text",
            "--region",
            deployment["region"],
        ]
        if decrypt:
            command.append("--with-decryption")
        result = run(command, cwd=repository(deployment), env=env, capture=True)
        env[variable] = result.stdout.strip()
    return env


def _query_assignments(values: list[str]) -> list[tuple[str, str]]:
    result = []
    for value in values:
        if "=" not in value:
            raise DeployError(f"Invalid query parameter {value!r}; expected KEY=VALUE")
        key, item = value.split("=", 1)
        if not key:
            raise DeployError("Query parameter names cannot be empty")
        result.append((key, item))
    return result


def _request_body(args: argparse.Namespace) -> Any | None:
    source = args.data
    if args.file:
        try:
            source = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        except OSError as exc:
            raise DeployError(f"Cannot read request body: {exc}") from exc
    if source is None:
        return None
    try:
        return json.loads(source)
    except json.JSONDecodeError as exc:
        raise DeployError(f"Request body is not valid JSON: {exc}") from exc


def api_request(args: argparse.Namespace, deployment: dict[str, Any]) -> int:
    parsed = urlsplit(args.path)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        raise DeployError(
            "API path must be relative to the selected deployment and start with one '/'"
        )
    if parsed.fragment:
        raise DeployError("API paths cannot contain a fragment")
    query = [*parse_qsl(parsed.query, keep_blank_values=True), *_query_assignments(args.query)]
    path = urlunsplit(("", "", parsed.path, urlencode(query), ""))
    body = _request_body(args)
    if body is not None and args.method in {"GET", "DELETE"}:
        raise DeployError(f"{args.method} requests cannot include a JSON body")

    env = api_environment(deployment)
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(
        env["OPENCDR_API_URL"].rstrip("/") + path,
        data=payload,
        method=args.method,
        headers={"x-api-key": env["OPENCDR_API_KEY"], "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=args.timeout) as response:
            raw = response.read().decode()
    except HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            detail = json.loads(raw).get("message", raw)
        except (json.JSONDecodeError, AttributeError):
            detail = raw
        raise APIResponseError(f"API HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise APIResponseError(f"API request failed: {exc.reason}") from exc

    if not raw:
        return 0
    try:
        print(json.dumps(json.loads(raw), indent=2, default=str))
    except json.JSONDecodeError:
        print(raw)
    return 0


def cmd_installations(args: argparse.Namespace, registry: dict[str, Any], path: Path) -> int:
    if args.installations_command == "list":
        rows = [
            {
                "name": name,
                "stage": item["stage"],
                "region": item["region"],
                "aws_profile": item.get("aws_profile"),
                "expected_account_id": item.get("expected_account_id"),
                "repository": item["repository"],
                "core_version": (item.get("source") or {}).get("version"),
            }
            for name, item in sorted(registry["deployments"].items())
        ]
        print(json.dumps(rows, indent=2))
        return 0
    if args.installations_command == "show":
        print(json.dumps(require_deployment(registry, args.name), indent=2, sort_keys=True))
        return 0
    if args.installations_command == "add":
        name = require_name(args.name)
        if name in registry["deployments"] and not args.replace:
            raise DeployError(f"Deployment {name!r} already exists; use --replace to update it")
        parameters = dict(parse_assignment(item) for item in args.param)
        for key, value in (
            ("archiveEnabled", args.archive),
            ("xrayEnabled", args.xray),
            ("securityProfile", args.security_profile),
        ):
            if value is not None:
                validate_parameter(key, value)
                parameters[key] = value
        item = build_deployment(
            name=name,
            stage=args.stage,
            region=args.region,
            aws_profile=args.aws_profile,
            expected_account_id=args.expected_account_id,
            parameters=parameters,
            local_repository=args.repository,
            core_version=args.core_version,
            core_repository_url=args.core_repository_url,
            source_root=_path(args.source_root),
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            registry["deployments"][name] = item
            save_registry(path, registry)
            print(f"Saved deployment {name!r} in {path}")
        else:
            print(json.dumps({"name": name, **item}, indent=2, sort_keys=True))
        return 0
    if args.installations_command == "versions":
        item = require_deployment(registry, args.name)
        source = item.get("source") or {}
        root = _path(args.source_root or source.get("root") or DEFAULT_SOURCE_ROOT)
        versions = installed_versions(root, args.name)
        active = source.get("version")
        for version in versions:
            version["active"] = version["version"] == active
        print(json.dumps(versions, indent=2, sort_keys=True))
        return 0
    if args.installations_command == "upgrade":
        item = require_deployment(registry, args.name)
        current_source = item.get("source")
        if not current_source:
            raise DeployError("A local-repository installation cannot be upgraded automatically")
        root = _path(args.source_root or current_source.get("root") or DEFAULT_SOURCE_ROOT)
        repo_url = args.core_repository_url or current_source["repository_url"]
        managed_path, source = install_core_source(
            name=args.name,
            version=args.core_version,
            repository_url=repo_url,
            root=root,
            runner=run,
            dry_run=args.dry_run,
        )
        source["root"] = str(root)
        updated = {**item, "repository": str(managed_path), "source": source}
        validate_deployment(updated, require_repository=not args.dry_run)
        if args.dry_run:
            print(json.dumps({"name": args.name, **updated}, indent=2, sort_keys=True))
            return 0
        registry["deployments"][args.name] = updated
        save_registry(path, registry)
        print(f"Updated {args.name!r} to Core {args.core_version}; previous versions retained")
        if args.prepare:
            prepare_deployment(updated, runtime_only=False, skip_submodules=False, dry_run=False)
        return 0
    if args.installations_command == "remove":
        require_deployment(registry, args.name)
        if not args.yes:
            raise DeployError(
                "Removing a registry entry requires --yes; AWS resources are untouched"
            )
        del registry["deployments"][args.name]
        save_registry(path, registry)
        print(f"Removed local registry entry {args.name!r}; AWS resources were not changed")
        return 0
    raise DeployError("An installations command is required")


def cmd_apply(args: argparse.Namespace, registry: dict[str, Any], registry_path: Path) -> int:
    config_path = _path(args.file)
    specs = load_fleet_config(config_path)
    selected = set(args.select)
    available = {spec["name"] for spec in specs}
    missing = selected - available
    if missing:
        raise DeployError(f"Selected deployments not found in config: {', '.join(sorted(missing))}")
    if selected:
        specs = [spec for spec in specs if spec["name"] in selected]

    resolved: list[tuple[str, dict[str, Any]]] = []
    failures: list[tuple[str, str]] = []
    for spec in specs:
        name = require_name(spec["name"])
        try:
            local_repository = spec["repository"]
            if local_repository and not Path(local_repository).expanduser().is_absolute():
                local_repository = str((config_path.parent / local_repository).resolve())
            source_root = spec["source_root"]
            if source_root and not Path(source_root).expanduser().is_absolute():
                source_root = str((config_path.parent / source_root).resolve())
            item = build_deployment(
                name=name,
                stage=spec["stage"],
                region=spec["region"],
                aws_profile=spec["aws_profile"],
                expected_account_id=spec["expected_account_id"],
                parameters=spec["parameters"],
                local_repository=local_repository,
                core_version=spec["core_version"],
                core_repository_url=spec["core_repository_url"] or DEFAULT_CORE_REPOSITORY,
                source_root=_path(source_root or DEFAULT_SOURCE_ROOT),
                dry_run=args.dry_run,
            )
            resolved.append((name, item))
        except (DeployError, SourceError, subprocess.CalledProcessError) as exc:
            failures.append((name, str(exc)))
            print(f"FAIL  {name}: {exc}", file=sys.stderr)
            if not args.continue_on_error:
                return 1

    if not args.dry_run:
        for name, item in resolved:
            registry["deployments"][name] = item
        save_registry(registry_path, registry)

    for name, item in resolved:
        try:
            if args.prepare:
                prepare_deployment(
                    item,
                    runtime_only=args.runtime_only,
                    skip_submodules=args.skip_submodules,
                    dry_run=args.dry_run,
                )
            if args.deploy:
                if not args.dry_run:
                    verify_expected_account(item)
                artifact = package_deployment(
                    name,
                    item,
                    None,
                    _path(args.artifact_root),
                    dry_run=args.dry_run,
                    allow_dirty=args.allow_dirty,
                )
                deploy_artifact(
                    name,
                    item,
                    artifact,
                    allow_downgrade=args.allow_protection_downgrade,
                    dry_run=args.dry_run,
                )
            print(f"PASS  {name}")
        except (DeployError, SourceError, subprocess.CalledProcessError) as exc:
            failures.append((name, str(exc)))
            print(f"FAIL  {name}: {exc}", file=sys.stderr)
            if not args.continue_on_error:
                return 1

    print(
        json.dumps(
            {
                "configured": [name for name, _item in resolved],
                "failed": [{"name": name, "error": error} for name, error in failures],
                "dry_run": args.dry_run,
            },
            indent=2,
        )
    )
    return 1 if failures else 0


def cmd_features(
    args: argparse.Namespace,
    registry: dict[str, Any],
    registry_path: Path,
    name: str,
    deployment: dict[str, Any],
) -> int:
    if args.features_command == "show":
        print(json.dumps(deployment.get("parameters", {}), indent=2, sort_keys=True))
        return 0
    updated = dict(deployment.get("parameters", {}))
    for assignment in args.assignments:
        key, value = parse_assignment(assignment)
        updated[key] = value
    deployment["parameters"] = updated
    save_registry(registry_path, registry)
    print(f"Updated desired parameters for {name!r}")
    if args.deploy:
        if not args.dry_run:
            verify_expected_account(deployment)
        artifact = package_deployment(
            name,
            deployment,
            None,
            _path(args.artifact_root),
            dry_run=args.dry_run,
            allow_dirty=args.allow_dirty,
        )
        deploy_artifact(
            name,
            deployment,
            artifact,
            allow_downgrade=args.allow_protection_downgrade,
            dry_run=args.dry_run,
        )
    return 0


def cmd_doctor(deployment: dict[str, Any]) -> int:
    repo = repository(deployment)
    checks: list[tuple[str, bool, str]] = []
    for executable in ("git", "node", "npm", "npx", "python3.12", "docker", "aws", "jq"):
        found = shutil.which(executable)
        checks.append((executable, bool(found), found or "not found"))
    for required in ("serverless.yml", "package-lock.json", "requirements.txt"):
        exists = (repo / required).is_file()
        checks.append((required, exists, "present" if exists else "missing"))
    credentials_ok = False
    identity = "unavailable"
    if shutil.which("aws"):
        try:
            value = aws_json(deployment, ["sts", "get-caller-identity"])
            actual = str(value.get("Account", ""))
            expected = deployment.get("expected_account_id")
            credentials_ok = not expected or actual == expected
            identity = f"account={actual} arn={value.get('Arn')}"
            if expected:
                identity += f" expected_account={expected}"
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            pass
    checks.append(("AWS credentials", credentials_ok, identity))
    for label, success, detail in checks:
        print(f"{'PASS' if success else 'FAIL'}  {label}: {detail}")
    print("INFO  Customer prerequisites such as CloudTrail are documented separately.")
    return 0 if all(success for _, success, _ in checks) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--registry", default=str(DEFAULT_REGISTRY), help="named deployment registry"
    )
    parser.add_argument("--deployment", help="named deployment to operate on")
    commands = parser.add_subparsers(dest="command", required=True)

    installations = commands.add_parser("installations", help="manage named deployments")
    installation_commands = installations.add_subparsers(
        dest="installations_command", required=True
    )
    installation_commands.add_parser("list")
    show = installation_commands.add_parser("show")
    show.add_argument("name")
    add = installation_commands.add_parser("add")
    add.add_argument("name")
    source = add.add_mutually_exclusive_group()
    source.add_argument("--repository", help="existing local Core checkout")
    source.add_argument("--core-version", help="managed immutable Core tag, e.g. v1.2.1")
    add.add_argument("--core-repository-url", default=DEFAULT_CORE_REPOSITORY)
    add.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    add.add_argument("--stage", required=True)
    add.add_argument("--region", default="us-east-1")
    add.add_argument("--aws-profile")
    add.add_argument("--expected-account-id")
    add.add_argument("--archive", choices=("true", "false"))
    add.add_argument("--xray", choices=("true", "false"))
    add.add_argument("--security-profile", choices=("standard", "hardened"))
    add.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    add.add_argument("--replace", action="store_true")
    add.add_argument("--dry-run", action="store_true")
    versions = installation_commands.add_parser("versions")
    versions.add_argument("name")
    versions.add_argument("--source-root")
    upgrade = installation_commands.add_parser("upgrade")
    upgrade.add_argument("name")
    upgrade.add_argument("--core-version", required=True)
    upgrade.add_argument("--core-repository-url")
    upgrade.add_argument("--source-root")
    upgrade.add_argument("--prepare", action="store_true")
    upgrade.add_argument("--dry-run", action="store_true")
    remove = installation_commands.add_parser("remove")
    remove.add_argument("name")
    remove.add_argument("--yes", action="store_true")

    apply = commands.add_parser("apply", help="reconcile one or more deployments from TOML")
    apply.add_argument("--file", required=True, type=Path)
    apply.add_argument("--select", action="append", default=[], metavar="NAME")
    apply.add_argument("--prepare", action="store_true")
    apply.add_argument("--deploy", action="store_true")
    apply.add_argument("--runtime-only", action="store_true")
    apply.add_argument("--skip-submodules", action="store_true")
    apply.add_argument("--continue-on-error", action="store_true")
    apply.add_argument("--allow-protection-downgrade", action="store_true")
    apply.add_argument("--allow-dirty", action="store_true")
    apply.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    apply.add_argument("--dry-run", action="store_true")

    commands.add_parser("doctor", help="validate local tools and AWS identity")
    prepare = commands.add_parser("prepare", help="install locked local dependencies")
    prepare.add_argument("--runtime-only", action="store_true")
    prepare.add_argument("--skip-submodules", action="store_true")
    prepare.add_argument("--dry-run", action="store_true")

    package = commands.add_parser("package", help="create an immutable deployment artifact")
    package.add_argument("--output", type=Path)
    package.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    package.add_argument("--allow-dirty", action="store_true")
    package.add_argument("--dry-run", action="store_true")

    deploy = commands.add_parser("deploy", help="deploy an exact packaged artifact")
    deploy.add_argument("--artifact", type=Path, help="existing artifact; otherwise package first")
    deploy.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    deploy.add_argument("--allow-protection-downgrade", action="store_true")
    deploy.add_argument("--allow-dirty", action="store_true")
    deploy.add_argument("--dry-run", action="store_true")
    commands.add_parser("status", help="show the selected live stack")

    features = commands.add_parser("features", help="inspect or update deployment parameters")
    feature_commands = features.add_subparsers(dest="features_command", required=True)
    feature_commands.add_parser("show")
    feature_set = feature_commands.add_parser("set")
    feature_set.add_argument("assignments", nargs="+", metavar="NAME=VALUE")
    feature_set.add_argument("--deploy", action="store_true")
    feature_set.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    feature_set.add_argument("--allow-protection-downgrade", action="store_true")
    feature_set.add_argument("--allow-dirty", action="store_true")
    feature_set.add_argument("--dry-run", action="store_true")

    scripts = commands.add_parser("scripts", help="run repository operational scripts")
    script_commands = scripts.add_subparsers(dest="scripts_command", required=True)
    script_commands.add_parser("list")
    script_run = script_commands.add_parser("run")
    script_run.add_argument("--dry-run", action="store_true")
    script_run.add_argument("script")
    script_run.add_argument("arguments", nargs=argparse.REMAINDER)

    api = commands.add_parser("api", help="manage the selected deployment's complete REST API")
    api_commands = api.add_subparsers(dest="api_command", required=True)
    request = api_commands.add_parser("request", help="call any relative REST API route")
    request.add_argument("method", type=str.upper, choices=["GET", "POST", "PUT", "DELETE"])
    request.add_argument("path", help="relative path, for example /rules/schema")
    request.add_argument("--query", action="append", default=[], metavar="KEY=VALUE")
    request.add_argument("--file", help="JSON request body file, or - for stdin")
    request.add_argument("--data", help="inline JSON request body")
    request.add_argument("--timeout", type=float, default=30.0)
    core_cli = api_commands.add_parser("core-cli", help="run this Core version's friendly API CLI")
    core_cli.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    registry_path = _path(args.registry)
    try:
        registry = load_registry(registry_path)
        if args.command == "installations":
            return cmd_installations(args, registry, registry_path)
        if args.command == "apply":
            return cmd_apply(args, registry, registry_path)
        if not args.deployment:
            raise DeployError(f"{args.command!r} requires --deployment NAME")
        name = require_name(args.deployment)
        deployment = require_deployment(registry, name)
        repo = repository(deployment)

        if args.command == "doctor":
            return cmd_doctor(deployment)
        if args.command == "prepare":
            prepare_deployment(
                deployment,
                runtime_only=args.runtime_only,
                skip_submodules=args.skip_submodules,
                dry_run=args.dry_run,
            )
            return 0
        if args.command == "package":
            package_deployment(
                name,
                deployment,
                args.output,
                _path(args.artifact_root),
                dry_run=args.dry_run,
                allow_dirty=args.allow_dirty,
            )
            return 0
        if args.command == "deploy":
            if not args.dry_run:
                verify_expected_account(deployment)
            artifact = args.artifact or package_deployment(
                name,
                deployment,
                None,
                _path(args.artifact_root),
                dry_run=args.dry_run,
                allow_dirty=args.allow_dirty,
            )
            deploy_artifact(
                name,
                deployment,
                artifact,
                allow_downgrade=args.allow_protection_downgrade,
                dry_run=args.dry_run,
            )
            return 0
        if args.command == "status":
            verify_expected_account(deployment)
            print(json.dumps(deployment_status(name, deployment), indent=2, sort_keys=True))
            return 0
        if args.command == "features":
            return cmd_features(args, registry, registry_path, name, deployment)
        if args.command == "scripts":
            if args.scripts_command == "list":
                print("\n".join(available_scripts(repo)))
                return 0
            script = resolve_script(repo, args.script)
            arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            arguments = context_arguments(script.name, deployment, arguments)
            env = (
                api_environment(deployment)
                if script.name in API_SCRIPTS and not args.dry_run
                else deployment_env(deployment)
            )
            if not args.dry_run:
                verify_expected_account(deployment)
            run(
                [*script_command(repo, script), *arguments],
                cwd=repo,
                env=env,
                dry_run=args.dry_run,
                conceal_arguments=True,
            )
            return 0
        if args.command == "api":
            verify_expected_account(deployment)
            if args.api_command == "request":
                if args.file and args.data:
                    raise DeployError("--file and --data are mutually exclusive")
                return api_request(args, deployment)
            arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            script = repo / "scripts" / "opencdr.py"
            run(
                [*script_command(repo, script), *arguments],
                cwd=repo,
                env=api_environment(deployment),
                conceal_arguments=True,
            )
            return 0
        raise DeployError(f"Unsupported command: {args.command}")
    except APIResponseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (DeployError, FleetConfigError, SourceError) as exc:
        parser.error(str(exc))
    except subprocess.CalledProcessError as exc:
        return exc.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
