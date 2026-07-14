"""Tests for the HNSW orphan repair sweep admin stats endpoint (Story #1360 AC4).

Accumulated cross-pass fleet stats (last full pass time, total orphans
repaired to date, current cursor position) are NOT modeled as a JobTracker
job -- they live on this small dedicated admin stats endpoint, backed by the
same durable state_backend the scheduler's cursor uses, read independently
of JobTracker (per the settled AC4 dashboard-pattern decision).
"""

from types import SimpleNamespace

import pytest


class _FakeStateBackend:
    def __init__(self, state):
        self._state = state

    def get_state(self):
        return self._state


def _make_request(state_backend):
    app_state = SimpleNamespace(
        backend_registry=SimpleNamespace(hnsw_orphan_sweep_state=state_backend)
    )
    app = SimpleNamespace(state=app_state)
    return SimpleNamespace(app=app)


class TestGetHNSWOrphanSweepStats:
    def test_returns_durable_state_fields(self) -> None:
        from code_indexer.server.routers.hnsw_orphan_sweep_admin import (
            get_hnsw_orphan_sweep_stats,
        )

        state = {
            "pass_id": 3,
            "last_completed_key": "golden:myrepo:.code-indexer/index/x/hnsw_index.bin",
            "pass_indexes_checked": 42,
            "pass_orphaned_found": 2,
            "pass_repaired": 2,
            "pass_errors": 0,
            "pass_transient_skips": 1,
            "last_full_pass_completed_at": "2026-07-01T00:00:00+00:00",
            "total_orphans_repaired_lifetime": 5,
        }
        request = _make_request(_FakeStateBackend(state))

        result = get_hnsw_orphan_sweep_stats(request, current_user=None)

        assert result["pass_id"] == 3
        assert result["total_orphans_repaired_lifetime"] == 5
        assert result["last_full_pass_completed_at"] == "2026-07-01T00:00:00+00:00"
        assert result["current_cursor"] == state["last_completed_key"]

    def test_raises_when_backend_registry_unavailable(self) -> None:
        from fastapi import HTTPException

        from code_indexer.server.routers.hnsw_orphan_sweep_admin import (
            get_hnsw_orphan_sweep_stats,
        )

        app_state = SimpleNamespace(backend_registry=None)
        app = SimpleNamespace(state=app_state)
        request = SimpleNamespace(app=app)

        with pytest.raises(HTTPException) as exc_info:
            get_hnsw_orphan_sweep_stats(request, current_user=None)

        assert exc_info.value.status_code == 503
