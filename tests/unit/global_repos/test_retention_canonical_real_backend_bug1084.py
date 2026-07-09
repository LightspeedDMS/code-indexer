"""Bug #1084 review fix: keep-last-N retention is correct on cow-daemon with the
REAL discovery API.

This is the GATE test for the review finding. The pre-existing retention test
(``test_refresh_scheduler_retention_bug1084.py``) mocked ``list_snapshots`` to
return CANONICAL-shaped paths the real cow-daemon backend never emitted — it
therefore could not catch the defect where ``_list_cow_daemon_snapshots`` built
LEGACY-shaped paths (``{mount}/{ns}/v_<ts>``) that never string-matched the
canonical alias ``target_path`` / ``previous_path``, allowing the rollback
(``previous``) snapshot to be scheduled for deletion at ``keep_last=1`` (AC10
violation).

Here everything is REAL except the daemon-HTTP boundary:
- REAL ``RefreshScheduler._enforce_retention`` (the production retention path)
- REAL ``CleanupManager`` (its ``get_pending_cleanups()`` is the assertion source)
- REAL ``VersionedSnapshotManager`` wrapping a REAL ``CowDaemonBackend``
- REAL ``AliasManager`` with on-disk alias JSON carrying canonical paths
- ONLY the ``requests`` module is stubbed (daemon ``GET /api/v1/clones`` payload)

``query_tracker`` and ``registry`` are stubbed: they are infra the retention
logic does not exercise (no ref-count gate is hit because we assert the SCHEDULED
set via ``get_pending_cleanups()``, which ``schedule_cleanup`` populates
directly, independent of the background cleanup loop).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.storage.shared.clone_backend import CowDaemonBackend
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.server.utils.config_manager import CowDaemonConfig

_MOUNT = "/mnt/cow-storage"
_NS = "my_repo"  # sanitized daemon namespace for repo "my-repo"


def _canonical(ts: int) -> str:
    """Canonical snapshot path the daemon + create_clone + discovery all agree on."""
    return f"{_MOUNT}/.versioned/{_NS}/v_{ts}"


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


def _real_cow_manager(list_clones_json):
    """REAL VersionedSnapshotManager over a REAL CowDaemonBackend.

    Returns ``(manager, mock_requests)`` — the ``mock_requests`` GET yields the
    daemon's list payload; everything else (sanitization, mapping to canonical
    path) is the real code under test.
    """
    backend = CowDaemonBackend(
        config=CowDaemonConfig(
            daemon_url="http://daemon:8081", api_key="k", mount_point=_MOUNT
        )
    )
    manager = VersionedSnapshotManager(clone_backend=backend)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = list_clones_json
    resp.raise_for_status = MagicMock()
    mock_req = MagicMock()
    mock_req.get.return_value = resp
    return manager, mock_req


def _make_scheduler(golden_repos_dir, snapshot_manager, cleanup_manager):
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=cleanup_manager,
        registry=MagicMock(),
        snapshot_manager=snapshot_manager,
    )


def _seed_canonical_alias(scheduler, alias_name, current_ts, previous_ts):
    """Write a real on-disk alias JSON with CANONICAL current + previous paths.

    Mirrors the create-then-swap pattern so previous_path is recorded exactly as
    the production swap path would record it.
    """
    scheduler.alias_manager.create_alias(
        alias_name, _canonical(previous_ts), repo_name="my-repo"
    )
    scheduler.alias_manager.swap_alias(
        alias_name=alias_name,
        new_target=_canonical(current_ts),
        old_target=_canonical(previous_ts),
    )


class TestRetentionCanonicalRealBackend:
    def test_keep_last_1_protects_previous_real_backend(self, golden_repos_dir):
        """3 canonical snapshots, keep_last=1, current=newest, previous=middle.

        The rollback ``previous`` snapshot MUST NOT be scheduled, ``current`` MUST
        survive, and only the OLDEST (third) snapshot is scheduled for deletion.
        Pre-fix, discovery emitted legacy paths that never matched the canonical
        ``previous_path`` -> ``previous`` would have been scheduled (AC10 break).
        """
        # Daemon knows 3 snapshots under the sanitized namespace.
        list_json = [
            {"namespace": _NS, "name": "v_100"},  # oldest -> should be scheduled
            {"namespace": _NS, "name": "v_200"},  # previous (rollback) -> protected
            {"namespace": _NS, "name": "v_300"},  # current (newest) -> protected
        ]
        manager, mock_req = _real_cow_manager(list_json)
        cleanup = CleanupManager(query_tracker=MagicMock(spec=QueryTracker))
        sched = _make_scheduler(golden_repos_dir, manager, cleanup)

        # Alias JSON: current = v_300 (canonical), previous = v_200 (canonical).
        _seed_canonical_alias(sched, "my-repo-global", current_ts=300, previous_ts=200)

        with patch.dict(sys.modules, {"requests": mock_req}):
            with patch(
                "code_indexer.global_repos.refresh_scheduler.get_config_service"
            ) as gcs:
                gcs.return_value.get_config.return_value.snapshot_retention_keep_last = 1
                sched._enforce_retention("my-repo-global", _canonical(300))

        scheduled = cleanup.get_pending_cleanups()

        # current (v_300) survives — it is the newest AND the alias target.
        assert _canonical(300) not in scheduled
        # previous (v_200) survives — force-protected rollback even though
        # keep_last=1 would otherwise drop it. THIS is the review finding.
        assert _canonical(200) not in scheduled
        # Only the oldest (v_100) is scheduled.
        assert scheduled == {_canonical(100)}

    def test_keep_last_1_current_survives_when_no_previous(self, golden_repos_dir):
        """Two canonical snapshots, keep_last=1, no previous recorded: only the
        non-current (older) snapshot is scheduled; current survives."""
        list_json = [
            {"namespace": _NS, "name": "v_100"},
            {"namespace": _NS, "name": "v_400"},
        ]
        manager, mock_req = _real_cow_manager(list_json)
        cleanup = CleanupManager(query_tracker=MagicMock(spec=QueryTracker))
        sched = _make_scheduler(golden_repos_dir, manager, cleanup)

        # No swap -> no previous_path; current = v_400.
        sched.alias_manager.create_alias(
            "my-repo-global", _canonical(400), repo_name="my-repo"
        )

        with patch.dict(sys.modules, {"requests": mock_req}):
            with patch(
                "code_indexer.global_repos.refresh_scheduler.get_config_service"
            ) as gcs:
                gcs.return_value.get_config.return_value.snapshot_retention_keep_last = 1
                sched._enforce_retention("my-repo-global", _canonical(400))

        scheduled = cleanup.get_pending_cleanups()
        assert _canonical(400) not in scheduled
        assert scheduled == {_canonical(100)}
