"""A failed tracked-file listing fails a golden-repo branch change before the
snapshot swap; the previously published snapshot stays published.

Real pieces: git (local origin, base clone, fetch/checkout/pull), the
standalone CoW snapshot copy, the alias pointer files. Stand-ins: the `cidx`
executable on PATH (process boundary; its `fix-config` leaves the snapshot's
git index unreadable), the server config service (scratch server dir) and
the registry DB backend.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any, List, Optional, cast
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
)
from code_indexer.server.services import config_service
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.server.utils.config_manager import ServerConfig

_GIT = ["git", "-c", "user.email=test@example.com", "-c", "user.name=test"]

_STAND_IN_CIDX = """#!/bin/sh
case "$1" in
  index) mkdir -p .code-indexer/tantivy_index .code-indexer/index ;;
  fix-config) printf 'not-a-git-index' > .git/index ;;
esac
exit 0
"""


class _ConfigService:
    def __init__(self, config: ServerConfig) -> None:
        self._config = config

    def get_config(self) -> ServerConfig:
        return self._config


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [*_GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _make_origin(origin: Path) -> None:
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "a.py").write_text("a = 1\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "main")
    _git(origin, "checkout", "-q", "-b", "feature-x")
    (origin / "b.py").write_text("b = 2\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "feature")
    _git(origin, "checkout", "-q", "main")


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GoldenRepoManager:
    server_dir = tmp_path / "server-dir"
    server_dir.mkdir()
    (server_dir / "config.json").write_text(
        json.dumps({"server_dir": str(server_dir), "storage_mode": "sqlite"})
    )
    monkeypatch.setattr(
        config_service,
        "get_config_service",
        lambda: _ConfigService(
            ServerConfig(server_dir=str(server_dir), storage_mode="sqlite")
        ),
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "cidx").write_text(_STAND_IN_CIDX)
    (bin_dir / "cidx").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    origin = tmp_path / "origin"
    _make_origin(origin)
    data_dir = tmp_path / "data"
    (data_dir / "golden-repos").mkdir(parents=True)
    clone_path = data_dir / "golden-repos" / "my-repo"
    _git(tmp_path, "clone", "-q", str(origin), str(clone_path))

    mgr = GoldenRepoManager(data_dir=str(data_dir))
    row = {
        "alias": "my-repo",
        "repo_url": str(origin),
        "default_branch": "main",
        "clone_path": str(clone_path),
        "created_at": "2025-01-01T00:00:00Z",
        "enable_temporal": False,
        "temporal_options": None,
        "category_id": None,
        "category_auto_assigned": False,
    }
    mgr.golden_repos["my-repo"] = GoldenRepo(
        alias="my-repo",
        repo_url=str(origin),
        default_branch="main",
        clone_path=str(clone_path),
        created_at="2025-01-01T00:00:00Z",
    )
    mgr._sqlite_backend = MagicMock()
    mgr._sqlite_backend.get_repo.return_value = row
    return mgr


def _publish_previous(manager: GoldenRepoManager, tmp_path: Path) -> AliasManager:
    previous = tmp_path / "previous-snapshot"
    previous.mkdir()
    aliases = AliasManager(os.path.join(manager.golden_repos_dir, "aliases"))
    aliases.create_alias("my-repo-global", str(previous), repo_name="my-repo")
    return aliases


class TestFailedListingFailsBranchChange:
    def test_failed_listing_raises_before_swap(
        self, manager: GoldenRepoManager, tmp_path: Path
    ) -> None:
        aliases = _publish_previous(manager, tmp_path)

        with pytest.raises(RuntimeError, match="git ls-files"):
            manager.change_branch("my-repo", "feature-x")

        assert aliases.read_alias("my-repo-global") == str(
            tmp_path / "previous-snapshot"
        )
        manager._sqlite_backend.update_default_branch.assert_not_called()
        assert manager.golden_repos["my-repo"].default_branch == "main"
        base = Path(manager.golden_repos["my-repo"].clone_path)
        assert _git(base, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"

    def test_failed_listing_discards_unpublished_snapshot(
        self, manager: GoldenRepoManager, tmp_path: Path
    ) -> None:
        _publish_previous(manager, tmp_path)
        versioned = Path(manager.golden_repos_dir) / ".versioned" / "my-repo"

        with pytest.raises(RuntimeError, match="git ls-files"):
            manager.change_branch("my-repo", "feature-x")

        assert versioned.is_dir(), "the branch change never created a snapshot"
        assert list(versioned.glob("v_*")) == []


def _snapshot_dir(manager: GoldenRepoManager, alias: str, name: str) -> Path:
    path = Path(manager.golden_repos_dir) / ".versioned" / alias / name
    path.mkdir(parents=True)
    (path / "marker").write_text("x")
    return path


class TestDiscardRefusesUnverifiedPaths:
    def test_base_clone_path_is_not_deleted(
        self, manager: GoldenRepoManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        base = Path(manager.golden_repos["my-repo"].clone_path)

        manager._cb_discard_unpublished_snapshot("my-repo", str(base))

        assert (base / ".git").is_dir()
        assert any(r.levelname == "ERROR" for r in caplog.records)

    def test_other_alias_snapshot_is_not_deleted(
        self, manager: GoldenRepoManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        other = _snapshot_dir(manager, "other-repo", "v_1700000000")

        manager._cb_discard_unpublished_snapshot("my-repo", str(other))

        assert (other / "marker").is_file()
        assert any(r.levelname == "ERROR" for r in caplog.records)

    def test_published_snapshot_is_not_deleted(
        self,
        manager: GoldenRepoManager,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        published = _snapshot_dir(manager, "my-repo", "v_1700000000")
        aliases = AliasManager(os.path.join(manager.golden_repos_dir, "aliases"))
        aliases.create_alias("my-repo-global", str(published), repo_name="my-repo")

        manager._cb_discard_unpublished_snapshot("my-repo", str(published))

        assert (published / "marker").is_file()
        assert any(r.levelname == "ERROR" for r in caplog.records)


class TestDiscardRequiresConfiguredRoot:
    def test_snapshot_outside_configured_root_is_not_deleted(
        self,
        manager: GoldenRepoManager,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        outside = tmp_path / "elsewhere" / ".versioned" / "my-repo" / "v_1700000000"
        outside.mkdir(parents=True)
        (outside / "marker").write_text("x")

        manager._cb_discard_unpublished_snapshot("my-repo", str(outside))

        assert (outside / "marker").is_file()
        assert any(r.levelname == "ERROR" for r in caplog.records)

    def test_symlinked_snapshot_escaping_root_is_not_deleted(
        self,
        manager: GoldenRepoManager,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        target = tmp_path / "elsewhere" / "data"
        target.mkdir(parents=True)
        (target / "marker").write_text("x")
        namespace = Path(manager.golden_repos_dir) / ".versioned" / "my-repo"
        namespace.mkdir(parents=True)
        link = namespace / "v_1700000001"
        link.symlink_to(target, target_is_directory=True)

        manager._cb_discard_unpublished_snapshot("my-repo", str(link))

        assert (target / "marker").is_file()
        assert any(r.levelname == "ERROR" for r in caplog.records)


class _UndeletableBackend:
    def __init__(self, versioned_base: str, error: Optional[Exception]) -> None:
        self._versioned_base = versioned_base
        self._error = error

    def delete_clone(self, path: str) -> bool:
        if self._error is not None:
            raise self._error
        return False


class _RecordingCleanup:
    def __init__(self) -> None:
        self.scheduled: List[str] = []

    def schedule_cleanup(self, index_path: str) -> None:
        self.scheduled.append(index_path)


class _SchedulerWithCleanup:
    def __init__(self) -> None:
        self.cleanup_manager = _RecordingCleanup()


class TestDiscardFallsBackToDurableCleanup:
    @pytest.mark.parametrize("error", [None, OSError("device busy")])
    def test_failed_delete_is_scheduled_for_cleanup(
        self, manager: GoldenRepoManager, error: Optional[Exception]
    ) -> None:
        snapshot = _snapshot_dir(manager, "my-repo", "v_1700000000")
        # ^ why cast, not a full CloneBackend; delete_snapshot calls only delete_clone
        backend = cast(Any, _UndeletableBackend(manager.golden_repos_dir, error))
        manager._snapshot_manager = VersionedSnapshotManager(clone_backend=backend)
        scheduler = _SchedulerWithCleanup()
        manager._refresh_scheduler = scheduler

        manager._cb_discard_unpublished_snapshot("my-repo", str(snapshot))

        assert scheduler.cleanup_manager.scheduled == [str(snapshot)]
