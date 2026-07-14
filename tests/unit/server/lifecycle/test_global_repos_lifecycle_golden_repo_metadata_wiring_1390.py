"""Bug #1390 wiring: GlobalReposLifecycleManager must hand a golden_repo_metadata
backend to its RefreshScheduler so filesystem reconciliation can update the
golden_repos_metadata table (bare-alias-keyed) alongside global_repos
(-global-alias-keyed).

This is the anti-orphan-code guard (Rule 12), mirroring
test_cleanup_manager_snapshot_wiring_bug1084.py's pattern for snapshot_manager:
RefreshScheduler.golden_repo_metadata_backend exists only to be wired here. If
this wiring regresses, RefreshScheduler silently falls back to a per-node
SQLite backend even in cluster/postgres mode, splitting golden_repos_metadata
from the shared PostgreSQL store.
"""

from unittest.mock import MagicMock

from code_indexer.server.lifecycle.global_repos_lifecycle import (
    GlobalReposLifecycleManager,
)


def test_lifecycle_wires_golden_repo_metadata_backend_into_refresh_scheduler(
    tmp_path,
):
    backend = MagicMock(name="GoldenRepoMetadataBackend")

    lifecycle = GlobalReposLifecycleManager(
        golden_repos_dir=str(tmp_path / "golden-repos"),
        golden_repo_metadata_backend=backend,
    )

    # RefreshScheduler must have received the backend for reconciliation.
    assert lifecycle.refresh_scheduler.golden_repo_metadata is backend


def test_lifecycle_without_golden_repo_metadata_backend_falls_back_lazily(
    tmp_path,
):
    """No golden_repo_metadata_backend passed (e.g. solo mode) -> RefreshScheduler
    must not eagerly fail; it lazily resolves its own SQLite fallback on first
    access instead (mirrors the existing `registry` property behavior)."""
    lifecycle = GlobalReposLifecycleManager(
        golden_repos_dir=str(tmp_path / "golden-repos"),
        golden_repo_metadata_backend=None,
    )

    from code_indexer.server.storage.sqlite_backends import (
        GoldenRepoMetadataSqliteBackend,
    )

    assert isinstance(
        lifecycle.refresh_scheduler.golden_repo_metadata,
        GoldenRepoMetadataSqliteBackend,
    )
