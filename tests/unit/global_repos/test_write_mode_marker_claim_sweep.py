"""Leftover marker claim files are swept once older than the marker TTL.

A cleanup claims a marker by renaming it to ``.<name>.cleanup-<hex>``; a crash
between the rename and the unlink leaves that file behind, outside the
``*.json`` sweep. Real RefreshScheduler, real files, no mocks of the sweep.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import (
    WRITE_MODE_MARKER_TTL_SECONDS,
    RefreshScheduler,
)
from tests.fixtures.refresh_scheduler_stores import real_metadata_store


def _scheduler(tmp_path: Path) -> RefreshScheduler:
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    registry = Mock()
    registry.list_global_repos.return_value = []
    return RefreshScheduler(
        golden_repos_dir=str(tmp_path / "golden-repos"),
        config_source=config,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=registry,
        golden_repo_metadata_backend=real_metadata_store(tmp_path / "server-data"),
    )


class TestLeftoverClaimSweep:
    def test_aged_claim_is_removed_and_fresh_claim_is_kept(
        self, tmp_path: Path
    ) -> None:
        scheduler = _scheduler(tmp_path)
        write_mode_dir = tmp_path / "golden-repos" / ".write_mode"
        write_mode_dir.mkdir(parents=True)
        aged = write_mode_dir / ".example-repo.json.cleanup-0a1b2c"
        fresh = write_mode_dir / ".example-repo.json.cleanup-3d4e5f"
        aged.write_text("{}")
        fresh.write_text("{}")
        old = time.time() - 2 * WRITE_MODE_MARKER_TTL_SECONDS
        os.utime(aged, (old, old))

        scheduler.cleanup_stale_write_mode_markers()

        assert not aged.exists()
        assert fresh.exists()


class TestClaimAgeCountsFromTheClaim:
    def test_sweep_during_a_claim_keeps_the_marker_to_restore(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from code_indexer.global_repos import write_mode_marker_cleanup as cleanup

        write_mode_dir = tmp_path / ".write_mode"
        write_mode_dir.mkdir()
        marker = write_mode_dir / "example-repo.json"
        marker.write_text('{"owner_token": "newer-session"}')
        old = time.time() - 2 * WRITE_MODE_MARKER_TTL_SECONDS
        os.utime(marker, (old, old))
        real_link = os.link

        def sweep_then_link(src: Any, dst: Any) -> None:
            # ^ why a sweep in another process lands between the claim and the restore
            cleanup.sweep_leftover_claims(write_mode_dir, WRITE_MODE_MARKER_TTL_SECONDS)
            real_link(src, dst)

        monkeypatch.setattr(cleanup.os, "link", sweep_then_link)

        removed = cleanup.remove_marker_if_unchanged(
            marker, '{"owner_token": "evaluated"}'
        )

        assert removed is False
        assert marker.read_text() == '{"owner_token": "newer-session"}'
