"""Versioned, retained OpenCDR Core source management."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_CORE_REPOSITORY = "https://github.com/dbnz-io/opencdr.git"
DEFAULT_SOURCE_ROOT = Path(
    os.environ.get("OPENCDR_CORE_ROOT", "~/.local/share/opencdr-deploy/core")
).expanduser()
VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
Runner = Callable[..., subprocess.CompletedProcess[str]]


class SourceError(RuntimeError):
    """A managed Core source cannot be installed or verified."""


def validate_version(version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise SourceError("Core versions must be immutable tags such as v1.2.1")
    return version


def validate_repository_url(url: str) -> str:
    if not url or url.startswith("-") or "\n" in url or "\r" in url:
        raise SourceError("Invalid Core repository URL")
    if re.fullmatch(r"git@[^:]+:[A-Za-z0-9_.\-/]+", url):
        return url
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "ssh", "file"}:
        raise SourceError("Core repository URL must use https, ssh, or file")
    if parsed.scheme == "https" and (parsed.username or parsed.password):
        raise SourceError("Core repository URLs must not embed credentials")
    if parsed.scheme != "file" and not parsed.hostname:
        raise SourceError("Core repository URL is missing a host")
    return url


def source_path(root: Path, name: str, version: str) -> Path:
    return root.expanduser().resolve() / name / version


def _verify_checkout(path: Path, version: str, runner: Runner) -> str:
    if not (path / ".git").exists() or not (path / "serverless.yml").is_file():
        raise SourceError(f"Managed Core checkout is incomplete: {path}")
    tags = runner(["git", "tag", "--points-at", "HEAD"], cwd=path, capture=True).stdout.split()
    if version not in tags:
        raise SourceError(f"Managed checkout {path} is not pinned to tag {version}")
    status = runner(["git", "status", "--porcelain"], cwd=path, capture=True).stdout
    if status:
        raise SourceError(f"Managed checkout has local changes: {path}")
    return runner(["git", "rev-parse", "HEAD"], cwd=path, capture=True).stdout.strip()


def install_core_source(
    *,
    name: str,
    version: str,
    repository_url: str,
    root: Path,
    runner: Runner,
    dry_run: bool = False,
) -> tuple[Path, dict[str, Any]]:
    version = validate_version(version)
    repository_url = validate_repository_url(repository_url)
    destination = source_path(root, name, version)
    metadata_path = destination.parent / f"{version}.source.json"

    if destination.exists():
        commit = _verify_checkout(destination, version, runner)
        return destination, {
            "type": "managed",
            "repository_url": repository_url,
            "version": version,
            "commit": commit,
        }

    if dry_run:
        runner(
            [
                "git",
                "clone",
                "--branch",
                version,
                "--depth",
                "1",
                "--recurse-submodules",
                "--shallow-submodules",
                repository_url,
                str(destination),
            ],
            cwd=destination.parent,
            dry_run=True,
        )
        return destination, {
            "type": "managed",
            "repository_url": repository_url,
            "version": version,
            "commit": None,
        }

    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    temporary = destination.parent / f".{version}.installing-{uuid.uuid4().hex}"
    try:
        runner(
            [
                "git",
                "clone",
                "--branch",
                version,
                "--depth",
                "1",
                "--recurse-submodules",
                "--shallow-submodules",
                repository_url,
                str(temporary),
            ],
            cwd=destination.parent,
        )
        commit = _verify_checkout(temporary, version, runner)
        temporary.replace(destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise

    metadata = {
        "type": "managed",
        "repository_url": repository_url,
        "version": version,
        "commit": commit,
        "installed_at": datetime.now(UTC).isoformat(),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    os.chmod(metadata_path, 0o600)
    return destination, metadata


def installed_versions(root: Path, name: str) -> list[dict[str, Any]]:
    parent = root.expanduser().resolve() / name
    if not parent.exists():
        return []
    result = []
    for path in sorted(parent.iterdir()):
        if not path.is_dir() or not VERSION_RE.fullmatch(path.name):
            continue
        metadata_path = parent / f"{path.name}.source.json"
        metadata = {}
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text())
            except json.JSONDecodeError:
                metadata = {"metadata_error": "invalid JSON"}
        result.append({"version": path.name, "path": str(path), **metadata})
    return result
