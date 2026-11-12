"""Named Fleet Manager deployments and operations for the installed CLI."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from botocore.exceptions import BotoCoreError, ClientError

from .fleet_api import (
    FleetAPI,
    FleetManagerError,
    agent_id,
    aws_client,
    aws_session,
    endpoint,
    relative_path,
    response_ok,
    verify_account,
)
from .fleet_recovery import (
    IMPORT_DESCRIPTION,
    apply_recovery,
    existing_stack,
    pinned_bucket_name,
    recovery_plan,
    retained_tables,
    validate_plan,
    verify_recovery_template,
)
from .sources import SourceError, install_core_source, validate_repository_url, validate_version

REPOSITORY = "https://github.com/dbnz-io/opencdr-fleet-manager.git"
DEFAULT_VERSION = "v0.2.0"
SOURCE_ROOT = Path(
    os.environ.get("OPENCDR_FLEET_SOURCE_ROOT", "~/.local/share/opencdr-deploy/fleet-manager")
).expanduser()
ARTIFACT_ROOT = Path(
    os.environ.get("OPENCDR_FLEET_ARTIFACT_ROOT", "~/.cache/opencdr/artifacts/fleet-manager")
).expanduser()
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
STAGE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
REGION = re.compile(r"[a-z]{2}-[a-z]+-\d")
REGISTRATION_FIELDS = {
    "agent_id",
    "name",
    "account_id",
    "region",
    "stage",
    "api_url",
    "enabled",
    "allow_writes",
    "allow_triage",
    "settings_ids",
    "integration_ids",
    "auth_mode",
    "write_access",
}


def configure_parser(commands) -> None:
    fleet = commands.add_parser("fleet", help="deploy and operate an optional Fleet Manager")
    actions = fleet.add_subparsers(dest="fleet_command", required=True)
    actions.add_parser("list", help="list saved fleet managers (no AWS calls)")
    worker_identity = actions.add_parser(
        "worker-identity", help="read the deployed detection worker's immutable IAM identity"
    )
    worker_identity.add_argument("name")
    connect = actions.add_parser("connect", help="verify and save an existing Fleet Manager")
    connect.add_argument("name")
    connect.add_argument("--url", required=True)
    connect.add_argument("--profile", required=True)
    connect.add_argument("--security-account-id", required=True)
    connect.add_argument("--region", default="us-east-1")
    connect.add_argument("--stage", default="prod")
    connect.add_argument(
        "--access",
        choices=["observer", "triage", "operator", "responder", "administrator", "integration"],
        default="observer",
    )
    recover = actions.add_parser(
        "recover", help="review and reattach retained Fleet tables and paired evidence storage"
    )
    recover.add_argument("name")
    recover.add_argument("--profile")
    recover.add_argument("--security-account-id")
    recover.add_argument("--region")
    recover.add_argument("--stage")
    recover.add_argument(
        "--evidence-bucket", help="original bucket if deleted-stack discovery is unavailable"
    )
    recover.add_argument("--out", type=Path, help="save the read-only recovery plan for review")
    recover.add_argument("--plan", type=Path, help="saved plan to execute or resume")
    recover.add_argument(
        "--expected-plan-digest", help="exact reviewed SHA-256 from the saved plan"
    )
    recover.add_argument(
        "--execute",
        action="store_true",
        help="apply the import-only recovery; default prints its plan",
    )
    for action in ("deploy", "package"):
        parser = actions.add_parser(
            action,
            help=(
                "build and deploy Fleet Manager"
                if action == "deploy"
                else "build without deploying"
            ),
        )
        parser.add_argument("name")
        parser.add_argument("--profile")
        parser.add_argument("--security-account-id")
        parser.add_argument("--region")
        parser.add_argument("--stage")
        parser.add_argument(
            "--access",
            choices=["observer", "triage", "operator", "responder", "administrator", "integration"],
        )
        parser.add_argument(
            "--version", dest="fleet_version", help="Fleet Manager tag, e.g. v0.1.0"
        )
        parser.add_argument("--repository-url", help="source repository; default is dbnz-io's repo")
        parser.add_argument("--source-root", type=Path)
        parser.add_argument("--python", help="Python 3.12+ executable for build tools")
        parser.add_argument("--security-profile", choices=["standard", "hardened"])
        parser.add_argument("--snapshot-retention-hours", type=int)
        parser.add_argument("--poll-concurrency", type=_positive)
        parser.add_argument("--poll-interval-minutes", type=_positive)
        parser.add_argument("--full-refresh-minutes", type=_positive)
        parser.add_argument("--detection-poll-interval-minutes", type=_positive)
        parser.add_argument("--detection-retention-days", type=_positive)
        parser.add_argument("--detection-poll-concurrency", type=_positive)
        detections = parser.add_mutually_exclusive_group()
        detections.add_argument(
            "--pause-detection-polling", dest="detection_polling_enabled", action="store_false"
        )
        detections.add_argument(
            "--enable-detection-polling", dest="detection_polling_enabled", action="store_true"
        )
        parser.set_defaults(detection_polling_enabled=None)
        parser.add_argument("--allowed-agent-hosts")
        poll = parser.add_mutually_exclusive_group()
        poll.add_argument("--pause-polling", dest="polling_enabled", action="store_false")
        poll.add_argument("--enable-polling", dest="polling_enabled", action="store_true")
        parser.set_defaults(polling_enabled=None)
        parser.add_argument("--package-only", action="store_true")
        parser.add_argument(
            "--artifact", type=Path, help="deploy this exact previously built package"
        )
        parser.add_argument("--output", type=Path, help="new, empty artifact directory")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--allow-protection-downgrade",
            action="store_true",
            help="explicitly permit hardened → standard",
        )
    status = actions.add_parser("status", help="verify live Fleet Manager identity and version")
    status.add_argument("--fleet", help="saved fleet name; optional when exactly one exists")
    request = actions.add_parser("request", help="call any /v1 Fleet Manager API route")
    request.add_argument("method", choices=["GET", "POST", "PUT", "DELETE"])
    request.add_argument("path")
    _target(request, body=True)
    agents = actions.add_parser("agents", help="manage agent registrations and Core operations")
    agent_commands = agents.add_subparsers(dest="agent_command", required=True)
    listing = agent_commands.add_parser("list", help="list every registered agent")
    _target(listing)
    for action in ("show", "refresh", "enable", "disable", "configuration", "credentials"):
        command = agent_commands.add_parser(action)
        command.add_argument("agent", type=_agent_argument)
        _target(command)
        if action in {"enable", "disable"}:
            command.add_argument("--expected-rev", type=_positive, required=True)
        if action == "configuration":
            command.add_argument("--kind")
            command.add_argument("--limit", type=_page_limit, default=20)
            command.add_argument("--next-token")
        if action == "credentials":
            command.add_argument("--kind", choices=["read", "write"], default="read")
    register = agent_commands.add_parser("register", help="register a reviewed agent JSON file")
    _target(register)
    register.add_argument("--file", required=True)
    register.add_argument(
        "--core-profile", help="prepare an IAM trust plan or explicit legacy credentials"
    )
    bootstrap = agent_commands.add_parser(
        "bootstrap", help="prepare an IAM trust plan or explicit legacy credentials"
    )
    _target(bootstrap)
    bootstrap.add_argument("--file", required=True)
    bootstrap.add_argument("--core-profile", required=True)
    update = agent_commands.add_parser("update", help="replace registration with a revision guard")
    update.add_argument("agent", type=_agent_argument)
    update.add_argument("--file", required=True)
    update.add_argument("--expected-rev", type=_positive, required=True)
    _target(update)
    invoke = agent_commands.add_parser(
        "request", help="forward a Core API call through Fleet Manager"
    )
    invoke.add_argument("agent", type=_agent_argument)
    invoke.add_argument("method", choices=["GET", "POST", "PUT", "DELETE"])
    invoke.add_argument("path", help="relative Core path, e.g. /rules?rule_kind=signal")
    _target(invoke, body=True)


def _positive(value: str) -> int:
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Must be a positive integer")


def _page_limit(value: str) -> int:
    number = _positive(value)
    if number > 50:
        raise argparse.ArgumentTypeError("Page limit must be 1–50")
    return number


def _agent_argument(value: str) -> str:
    try:
        return agent_id(value)
    except FleetManagerError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _target(parser, body=False):
    parser.add_argument("--fleet", help="saved fleet name; optional when exactly one exists")
    if body:
        parser.add_argument("--file", help="JSON body file, or - for stdin")


def _validate(config: dict[str, Any]) -> None:
    for field, pattern in (("name", NAME), ("stage", STAGE), ("region", REGION)):
        if not isinstance(config.get(field), str) or not pattern.fullmatch(config[field]):
            raise FleetManagerError(f"Invalid fleet {field}")
    if not re.fullmatch(r"\d{12}", str(config.get("security_account_id", ""))):
        raise FleetManagerError("--security-account-id must contain 12 digits")
    if not isinstance(config.get("aws_profile"), str) or not config["aws_profile"].strip():
        raise FleetManagerError("A named AWS --profile is required")
    if config.get("security_profile", "standard") not in {"standard", "hardened"}:
        raise FleetManagerError("Invalid security profile")
    retention = config.get("snapshot_retention_hours", 24)
    if not isinstance(retention, int) or isinstance(retention, bool) or not 2 <= retention <= 168:
        raise FleetManagerError("Snapshot retention must be 2–168 hours")
    interval = config.get("poll_interval_minutes", 1)
    refresh = config.get("full_refresh_minutes", 60)
    if (
        not isinstance(interval, int)
        or isinstance(interval, bool)
        or not 1 <= interval <= 60
        or not isinstance(refresh, int)
        or isinstance(refresh, bool)
        or not interval <= refresh <= 1440
    ):
        raise FleetManagerError(
            "Poll interval must be 1–60 minutes; "
            "full refresh must be between that interval and 1440"
        )
    for field, default, low, high in (
        ("detection_poll_interval_minutes", 1, 1, 60),
        ("detection_retention_days", 90, 7, 365),
        ("detection_poll_concurrency", 2, 2, 1000),
    ):
        value = config.get(field, default)
        if type(value) is not int or not low <= value <= high:
            raise FleetManagerError(f"{field} must be {low}–{high}")
    if type(config.get("detection_polling_enabled", True)) is not bool:
        raise FleetManagerError("detection_polling_enabled must be boolean")
    concurrency = config.get("poll_concurrency", 2)
    if (
        not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or not 2 <= concurrency <= 1000
    ):
        raise FleetManagerError("Poll concurrency must be 2–1000")
    hosts = config.get("allowed_agent_hosts", "")
    if not isinstance(hosts, str) or any(
        not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", host)
        for host in hosts.split(",")
        if host
    ):
        raise FleetManagerError("Allowed agent hosts must be comma-separated hostnames")
    if not isinstance(config.get("polling_enabled", True), bool):
        raise FleetManagerError("polling_enabled must be boolean")
    if config.get("api_url"):
        endpoint(config["api_url"], config["region"], config["stage"])
    bucket = config.get("recovered_detection_bucket")
    if bucket is not None and (
        not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
    ):
        raise FleetManagerError("Invalid recovered evidence bucket")


def _managers(registry):
    managers = registry.setdefault("fleet_managers", {})
    if not isinstance(managers, dict):
        raise FleetManagerError("Invalid fleet_managers registry")
    return managers


def selected(registry, name=None):
    managers = _managers(registry)
    if name is None and len(managers) == 1:
        name = next(iter(managers))
    if name not in managers:
        raise FleetManagerError("Select a saved manager with --fleet NAME; run 'fleet list'")
    config = {**managers[name], "name": name}
    _validate(config)
    return config


def _identity(config):
    return tuple(config.get(key) for key in ("security_account_id", "region", "stage"))


def _bind(registry, config):
    previous = _managers(registry).get(config["name"])
    if previous and _identity(previous) != _identity(config):
        raise FleetManagerError(
            "This fleet name belongs to another account/region/stage; use a new name"
        )


def _read_json(file):
    try:
        raw = sys.stdin.read(2_000_001) if file == "-" else Path(file).expanduser().read_text()
        if len(raw.encode()) > 2_000_000:
            raise FleetManagerError("JSON body exceeds 2 MB")
        return json.loads(raw)
    except (OSError, ValueError) as error:
        raise FleetManagerError("Cannot read JSON body file") from error


def _registration(file, expected_id=None):
    value = _read_json(file)
    if (
        not isinstance(value, dict)
        or set(value) - REGISTRATION_FIELDS
        or not {"agent_id", "name", "account_id", "region", "stage", "api_url"} <= set(value)
    ):
        raise FleetManagerError(
            "Use an agent registration document without credentials or metadata"
        )
    agent_id(value["agent_id"])
    if expected_id and value["agent_id"] != expected_id:
        raise FleetManagerError("Registration agent_id does not match the selected agent")
    return value


def _print(status, value):
    print(json.dumps(value, indent=2, default=str))
    return 0 if response_ok(status, value) else 1


def _source(config, runner, dry_run=False):
    version = config.get("source_version", DEFAULT_VERSION)
    url = config.get("repository_url", REPOSITORY)
    root = Path(config.get("source_root", SOURCE_ROOT))
    validate_version(version)
    validate_repository_url(url)
    expected = root.expanduser().resolve() / config["name"] / version
    metadata = expected.parent / f"{version}.source.json"
    if metadata.is_file():
        try:
            old = json.loads(metadata.read_text())
        except (OSError, ValueError) as error:
            raise FleetManagerError("Managed fleet source metadata is unreadable") from error
        if old.get("repository_url") != url:
            raise FleetManagerError(
                "Managed checkout belongs to another repository; use a new source root"
            )
    try:
        repo, source = install_core_source(
            name=config["name"],
            version=version,
            repository_url=url,
            root=root,
            runner=runner,
            dry_run=dry_run,
        )
    except SourceError as error:
        raise FleetManagerError(str(error).replace("Core", "Fleet Manager")) from error
    if (
        config.get("source_commit")
        and config.get("source_version") == version
        and source.get("commit") != config["source_commit"]
    ):
        raise FleetManagerError("Pinned fleet source commit changed; use a different version tag")
    if not dry_run and not all(
        (repo / name).is_file()
        for name in (
            "requirements.lock",
            "package-lock.json",
            "scripts/package.py",
            "scripts/bootstrap_agent.py",
        )
    ):
        raise FleetManagerError("Checkout is not a supported opencdr-fleet-manager source release")
    return repo, source


def _environment(config, repo):
    env = os.environ.copy()
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    ):
        env.pop(key, None)
    env.update(
        AWS_PROFILE=config["aws_profile"],
        AWS_DEFAULT_PROFILE=config["aws_profile"],
        AWS_REGION=config["region"],
        AWS_DEFAULT_REGION=config["region"],
        SECURITY_ACCOUNT_ID=config["security_account_id"],
        PYTHONPATH=str(repo / "src"),
    )
    return env


def _prepare(repo, config, runner, python=None, dry_run=False, node=True):
    python = python or shutil.which("python3.12") or sys.executable
    env = _environment(config, repo)
    executable = repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.exists():
        runner(
            [
                python,
                "-c",
                "import sys; assert sys.version_info >= (3,12), 'Python 3.12+ required'",
            ],
            cwd=repo,
            dry_run=dry_run,
        )
        runner([python, "-m", "venv", str(repo / ".venv")], cwd=repo, dry_run=dry_run)
    marker = repo / ".venv" / "opencdr-fleet-lock.sha256"
    digest = (
        "" if dry_run else hashlib.sha256((repo / "requirements.lock").read_bytes()).hexdigest()
    )
    if dry_run or not marker.is_file() or marker.read_text() != digest:
        runner(
            [
                str(executable),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--require-hashes",
                "-r",
                str(repo / "requirements.lock"),
            ],
            cwd=repo,
            env=env,
            dry_run=dry_run,
        )
        if not dry_run:
            marker.write_text(digest)
    if node and not (repo / "node_modules/.bin/serverless").is_file():
        runner(["npm", "ci"], cwd=repo, env=env, dry_run=dry_run)
    return executable


def _parameters(config):
    result = [
        f"--param=securityProfile={config['security_profile']}",
        f"--param=snapshotRetentionHours={config['snapshot_retention_hours']}",
        f"--param=pollingEnabled={str(config['polling_enabled']).lower()}",
        f"--param=pollIntervalMinutes={config.get('poll_interval_minutes', 1)}",
        f"--param=fullRefreshMinutes={config.get('full_refresh_minutes', 60)}",
        f"--param=detectionPollIntervalMinutes={config.get('detection_poll_interval_minutes', 1)}",
        f"--param=detectionRetentionDays={config.get('detection_retention_days', 90)}",
        f"--param=detectionPollConcurrency={config.get('detection_poll_concurrency', 2)}",
        "--param=detectionPollingEnabled="
        + str(config.get("detection_polling_enabled", True)).lower(),
    ]
    result.append("--param=pollConcurrency=" + str(config.get("poll_concurrency", 2)))
    if config["allowed_agent_hosts"]:
        result.append(f"--param=allowedAgentHosts={config['allowed_agent_hosts']}")
    if config.get("recovered_detection_bucket"):
        result.append("--param=detectionEvidenceBucketName=" + config["recovered_detection_bucket"])
    return result


def _digest(path):
    digest = hashlib.sha256()
    for file in sorted(path.rglob("*")):
        if file.is_symlink():
            raise FleetManagerError("Artifact must not contain symbolic links")
        if file.is_file() and file != path / "fleet-manifest.json":
            digest.update(file.relative_to(path).as_posix().encode() + b"\0")
            digest.update(file.read_bytes())
    return digest.hexdigest()


def _package(config, repo, python, source, args, runner):
    output = (
        (
            args.output
            or ARTIFACT_ROOT / config["name"] / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        )
        .expanduser()
        .resolve()
    )
    if output == repo or output in repo.parents or repo in output.parents:
        raise FleetManagerError("Use an artifact directory outside the managed source checkout")
    if output.exists() and any(output.iterdir()):
        raise FleetManagerError("Artifact directory must be empty")
    env = _environment(config, repo)
    runner([str(python), str(repo / "scripts/package.py")], cwd=repo, env=env, dry_run=args.dry_run)
    runner(
        [
            str(repo / "node_modules/.bin/serverless"),
            "package",
            "--stage",
            config["stage"],
            "--region",
            config["region"],
            "--package",
            str(output),
            *_parameters(config),
        ],
        cwd=repo,
        env=env,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        output.mkdir(parents=True, exist_ok=True)
        # Preserve the reviewed ZIP alongside the template, independently of later source builds.
        shutil.copy2(repo / "build/fleet-manager.zip", output / "fleet-manager.zip")
        state_path = output / "serverless-state.json"
        state = json.loads(state_path.read_text())
        state["service"]["initialServerlessConfig"]["package"]["artifact"] = str(
            output / "fleet-manager.zip"
        )
        state_path.write_text(json.dumps(state, indent=2) + "\n")
        manifest = {
            "schema": 1,
            "product": "OpenCDR Fleet Manager",
            "target": config,
            "source_commit": source["commit"],
            "sha256": _digest(output),
        }
        (output / "fleet-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        for file in output.rglob("*"):
            file.chmod(0o700 if file.is_dir() else 0o600)
        output.chmod(0o700)
        if config.get("recovered_detection_bucket") or config.get("recovery_storage"):
            _check_artifact(output, config)
    return output


def _check_artifact(artifact, config):
    try:
        manifest = json.loads((artifact / "fleet-manifest.json").read_text())
    except (OSError, ValueError) as error:
        raise FleetManagerError("Artifact is missing a valid fleet-manifest.json") from error
    target = manifest.get("target", {})
    keys = (
        "security_account_id",
        "region",
        "stage",
        "security_profile",
        "polling_enabled",
        "snapshot_retention_hours",
        "allowed_agent_hosts",
        "poll_concurrency",
        "poll_interval_minutes",
        "full_refresh_minutes",
        "detection_poll_interval_minutes",
        "detection_retention_days",
        "detection_poll_concurrency",
        "detection_polling_enabled",
        "recovered_detection_bucket",
        "recovery_storage",
        "source_version",
        "repository_url",
    )
    if (
        manifest.get("schema") != 1
        or manifest.get("product") != "OpenCDR Fleet Manager"
        or any(target.get(key) != config.get(key) for key in keys)
        or manifest.get("sha256") != _digest(artifact)
    ):
        raise FleetManagerError(
            "Artifact content or deployment identity/options changed; rebuild it"
        )
    if not (artifact / "fleet-manager.zip").is_file():
        raise FleetManagerError("Artifact is missing its reviewed Lambda ZIP")
    state = json.loads((artifact / "serverless-state.json").read_text())
    source_artifact = state["service"]["initialServerlessConfig"]["package"]["artifact"]
    if Path(source_artifact).resolve() != artifact / "fleet-manager.zip":
        raise FleetManagerError("Artifact moved or references an outside ZIP; build a new package")
    if config.get("recovered_detection_bucket") or config.get("recovery_storage"):
        template = json.loads((artifact / "cloudformation-template-update-stack.json").read_text())
        if (
            config.get("recovered_detection_bucket")
            and pinned_bucket_name(template) != config["recovered_detection_bucket"]
        ):
            raise FleetManagerError("Runtime package would disconnect the retained evidence bucket")
        if config.get("recovery_storage"):
            verify_recovery_template(template, config)
    return manifest


def _deploy(args, registry, save, runner):
    previous = _managers(registry).get(args.name, {})
    config = {
        "name": args.name,
        "aws_profile": args.profile or previous.get("aws_profile"),
        "security_account_id": args.security_account_id or previous.get("security_account_id"),
        "region": args.region or previous.get("region", "us-east-1"),
        "stage": args.stage or previous.get("stage", "prod"),
        "access": args.access or previous.get("access", "observer"),
        **{
            key: previous[key]
            for key in ("recovered_detection_bucket", "recovery_storage", "last_recovery")
            if key in previous
        },
        "poll_interval_minutes": args.poll_interval_minutes
        if args.poll_interval_minutes is not None
        else previous.get("poll_interval_minutes", 1),
        "full_refresh_minutes": args.full_refresh_minutes
        if args.full_refresh_minutes is not None
        else previous.get("full_refresh_minutes", 60),
        "poll_concurrency": args.poll_concurrency
        if args.poll_concurrency is not None
        else previous.get("poll_concurrency", 2),
        **{
            key: getattr(args, key, None)
            if getattr(args, key, None) is not None
            else previous.get(key, default)
            for key, default in (
                ("detection_poll_interval_minutes", 1),
                ("detection_retention_days", 90),
                ("detection_poll_concurrency", 2),
                ("detection_polling_enabled", True),
            )
        },
        "source_version": args.fleet_version or previous.get("source_version", DEFAULT_VERSION),
        "repository_url": args.repository_url or previous.get("repository_url", REPOSITORY),
        "source_root": str(args.source_root or previous.get("source_root", SOURCE_ROOT)),
        "security_profile": args.security_profile or previous.get("security_profile", "standard"),
        "snapshot_retention_hours": (
            args.snapshot_retention_hours
            if args.snapshot_retention_hours is not None
            else previous.get("snapshot_retention_hours", 24)
        ),
        "allowed_agent_hosts": (
            args.allowed_agent_hosts
            if args.allowed_agent_hosts is not None
            else previous.get("allowed_agent_hosts", "")
        ),
        "polling_enabled": (
            args.polling_enabled
            if args.polling_enabled is not None
            else previous.get("polling_enabled", True)
        ),
    }
    _validate(config)
    _bind(registry, config)
    if args.artifact and (args.output or args.package_only or args.fleet_command == "package"):
        raise FleetManagerError("--artifact is for deploying an existing package only")
    if previous.get("source_version") == config["source_version"] and previous.get("source_commit"):
        config["source_commit"] = previous["source_commit"]
    session = None if args.dry_run else aws_session(config)
    if session:
        verify_account(session, config["security_account_id"])
        if (
            not (args.package_only or args.fleet_command == "package")
            and not existing_stack(session, config["stage"])
            and retained_tables(session, config["stage"])
        ):
            raise FleetManagerError(
                "Retained tables found without a stack. Review: opencdr-deploy fleet recover "
                + args.name
                + " --profile "
                + config["aws_profile"]
                + " --security-account-id "
                + config["security_account_id"]
                + " --region "
                + config["region"]
                + " --stage "
                + config["stage"]
            )
    if args.artifact:
        artifact = args.artifact.expanduser().resolve()
        if not args.dry_run:
            manifest = _check_artifact(artifact, config)
            config["source_commit"] = manifest["source_commit"]
    repo, source = _source(config, runner, args.dry_run)
    if (
        config.get("recovered_detection_bucket")
        and not args.dry_run
        and "detectionEvidenceBucketName:" not in (repo / "serverless.yml").read_text()
    ):
        raise FleetManagerError(
            "Selected Fleet source cannot preserve the recovered evidence bucket; "
            "use a compatible version"
        )
    python = _prepare(repo, config, runner, args.python, args.dry_run)
    if not args.artifact:
        artifact = _package(config, repo, python, source, args, runner)
    config["source_commit"] = source["commit"]
    if not args.dry_run:
        config["last_artifact"] = str(artifact)
        _managers(registry)[args.name] = {**previous, **config}
        save(registry)
    if args.package_only or args.fleet_command == "package":
        print(f"Package ready: {artifact}. No AWS stack was created or updated.")
        print(
            f"Deploy this exact package: opencdr-deploy fleet deploy {args.name} "
            f"--artifact {artifact}"
        )
        return 0
    if not args.dry_run:
        _check_artifact(artifact, config)
        from .ownership_contract import guard_legacy_deployment

        guard_legacy_deployment(session, f"opencdr-fleet-manager-{config['stage']}")
        stack = aws_client(session, "cloudformation")
        try:
            description = stack.describe_stacks(
                StackName=f"opencdr-fleet-manager-{config['stage']}"
            )
            parameters = {
                p["ParameterKey"]: p.get("ParameterValue")
                for p in description["Stacks"][0].get("Parameters", [])
            }
            # Serverless resolves these into the template; inspect the deployed table property too.
            template = stack.get_template(StackName=f"opencdr-fleet-manager-{config['stage']}")[
                "TemplateBody"
            ]
            if isinstance(template, str):
                template = json.loads(template)
            if template.get("Description") == IMPORT_DESCRIPTION and not config.get(
                "recovery_storage"
            ):
                raise FleetManagerError(
                    "Resume the saved recovery plan to restore local storage guards "
                    "before deploying runtime"
                )
            live_bucket = pinned_bucket_name(template, parameters)
            if live_bucket:
                candidate = json.loads(
                    (artifact / "cloudformation-template-update-stack.json").read_text()
                )
                if pinned_bucket_name(candidate) != live_bucket:
                    raise FleetManagerError(
                        "Runtime package would disconnect the retained evidence bucket; "
                        "restore the recovery registry and rebuild"
                    )
            hardened = any(
                resource.get("Properties", {})
                .get("PointInTimeRecoverySpecification", {})
                .get("PointInTimeRecoveryEnabled")
                is True
                for logical, resource in template.get("Resources", {}).items()
                if logical in {"RegistryTable", "StatusTable", "ConfigurationTable"}
            )
            hardened |= parameters.get("securityProfile") == "hardened"
            condition = template.get("Conditions", {}).get("Hardened", {}).get("Fn::Equals", [])
            hardened |= condition == ["hardened", "hardened"]
            if (
                hardened
                and config["security_profile"] == "standard"
                and not args.allow_protection_downgrade
            ):
                raise FleetManagerError(
                    "Hardened stack detected; use --allow-protection-downgrade explicitly"
                )
        except ClientError as error:
            if error.response["Error"].get(
                "Code"
            ) != "ValidationError" or "does not exist" not in str(error):
                raise
    runner(
        [
            str(repo / "node_modules/.bin/serverless"),
            "deploy",
            "--conceal",
            "--stage",
            config["stage"],
            "--region",
            config["region"],
            "--package",
            str(artifact),
            *_parameters(config),
        ],
        cwd=repo,
        env=_environment(config, repo),
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        response = aws_client(session, "cloudformation").describe_stacks(
            StackName=f"opencdr-fleet-manager-{config['stage']}"
        )["Stacks"][0]
        outputs = {row["OutputKey"]: row["OutputValue"] for row in response.get("Outputs", [])}
        if outputs.get("FleetManagerApiUrl"):
            config["api_url"] = endpoint(
                outputs["FleetManagerApiUrl"], config["region"], config["stage"]
            )
        config["deployed_security_profile"] = config["security_profile"]
        if outputs.get("DetectionEvidenceBucket"):
            config["observed_detection_evidence_bucket"] = outputs["DetectionEvidenceBucket"]
        config["observed_stack_id"] = response.get("StackId")
        # This guard covered the first runtime update after import. Preserve the
        # bucket pin and audit summary; subsequent explicitly requested settings
        # changes use normal reviewed deployment behavior.
        if config.get("recovery_storage"):
            config.pop("recovery_storage")
            previous = {k: v for k, v in previous.items() if k != "recovery_storage"}
        _managers(registry)[args.name] = {**previous, **config}
        save(registry)
        print(
            json.dumps(
                {
                    "fleet": args.name,
                    "stack_status": response["StackStatus"],
                    "api_url": config.get("api_url"),
                },
                indent=2,
            )
        )
    return 0


def _bootstrap(args, config, runner):
    if args.file == "-":
        raise FleetManagerError("Credential bootstrap requires a reviewed JSON file, not stdin")
    agent = _registration(args.file)
    if agent.get("auth_mode", "iam") == "iam":
        security = aws_session(config)
        core = aws_session({"aws_profile": args.core_profile, "region": agent["region"]})
        verify_account(security, config["security_account_id"])
        verify_account(core, agent["account_id"])
        stack = aws_client(security, "cloudformation").describe_stacks(
            StackName=f"opencdr-fleet-manager-{config['stage']}"
        )["Stacks"][0]
        outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
        required = [
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
        if agent.get("allow_triage"):
            required.extend(["ManagementTriageRoleArn", "ApiRoleArn"])
        if any(key not in outputs for key in required):
            raise FleetManagerError("Upgrade Fleet Manager before IAM onboarding")
        print(
            json.dumps(
                {
                    "auth_mode": "iam",
                    "principal_arns": [outputs[key] for key in required],
                    "next_step": (
                        "Apply these exact Core trust principals, "
                        "then register without --core-profile"
                    ),
                    "credentials_created": False,
                },
                indent=2,
            )
        )
        return False
    repo, _ = _source(config, runner)
    python = _prepare(repo, config, runner, node=False)
    runner(
        [
            str(python),
            str(repo / "scripts/bootstrap_agent.py"),
            "--security-profile",
            config["aws_profile"],
            "--security-account-id",
            config["security_account_id"],
            "--region",
            config["region"],
            "--fleet-stage",
            config["stage"],
            "--core-profile",
            args.core_profile,
            "--file",
            str(Path(args.file).expanduser().resolve()),
        ],
        cwd=repo,
        env=_environment(config, repo),
    )
    return True


def execute(args, registry, registry_path, *, runner, save_registry):
    def save(value):
        save_registry(registry_path, value)

    try:
        if args.fleet_command == "list":
            print(json.dumps(_managers(registry), indent=2))
            return 0
        if args.fleet_command == "worker-identity":
            config = selected(registry, args.name)
            session = aws_session(config)
            verify_account(session, config["security_account_id"])
            stack = aws_client(session, "cloudformation").describe_stacks(
                StackName="opencdr-fleet-manager-" + config["stage"]
            )["Stacks"][0]
            outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
            arn = outputs.get("DetectionPollRoleArn", "")
            match = re.fullmatch(r"arn:(aws(?:-us-gov|-cn)?):iam::([0-9]{12}):role/(.+)", arn)
            if not match or match[2] != config["security_account_id"]:
                raise FleetManagerError("Stack has no verified detection worker role output")
            role = aws_client(session, "iam").get_role(RoleName=match[3].rsplit("/", 1)[-1])["Role"]
            if role.get("Arn") != arn or not re.fullmatch(
                r"AROA[A-Z0-9]{12,64}", role.get("RoleId", "")
            ):
                raise FleetManagerError("Detection worker IAM identity does not match the stack")
            print(
                json.dumps(
                    {
                        "fleet": args.name,
                        "role_arn": arn,
                        "principal": ":".join((match[1], match[2], role["RoleId"])),
                    },
                    indent=2,
                )
            )
            return 0
        if args.fleet_command == "recover":
            if args.execute and (not args.plan or not args.expected_plan_digest):
                raise FleetManagerError(
                    "Recovery execution requires --plan and --expected-plan-digest from review"
                )
            if not args.execute and (args.plan or args.expected_plan_digest):
                raise FleetManagerError(
                    "Use --execute with the saved plan, or omit --plan to create a new plan"
                )
            if args.execute and (args.out or args.evidence_bucket):
                raise FleetManagerError(
                    "Execution uses the saved plan's evidence bucket; "
                    "omit --out and --evidence-bucket"
                )
            previous = _managers(registry).get(args.name, {})
            config = {
                **previous,
                "name": args.name,
                "aws_profile": args.profile or previous.get("aws_profile"),
                "security_account_id": args.security_account_id
                or previous.get("security_account_id"),
                "region": args.region or previous.get("region", "us-east-1"),
                "stage": args.stage or previous.get("stage", "prod"),
            }
            _validate(config)
            _bind(registry, config)
            session = aws_session(config)
            verify_account(session, config["security_account_id"])
            if not args.execute:
                plan = recovery_plan(
                    session,
                    config["stage"],
                    account_id=config["security_account_id"],
                    region=config["region"],
                    evidence_bucket=args.evidence_bucket
                    or previous.get("observed_detection_evidence_bucket"),
                )
                if args.out:
                    # Exclusive creation avoids overwriting the artifact an operator reviewed.
                    with args.out.open("x", encoding="utf-8") as file:
                        args.out.chmod(0o600)
                        file.write(json.dumps(plan, indent=2) + "\n")
                print(json.dumps(plan, indent=2))
                print(
                    "Review the saved plan, then use --execute --plan FILE "
                    "--expected-plan-digest DIGEST. No resources were changed."
                )
                return 0
            plan = json.loads(args.plan.read_text())
            validate_plan(plan, args.expected_plan_digest)
            if plan["target"] != {
                "account_id": config["security_account_id"],
                "region": config["region"],
                "stage": config["stage"],
                "stack_name": "opencdr-fleet-manager-" + config["stage"],
            }:
                raise FleetManagerError("Saved recovery plan belongs to a different target")
            result = apply_recovery(session, plan, expected_digest=args.expected_plan_digest)
            if result["status"] != "IMPORT_COMPLETE":
                print(json.dumps(result, indent=2))
                print(
                    "Recovery is pending. Resume the same saved plan and digest; "
                    "do not recreate or delete storage."
                )
                return 0
            if any(
                resource["Properties"]
                .get("PointInTimeRecoverySpecification", {})
                .get("PointInTimeRecoveryEnabled")
                for logical, resource in plan["template"]["Resources"].items()
                if logical in {"RegistryTable", "StatusTable", "ConfigurationTable"}
            ):
                config["security_profile"] = "hardened"
            config.update(
                polling_enabled=False,
                detection_polling_enabled=False,
                recovery_storage=plan["template"]["Resources"],
                last_recovery=result,
            )
            config.pop("api_url", None)
            config.pop("last_artifact", None)
            bucket = plan["physical_identities"].get("DetectionBucket", {}).get("bucket_name")
            if bucket:
                config.update(
                    recovered_detection_bucket=bucket,
                    observed_detection_evidence_bucket=bucket,
                    detection_retention_days=plan["detection_retention_days"],
                )
            _managers(registry)[args.name] = config
            save(registry)
            print(json.dumps(result, indent=2))
            print(
                "Storage reattached; polling stays paused. Build a compatible reviewed runtime "
                "package with: opencdr-deploy fleet package " + args.name
            )
            return 0
        if args.fleet_command in {"deploy", "package"}:
            return _deploy(args, registry, save, runner)
        if args.fleet_command == "connect":
            config = {
                **_managers(registry).get(args.name, {}),
                "name": args.name,
                "aws_profile": args.profile,
                "security_account_id": args.security_account_id,
                "region": args.region,
                "stage": args.stage,
                "api_url": args.url,
                "access": args.access,
            }
            _validate(config)
            _bind(registry, config)
            api = FleetAPI(config)
            api.verify()
            _managers(registry)[args.name] = config
            save(registry)
            print(
                f"Connected {args.name}. AWS credentials stay in the AWS profile; "
                "no Core keys saved."
            )
            return 0
        config = selected(registry, args.fleet)
        if not config.get("api_url"):
            raise FleetManagerError(
                "No live endpoint saved. Deploy without --package-only, or use 'fleet connect'"
            )
        api = FleetAPI(config)
        status = api.verify()
        if args.fleet_command == "status":
            return _print(200, status)
        if args.fleet_command == "request":
            body = _read_json(args.file) if args.file else None
            return _print(*api.request(args.method, relative_path(args.path), body))
        action = args.agent_command
        if action == "list":
            return _print(200, api.agents())
        if action in {"register", "bootstrap"}:
            body = _registration(args.file)
            if args.core_profile:
                ready = _bootstrap(args, config, runner)
                if ready is False and action != "bootstrap":
                    raise FleetManagerError(
                        "IAM trust plan prepared; apply it before registering the agent"
                    )
            if action == "bootstrap":
                return 0
            return _print(*api.request("POST", "/v1/agents", body))
        path = f"/v1/agents/{agent_id(args.agent)}"
        if action == "show":
            return _print(*api.request("GET", path))
        if action == "refresh":
            return _print(*api.request("POST", path + "/refresh"))
        if action == "configuration":
            query = {
                "limit": args.limit,
                **({"kind": args.kind} if args.kind else {}),
                **({"next_token": args.next_token} if args.next_token else {}),
            }
            return _print(*api.request("GET", path + "/configuration?" + urlencode(query)))
        if action == "request":
            body = _read_json(args.file) if args.file else None
            return _print(
                *api.request(args.method, path + "/api" + relative_path(args.path, "/"), body)
            )
        if action == "credentials":
            verify_account(api.session, config["security_account_id"])
            value = getpass.getpass("Scoped Core API key (input hidden): ")
            if not value or any(ord(char) < 32 for char in value):
                raise FleetManagerError("Invalid API key")
            name = f"/opencdr-fleet-manager/{config['stage']}/agents/{args.agent}/{args.kind}-key"
            aws_client(api.session, "ssm").put_parameter(
                Name=name, Value=value, Type="SecureString", Overwrite=True
            )
            print(f"Stored {name}; the secret was not saved locally or displayed.")
            return 0
        if action == "update":
            body = _registration(args.file, args.agent)
        else:
            code, view = api.request("GET", path)
            if code != 200 or not isinstance(view, dict) or not isinstance(view.get("agent"), dict):
                return _print(code, view)
            if view["agent"].get("rev") != args.expected_rev:
                raise FleetManagerError(
                    "Agent revision changed; read the registration and review again"
                )
            body = {
                key: value for key, value in view["agent"].items() if key in REGISTRATION_FIELDS
            }
            body["enabled"] = action == "enable"
        body["expected_rev"] = args.expected_rev
        return _print(*api.request("PUT", path, body))
    except OSError as error:
        raise FleetManagerError(
            "Required tool or file is unavailable; check Git, Node/npm, Python and file permissions"
        ) from error
    except (BotoCoreError, ClientError) as error:
        # AWS exception payloads can contain request details. Do not leak key material.
        raise FleetManagerError(
            "AWS rejected the operation; check SSO login, profile and permissions"
        ) from error
