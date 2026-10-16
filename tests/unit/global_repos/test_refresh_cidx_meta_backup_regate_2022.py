"""Bug #2022 Gap 4, review round 4 item 4 (m34/m35) and P4-5: cidx-meta with
the backup mirror enabled takes its own "No changes detected" exits when the
backup sync reports nothing to push -- on the ``local://cidx-meta`` path and
on the legacy meta-directory path.

- While a failure is unresolved (a persisted backoff), both exits must
  re-gate and publish instead of leaving the alias on the old snapshot.
- A backup push that fails AFTER the new snapshot was published still
  resolves the backoff: the alias is healthy and published.

Real stores, registry, scheduler indexing step, snapshot and alias publish.
Stood in at their boundaries only: the ``cidx index`` child process (it
needs an embedding provider), the config service (a real ``ServerConfig``
with the backup enabled), the git backup sync (returns a real
``SyncResult``) and the description-file updater.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable, Iterator, List, Optional
from unittest.mock import patch

import pytest

from code_indexer.server.services.cidx_meta_backup.sync import SyncResult
from code_indexer.server.utils.config_manager import (
    CidxMetaBackupConfig,
    ServerConfig,
)
from tests.utils.fatal_chunk_store_fixtures import make_chunks_db_repo
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from tests.utils.refresh_fatal_store_harness import Harness, build_harness

if TYPE_CHECKING:
    from code_indexer.server.storage.protocols.golden_repo_metadata_backend import (
        GoldenRepoMetadataBackend,
    )

META_ALIAS = "cidx-meta-global"
_SCHEDULER = "code_indexer.global_repos.refresh_scheduler"
_CHILD_RUNNER = (
    "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
)
#: m34: the post-migration local path; m35: the legacy meta-directory path.
BACKUP_PATHS = {"local_backup": "local://cidx-meta", "meta_backup": None}
NOTHING_TO_PUSH = SyncResult(skipped=True, sync_failure=None)


@pytest.fixture(params=STORE_KINDS)
def metadata(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator["GoldenRepoMetadataBackend"]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


@pytest.fixture(params=sorted(BACKUP_PATHS))
def repo_url(request: pytest.FixtureRequest) -> Optional[str]:
    return BACKUP_PATHS[request.param]


def _meta_harness(
    tmp_path: Path, metadata: "GoldenRepoMetadataBackend", repo_url: Optional[str]
) -> Harness:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    meta_dir = tmp_path / "data" / "golden-repos" / "cidx-meta"
    make_chunks_db_repo(meta_dir, metadata_marker="meta")
    harness.registry.register_global_repo(
        "cidx-meta", META_ALIAS, repo_url, index_path=str(meta_dir), allow_reserved=True
    )
    harness.scheduler.alias_manager.create_alias(
        META_ALIAS, str(meta_dir), repo_name="cidx-meta"
    )
    return harness


@contextlib.contextmanager
def _backup_enabled(
    tmp_path: Path,
    sync_result: SyncResult,
    during_sync: Optional[Callable[[], None]] = None,
) -> Iterator[List[List[str]]]:
    """Yields the ``cidx`` commands the indexing step ran. *during_sync*
    runs while the backup sync is in progress (a concurrent event)."""
    config = ServerConfig(server_dir=str(tmp_path / "server"))
    config.cidx_meta_backup_config = CidxMetaBackupConfig(enabled=True, remote_url="")
    config_service = SimpleNamespace(
        get_config=lambda: config,
        sync_repo_extensions_if_drifted=lambda repo_path: None,  # in sync
    )
    child_commands: List[List[str]] = []

    def _index_child(command: List[str], **kwargs: object) -> int:
        child_commands.append(list(command))
        return 0

    def _sync() -> SyncResult:
        if during_sync is not None:
            during_sync()
        return sync_result

    def _backup_sync(
        repo_path: str, branch: str, cancel_check: object = None
    ) -> SimpleNamespace:
        return SimpleNamespace(sync=_sync)

    with (
        patch(f"{_SCHEDULER}.get_config_service", return_value=config_service),
        patch(f"{_SCHEDULER}.CidxMetaBackupSync", side_effect=_backup_sync),
        patch(f"{_SCHEDULER}.MetaDirectoryUpdater"),
        patch(_CHILD_RUNNER, side_effect=_index_child),
    ):
        yield child_commands


def _indexed(child_commands: List[List[str]]) -> bool:
    return any(command[:2] == ["cidx", "index"] for command in child_commands)


def test_unresolved_backoff_regates_through_a_skipped_backup_sync(
    tmp_path: Path, metadata: "GoldenRepoMetadataBackend", repo_url: Optional[str]
) -> None:
    harness = _meta_harness(tmp_path, metadata, repo_url)
    metadata.record_refresh_failure_backoff(META_ALIAS, "disk full")

    with _backup_enabled(tmp_path, NOTHING_TO_PUSH) as child_commands:
        result = harness.scheduler._execute_refresh(META_ALIAS)

    assert result.get("message") == "Refresh complete", result
    assert _indexed(child_commands), child_commands
    assert metadata.get_refresh_failure_backoff_state(META_ALIAS) is None


def test_healthy_alias_takes_the_skipped_backup_shortcut(
    tmp_path: Path, metadata: "GoldenRepoMetadataBackend", repo_url: Optional[str]
) -> None:
    harness = _meta_harness(tmp_path, metadata, repo_url)

    with _backup_enabled(tmp_path, NOTHING_TO_PUSH) as child_commands:
        result = harness.scheduler._execute_refresh(META_ALIAS)

    assert result.get("message") == "No changes detected", result
    assert not _indexed(child_commands), child_commands


def test_backup_failure_after_publish_still_resolves_the_backoff(
    tmp_path: Path, metadata: "GoldenRepoMetadataBackend", repo_url: Optional[str]
) -> None:
    harness = _meta_harness(tmp_path, metadata, repo_url)
    metadata.record_refresh_failure_backoff(META_ALIAS, "disk full")
    published_before = harness.scheduler.alias_manager.read_alias(META_ALIAS)
    push_rejected = SyncResult(skipped=False, sync_failure="push rejected")

    with _backup_enabled(tmp_path, push_rejected):
        with pytest.raises(Exception, match="push rejected"):
            harness.scheduler._execute_refresh(META_ALIAS)

    assert harness.scheduler.alias_manager.read_alias(META_ALIAS) != published_before
    assert metadata.get_refresh_failure_backoff_state(META_ALIAS) is None, (
        "a published alias kept its failure backoff"
    )


def test_trigger_deferred_during_the_backup_sync_survives_the_publish(
    tmp_path: Path, metadata: "GoldenRepoMetadataBackend", repo_url: Optional[str]
) -> None:
    from code_indexer.global_repos.refresh_failure_recovery import (
        RefreshDeferredError,
    )
    from tests.utils.refresh_fatal_store_harness import (
        RecordingJobManager,
        run_one_scheduler_iteration,
    )

    harness = _meta_harness(tmp_path, metadata, repo_url)
    jobs = RecordingJobManager()
    harness.scheduler.background_job_manager = jobs  # type: ignore[assignment]
    metadata.record_refresh_failure_backoff(META_ALIAS, "disk full")

    def _writer_request_arrives() -> None:
        with pytest.raises(RefreshDeferredError):
            harness.scheduler.trigger_refresh_for_repo(META_ALIAS)

    with _backup_enabled(tmp_path, NOTHING_TO_PUSH, _writer_request_arrives):
        result = harness.scheduler._execute_refresh(META_ALIAS)
    assert result.get("message") == "Refresh complete", result

    run_one_scheduler_iteration(harness)

    assert jobs.submitted == [META_ALIAS], "a write during the backup sync was lost"
