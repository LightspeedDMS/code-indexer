"""issue-65484 regression guard: the periodic SCIP reconcile is scheduled.

``reconcile_scip_from_filesystem`` enqueues delegated SCIP builds for
SCIP-eligible golden repos that have none. Since modular phase 4d it is the
scip feature's recurring background job (``ScipFeature.background_jobs()``),
run by the core's feature job scheduler -- on a worker thread, one node per
tick (the cluster-wide dedup that replaced the issue-66188 leader gate), stopped
on shutdown -- instead of a lifespan task. If the feature stops contributing
it, eligible repos silently never get SCIP again.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import patch

import pytest

from cidx_scip.feature import RECONCILE_JOB, make_feature

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


@pytest.fixture
def reconcile_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIDX_SCIP_AUTO_RECONCILE_ENABLED", "true")
    monkeypatch.delenv("CIDX_SCIP_RECONCILE_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("CIDX_SCIP_RECONCILE_MAX_PER_CYCLE", raising=False)


class TestScipReconcileJob:
    def test_scip_reconcile_is_a_recurring_feature_job(self, reconcile_enabled):
        (job,) = make_feature().background_jobs()
        assert job.name == RECONCILE_JOB
        assert job.interval_seconds == 1800.0

    def test_scip_reconcile_interval_is_configurable(
        self, reconcile_enabled, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CIDX_SCIP_RECONCILE_INTERVAL_SECONDS", "90")
        (job,) = make_feature().background_jobs()
        assert job.interval_seconds == 90.0

    def test_no_job_while_the_operator_has_not_opted_in(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("CIDX_SCIP_AUTO_RECONCILE_ENABLED", raising=False)
        assert make_feature().background_jobs() == ()

    def test_the_job_runs_one_bounded_reconcile_cycle(
        self, reconcile_enabled, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CIDX_SCIP_RECONCILE_MAX_PER_CYCLE", "3")
        grm = SimpleNamespace(name="golden repo manager")
        calls: List[Any] = []

        def reconcile(manager: Any, config: Any, max_enqueue: int) -> dict:
            calls.append((manager, config.scip_backend, max_enqueue))
            return {}

        (job,) = make_feature().background_jobs()
        with (
            patch("cidx_scip.server._core.get_golden_repo_manager", return_value=grm),
            patch(
                "cidx_scip.server.reconcile.reconcile_scip_from_filesystem",
                side_effect=reconcile,
            ),
        ):
            job.run(SimpleNamespace())
        assert calls == [(grm, "local", 3)]

    def test_lifespan_no_longer_schedules_its_own_loop(self):
        source = _LIFESPAN_PATH.read_text()
        assert "_run_scip_reconcile" not in source
        assert "reconcile_scip_from_filesystem" not in source
