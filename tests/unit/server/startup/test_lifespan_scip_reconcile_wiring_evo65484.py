"""EVO-65484 regression guard: lifespan must schedule the periodic SCIP reconcile.

``GoldenRepoManager.reconcile_scip_from_filesystem()`` enqueues delegated SCIP builds
for SCIP-eligible golden repos that have none. If lifespan.py ever stops invoking it,
eligible repos silently never get SCIP again — so guard the wiring at the source level
(matching the sibling registry-reconcile wiring guard in this repo).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanScipReconcileWiringSourceGuard:
    def test_scip_reconcile_scheduled_in_lifespan_source(self):
        source = _LIFESPAN_PATH.read_text()
        assert "reconcile_scip_from_filesystem" in source, (
            "EVO-65484: lifespan.py no longer invokes reconcile_scip_from_filesystem "
            "— SCIP-eligible golden repos will silently never be backfilled."
        )
        assert "asyncio.create_task(" in source and "_run_scip_reconcile()" in source, (
            "EVO-65484: the SCIP reconcile must be scheduled as a background task "
            "(asyncio.create_task)."
        )
        assert "app.state.scip_reconcile_task" in source, (
            "EVO-65484: the SCIP reconcile task must be kept on app.state so it "
            "survives GC and can be cancelled on shutdown."
        )

    def test_scip_reconcile_is_periodic(self):
        source = _LIFESPAN_PATH.read_text()
        assert "CIDX_SCIP_RECONCILE_INTERVAL_SECONDS" in source, (
            "EVO-65484: the SCIP reconcile interval must be configurable "
            "(CIDX_SCIP_RECONCILE_INTERVAL_SECONDS)."
        )
        marker = "async def _run_scip_reconcile"
        start = source.find(marker)
        assert start != -1, "EVO-65484: _run_scip_reconcile helper is missing"
        body = source[start : source.find("app.state.scip_reconcile_task", start)]
        assert "while True:" in body and "asyncio.sleep" in body, (
            "EVO-65484: the SCIP reconcile helper must loop (while True + "
            "asyncio.sleep) to run periodically."
        )

    def test_scip_reconcile_runs_off_the_event_loop(self):
        source = _LIFESPAN_PATH.read_text()
        marker = "async def _run_scip_reconcile"
        start = source.find(marker)
        assert start != -1, "EVO-65484: _run_scip_reconcile helper is missing"
        body = source[start : source.find("app.state.scip_reconcile_task", start)]
        assert "to_thread.run_sync" in body, (
            "EVO-65484: reconcile_scip_from_filesystem must be dispatched via "
            "anyio.to_thread.run_sync so the NFS scan does not block the event loop."
        )

    def test_scip_reconcile_waits_for_leader_gate_readiness_in_postgres(self):
        source = _LIFESPAN_PATH.read_text()
        marker = "async def _run_scip_reconcile"
        start = source.find(marker)
        assert start != -1, "EVO-66188: _run_scip_reconcile helper is missing"
        body = source[start : source.find("app.state.scip_reconcile_task", start)]
        assert "scip_reconcile_leader_gate_ready" in source, (
            "EVO-66188: startup must initialize scip_reconcile_leader_gate_ready "
            "to prevent first-cycle leader-election races."
        )
        assert "if not _leader_gate_ready:" in body and "await asyncio.sleep(1)" in body, (
            "EVO-66188: SCIP reconcile must wait briefly when leader-gate wiring "
            "is not ready in postgres mode."
        )
        assert "app.state.scip_reconcile_leader_gate_ready = True" in source, (
            "EVO-66188: cluster-service startup must release the SCIP leader-gate "
            "hold when initialization completes."
        )

    def test_scip_reconcile_cancelled_on_shutdown(self):
        source = _LIFESPAN_PATH.read_text()
        assert 'getattr(app.state, "scip_reconcile_task"' in source, (
            "EVO-65484: the SCIP reconcile task must be cancelled on shutdown "
            "(getattr(app.state, 'scip_reconcile_task', None) -> cancel())."
        )
