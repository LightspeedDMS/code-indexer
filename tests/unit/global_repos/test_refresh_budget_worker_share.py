"""The per-cycle refresh budget is a node budget too, so it divides.

``max_concurrent_refresh_jobs`` derives from ``max_concurrent_background_jobs``
(``max(1, n // 2)``, default 2) and bounds how many golden-repo refreshes this
scheduler submits per poll. The scheduler loop runs once per uvicorn worker
PROCESS, so on a 2-worker pod the node submitted 2x its own stated budget, and
the fleet 12x.

This is the one budget where getting it wrong is self-reinforcing: refreshes are
the heaviest thing the server does on the NFS mount, and the memory pressure they
cause is what made the oversubscription visible in the first place.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from code_indexer.global_repos import refresh_scheduler as rs


class _Manager:
    def __init__(self, refresh_limit: int, active: int) -> None:
        self._background_jobs_config = SimpleNamespace(
            max_concurrent_refresh_jobs=refresh_limit
        )
        self.max_concurrent_jobs = 5
        self._active = active

    def count_active_refresh_jobs(self) -> int:
        return self._active


def _budget(monkeypatch, *, workers: int, limit: int, active: int = 0) -> int:
    monkeypatch.setattr(rs, "detect_worker_count", lambda: workers)
    scheduler = rs.RefreshScheduler.__new__(rs.RefreshScheduler)
    scheduler.background_job_manager = _Manager(limit, active)
    return scheduler._get_refresh_budget()


@pytest.mark.parametrize(
    "workers,limit,expected",
    [
        (1, 4, 4),
        (2, 4, 2),
        (2, 5, 2),
        (4, 4, 1),
        (12, 2, 1),
    ],
)
def test_budget_is_this_process_s_share(monkeypatch, workers, limit, expected):
    assert _budget(monkeypatch, workers=workers, limit=limit) == expected


def test_a_single_worker_node_is_unchanged(monkeypatch):
    """The VM/solo deployment must behave exactly as before."""
    assert _budget(monkeypatch, workers=1, limit=2) == 2


def test_active_jobs_are_subtracted_from_the_divided_budget(monkeypatch):
    """Division happens to the LIMIT, not to the remainder.

    Subtracting first and dividing after would hand a process with two running
    refreshes a budget of 1 instead of 0, i.e. the gate would leak one slot per
    cycle forever.
    """
    assert _budget(monkeypatch, workers=2, limit=4, active=2) == 0
    assert _budget(monkeypatch, workers=2, limit=4, active=1) == 1


def test_budget_is_never_negative(monkeypatch):
    assert _budget(monkeypatch, workers=2, limit=4, active=99) == 0


def test_cli_mode_without_a_manager_is_untouched(monkeypatch):
    """No manager means CLI, which runs refreshes synchronously -- no gate."""
    monkeypatch.setattr(rs, "detect_worker_count", lambda: 12)
    scheduler = rs.RefreshScheduler.__new__(rs.RefreshScheduler)
    scheduler.background_job_manager = None
    assert scheduler._get_refresh_budget() == 10_000
