"""XrayPatternService._git_commit commits locally and never pushes.

The pattern store's commit uses the canonical cidx-meta-backup identity and
stays local: the catalog refresh the store requests runs the backup sync
under the exclusive backup lock (covered end to end in
tests/unit/global_repos/test_catalog_backup_sync_under_publish_lock.py), so
the backup mirror has one writer.

Real git repositories and a real bare remote; the real ConfigService is
injected through set_config_service/reset_config_service.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator, Tuple

import pytest

MINIMAL_EVALUATOR = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"

MINIMAL_PATTERN_YAML = f"""\
name: my-pattern
description: "Test pattern"
language: java
evaluator_code: |
  {MINIMAL_EVALUATOR}
"""
STORED = "xray-patterns/__any__/my-pattern.yaml"


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _make_cidx_meta_git_repo(tmp_path: Path) -> Path:
    """A real git-initialized cidx-meta directory with one commit."""
    cidx_meta = tmp_path / "data" / "golden-repos" / "cidx-meta"
    cidx_meta.mkdir(parents=True, exist_ok=True)
    _git(["init"], cidx_meta)
    _git(["config", "user.email", "test@test.invalid"], cidx_meta)
    _git(["config", "user.name", "Test User"], cidx_meta)
    (cidx_meta / "README.md").write_text("cidx-meta\n")
    _git(["add", "README.md"], cidx_meta)
    _git(["commit", "-m", "initial"], cidx_meta)
    return cidx_meta


def _create_test_pattern_file(cidx_meta: Path) -> Path:
    pattern_dir = cidx_meta / "xray-patterns" / "__any__"
    pattern_dir.mkdir(parents=True, exist_ok=True)
    pattern_file = pattern_dir / "test-pattern.yaml"
    pattern_file.write_text(MINIMAL_PATTERN_YAML)
    return pattern_file


def _bootstrap_bare_remote_cidx_meta(tmp_path: Path) -> Tuple[Path, Path]:
    """A real bare remote and a cidx-meta bootstrapped to back up to it."""
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )

    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    cidx_meta = tmp_path / "cidx-meta"
    cidx_meta.mkdir()
    (cidx_meta / "README.md").write_text("seed\n")
    CidxMetaBackupBootstrap().bootstrap(str(cidx_meta), remote.as_uri())
    return cidx_meta, remote


@pytest.fixture(params=[True, False], ids=["backup-enabled", "backup-disabled"])
def backup_config(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[None]:
    """The real ConfigService with the backup enabled or disabled."""
    from code_indexer.server.services.config_service import (
        ConfigService,
        reset_config_service,
        set_config_service,
    )
    from code_indexer.server.utils.config_manager import CidxMetaBackupConfig

    service = ConfigService(server_dir_path=str(tmp_path / "cidx-server-dir"))
    service.get_config().cidx_meta_backup_config = CidxMetaBackupConfig(
        enabled=bool(request.param), remote_url=""
    )
    set_config_service(service)
    try:
        yield
    finally:
        reset_config_service()


def test_git_commit_uses_canonical_author(tmp_path: Path) -> None:
    from code_indexer.server.services.xray_pattern_service import XrayPatternService

    cidx_meta = _make_cidx_meta_git_repo(tmp_path)
    pattern_file = _create_test_pattern_file(cidx_meta)

    XrayPatternService(cidx_meta)._git_commit(files=[pattern_file], message="test")

    author = _git(["log", "-1", "--format=%an %ae"], cidx_meta)
    assert author == "cidx-meta-backup cidx-meta-backup@example.invalid"
    assert "test" in _git(["log", "--oneline"], cidx_meta)


def test_git_commit_stays_local(tmp_path: Path, backup_config: None) -> None:
    from code_indexer.server.services.xray_pattern_service import XrayPatternService

    cidx_meta, remote = _bootstrap_bare_remote_cidx_meta(tmp_path)
    remote_head = _git(["rev-parse", "HEAD"], remote)
    pattern_file = _create_test_pattern_file(cidx_meta)

    XrayPatternService(cidx_meta)._git_commit(files=[pattern_file], message="test")

    assert "xray-patterns/__any__/test-pattern.yaml" in _git(["ls-files"], cidx_meta)
    assert _git(["rev-parse", "HEAD"], remote) == remote_head, (
        "the pattern commit was pushed by the pattern store"
    )


def test_store_xray_pattern_commits_locally_and_pushes_nothing(
    tmp_path: Path, backup_config: None
) -> None:
    from code_indexer.server.services.xray_pattern_service import XrayPatternService

    cidx_meta, remote = _bootstrap_bare_remote_cidx_meta(tmp_path)
    remote_head = _git(["rev-parse", "HEAD"], remote)

    result = XrayPatternService(cidx_meta).store_xray_pattern_audited(
        actor="example-user",
        may_replace_global=True,
        scope="__any__",
        pattern_yaml=MINIMAL_PATTERN_YAML,
    )

    assert result == {"success": True, "path": STORED}, result
    assert STORED in _git(["ls-files"], cidx_meta).splitlines()
    assert (cidx_meta / STORED).read_text() == MINIMAL_PATTERN_YAML
    assert _git(["rev-parse", "HEAD"], remote) == remote_head, (
        "the pattern store ran a backup push of its own"
    )
