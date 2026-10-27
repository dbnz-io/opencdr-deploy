from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from opencdr_deploy.cli import run
from opencdr_deploy.sources import (
    SourceError,
    install_core_source,
    installed_versions,
    validate_repository_url,
    validate_version,
)


def git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True)


def core_repository(tmp_path: Path, tag: str = "v1.2.1") -> Path:
    repository = tmp_path / "upstream"
    repository.mkdir()
    git("init", cwd=repository)
    git("config", "user.email", "tests@example.com", cwd=repository)
    git("config", "user.name", "OpenCDR tests", cwd=repository)
    git("config", "commit.gpgsign", "false", cwd=repository)
    (repository / "scripts").mkdir()
    (repository / "serverless.yml").write_text("service: opencdr\n")
    (repository / "scripts" / "qa.py").write_text("print('ok')\n")
    git("add", ".", cwd=repository)
    git("commit", "-m", "fixture", cwd=repository)
    git("tag", tag, cwd=repository)
    return repository


def test_managed_source_clones_exact_tag_and_retains_metadata(tmp_path):
    upstream = core_repository(tmp_path)
    root = tmp_path / "managed"

    checkout, metadata = install_core_source(
        name="production",
        version="v1.2.1",
        repository_url=upstream.as_uri(),
        root=root,
        runner=run,
    )

    assert checkout == root / "production" / "v1.2.1"
    assert (checkout / "serverless.yml").is_file()
    assert metadata["version"] == "v1.2.1"
    assert len(metadata["commit"]) == 40
    stored = json.loads((checkout.parent / "v1.2.1.source.json").read_text())
    assert stored["commit"] == metadata["commit"]
    assert (checkout.parent / "v1.2.1.source.json").stat().st_mode & 0o777 == 0o600
    assert installed_versions(root, "production")[0]["version"] == "v1.2.1"

    cached, cached_metadata = install_core_source(
        name="production",
        version="v1.2.1",
        repository_url=upstream.as_uri(),
        root=root,
        runner=run,
    )
    assert cached == checkout
    assert cached_metadata["commit"] == metadata["commit"]


@pytest.mark.parametrize("version", ["main", "1.2.1", "v1", "v1.2.1/other"])
def test_managed_source_requires_version_tags(version):
    with pytest.raises(SourceError, match="immutable tags"):
        validate_version(version)


@pytest.mark.parametrize(
    "url",
    ["http://github.com/dbnz-io/opencdr.git", "https://token@github.com/repo.git", "-x"],
)
def test_managed_source_rejects_unsafe_repository_urls(url):
    with pytest.raises(SourceError):
        validate_repository_url(url)
