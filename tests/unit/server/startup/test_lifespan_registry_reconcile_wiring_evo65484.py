"""EVO-65484 regression guard: lifespan must schedule the startup registry reconcile.

``GoldenRepoManager.reconcile_registry_from_filesystem()`` backfills registry rows
for golden repos that are indexed on disk but missing a registry entry -- the
registry/disk split-brain that makes the workspace-mcp seeder re-provision
already-indexed repos (EVO-65481). If lifespan.py ever stops invoking it, the drift
silently returns on every deployed node, so guard the wiring at the source level
(matching this package's established source-text wiring-guard convention).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanRegistryReconcileWiringSourceGuard:
    """Source-text guard: lifespan.py must schedule reconcile_registry_from_filesystem."""

    def test_registry_reconcile_scheduled_in_lifespan_source(self):
        source = _LIFESPAN_PATH.read_text()
        assert "reconcile_registry_from_filesystem" in source, (
            "EVO-65484: lifespan.py no longer invokes "
            "reconcile_registry_from_filesystem — the registry/disk split-brain "
            "(indexed-but-unregistered golden repos) silently returns on every node. "
            "Re-add the startup background task after the golden_repo_manager wiring."
        )
        assert "asyncio.create_task(_run_registry_reconcile())" in source, (
            "EVO-65484: the registry reconcile must be scheduled as a background "
            "task (asyncio.create_task) so it is actually invoked at startup."
        )

    def test_registry_reconcile_runs_off_the_event_loop(self):
        """The reconcile scans the golden set on NFS; it must run OFF the event loop
        (anyio.to_thread) so it never blocks the async lifespan / pod readiness."""
        source = _LIFESPAN_PATH.read_text()
        marker = "async def _run_registry_reconcile"
        start = source.find(marker)
        assert start != -1, "EVO-65484: _run_registry_reconcile helper is missing"
        # inspect only the helper body (up to the create_task that schedules it)
        body = source[
            start : source.find("asyncio.create_task(_run_registry_reconcile())", start)
        ]
        assert "to_thread.run_sync" in body, (
            "EVO-65484: reconcile_registry_from_filesystem must be dispatched via "
            "anyio.to_thread.run_sync so the NFS scan does not block the event loop."
        )
