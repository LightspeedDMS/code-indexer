"""Bug #2022 Gap 4: branches of the fatal chunk-store self-heal that the
end-to-end refresh flows do not reach. Real harness (real stores, real
snapshot, real restore primitive); the typed error is constructed directly
because the child-process boundary is covered elsewhere."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import Mock

import pytest

from code_indexer.global_repos import refresh_failure_recovery as recovery
from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
)
from code_indexer.services.index_failure_exit_codes import (
    ChunkStoreFailureKind,
    FatalChunkStoreIndexError,
)
from tests.utils.fatal_chunk_store_fixtures import (
    corrupt_btree_pages,
    diverge_store,
    make_journal_path_a_directory,
)
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from tests.utils.refresh_fatal_store_harness import (
    ALIAS,
    REPO,
    Harness,
    build_harness,
    sha256_of,
)

CORRUPTION = FatalChunkStoreIndexError("boom", ChunkStoreFailureKind.CORRUPTION)
ONE_DAY_SECONDS = 24 * 3600


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


def _self_heal(harness: Harness, error: FatalChunkStoreIndexError) -> None:
    """Exactly the callbacks _execute_refresh passes."""
    scheduler = harness.scheduler
    recovery.self_heal_after_fatal_chunk_store_failure(
        metadata=harness.metadata,
        snapshot_manager=scheduler._snapshot_manager,
        alias_name=ALIAS,
        source_path=str(harness.source),
        current_target=str(harness.snapshot),
        error=error,
        verify_ownership=lambda: scheduler.raise_if_write_lock_ownership_lost(
            REPO, owner_name="refresh_scheduler"
        ),
    )


def _self_heal_under_lock(harness: Harness, error: FatalChunkStoreIndexError) -> None:
    with harness.scheduler._held_write_lock_for_publish(REPO) as acquired:
        assert acquired
        _self_heal(harness, error)


def _backoff_active(harness: Harness) -> bool:
    return recovery.active_backoff_until(harness.metadata, ALIAS) is not None


def test_reported_corruption_with_healthy_store_backs_off_without_strike(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    sha_before = sha256_of(harness.source_db)

    _self_heal_under_lock(harness, CORRUPTION)

    assert harness.strikes() == 0
    assert sha256_of(harness.source_db) == sha_before
    assert _backoff_active(harness)


def test_reported_corruption_with_unverifiable_store_restores_nothing(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    diverge_store(harness.source_db)
    sha_before = sha256_of(harness.source_db)
    make_journal_path_a_directory(harness.source_db)

    _self_heal_under_lock(harness, CORRUPTION)

    assert sha256_of(harness.source_db) == sha_before
    assert harness.strikes() == 0, "an unverifiable store is not a strike"
    assert _backoff_active(harness)


def test_lost_write_lock_restores_nothing(tmp_path: Path, metadata: Any) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    corrupt_btree_pages(harness.source_db)
    corrupt_sha = sha256_of(harness.source_db)

    with pytest.raises(AliasLockOwnershipLostError):
        _self_heal(harness, CORRUPTION)

    assert sha256_of(harness.source_db) == corrupt_sha
    assert harness.strikes() == 0


def test_failed_restore_strikes_and_backs_off(tmp_path: Path, metadata: Any) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    harness.scheduler._snapshot_manager = None  # no clone primitive available
    corrupt_btree_pages(harness.source_db)
    corrupt_sha = sha256_of(harness.source_db)

    _self_heal_under_lock(harness, CORRUPTION)

    assert harness.strikes() == 1
    assert sha256_of(harness.source_db) == corrupt_sha
    assert _backoff_active(harness)


def test_expired_backoff_is_not_active(
    tmp_path: Path, metadata: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    metadata.record_refresh_failure_backoff(ALIAS, "disk full")
    state = metadata.get_refresh_failure_backoff_state(ALIAS)
    assert state["consecutive_failure_count"] == 1
    assert state["last_failed_at"] <= time.time()
    assert _backoff_active(harness)

    # One failure backs off for minutes, not forever: viewed from a day
    # later the same persisted state is no longer active.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + ONE_DAY_SECONDS)
    assert not _backoff_active(harness)


def test_backoff_store_failures_are_logged_never_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = Mock()
    failing.record_refresh_failure_backoff.side_effect = OSError("store down")
    failing.resolve_refresh_failure_backoff.side_effect = OSError("store down")

    with caplog.at_level(logging.ERROR):
        recovery.record_failure_backoff(failing, ALIAS, "disk full")
        recovery.resolve_after_publish(failing, ALIAS, 0.0)

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("failed to persist refresh failure backoff" in m for m in messages)
    assert any("failed to resolve the refresh failure backoff" in m for m in messages)


def test_backoff_store_rejects_blank_alias_and_detail(metadata: Any) -> None:
    with pytest.raises(ValueError):
        metadata.record_refresh_failure_backoff("", "disk full")
    with pytest.raises(ValueError):
        metadata.record_refresh_failure_backoff(ALIAS, "")
    with pytest.raises(ValueError):
        metadata.get_refresh_failure_backoff_state("")
    with pytest.raises(ValueError):
        metadata.resolve_refresh_failure_backoff("", 0.0)
    with pytest.raises(ValueError):
        metadata.mark_refresh_trigger_pending("", 0.0)
    with pytest.raises(ValueError):
        metadata.lease_pending_refresh_trigger("", 0.0, 1.0)
    with pytest.raises(ValueError):
        metadata.lease_pending_refresh_trigger(ALIAS, 5.0, 5.0)  # not exclusive

    metadata.resolve_refresh_failure_backoff(ALIAS, 0.0)  # no state: a no-op
    assert metadata.get_refresh_failure_backoff_state(ALIAS) is None
    assert metadata.record_refresh_failure_backoff(ALIAS, "disk full") == 1
    assert metadata.record_refresh_failure_backoff(ALIAS, "disk full") == 2
