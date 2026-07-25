"""Codex MEDIUM finding (round 5, Story #1458): FleetMigrationScheduler is
started in lifespan.py's startup block (`fleet_migration_scheduler.start()`,
stored at `app.state.fleet_migration_scheduler`) but is never stopped in the
shutdown block -- unlike its sibling schedulers (data_retention_scheduler,
research_cleanup_scheduler, activated_reaper_scheduler,
embedding_stats_retention_scheduler, description_refresh_scheduler), each of
which has an explicit `getattr(app.state, "<name>", None)` + `.stop()` +
try/except/log block in shutdown.

Source-text guard: same style as test_lifespan_clone_backend_wiring_bug1044.py
(TestLifespanCloneBackendWiringSourceGuard) -- verifies the wiring assignment
is present in lifespan.py source, without exercising the full lifespan
context manager (which the codebase has no existing scaffold for testing at
the unit level for ANY scheduler's shutdown wiring).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestFleetMigrationSchedulerShutdownWiring:
    def test_fleet_migration_scheduler_is_stopped_in_shutdown_block(self) -> None:
        """lifespan.py's shutdown block must call
        fleet_migration_scheduler.stop() (via the same
        getattr(app.state, "fleet_migration_scheduler", None) + .stop()
        pattern every sibling scheduler already uses), so the daemon
        thread is signaled to stop and joined on server shutdown/restart
        instead of being silently abandoned."""
        source = _LIFESPAN_PATH.read_text()

        # Anchor on the shutdown section (after the startup block that
        # calls .start()), so a match inside the startup block's own
        # `fleet_migration_scheduler.start()` line does not create a
        # false positive.
        startup_marker = "fleet_migration_scheduler.start()"
        startup_pos = source.find(startup_marker)
        assert startup_pos != -1, (
            "Sanity check failed: fleet_migration_scheduler.start() not "
            "found in lifespan.py -- test fixture assumption broken."
        )

        shutdown_region = source[startup_pos + len(startup_marker) :]

        has_stop_call = (
            "fleet_migration_scheduler" in shutdown_region
            and ".stop()" in shutdown_region
            and (
                'getattr(app.state, "fleet_migration_scheduler"' in shutdown_region
                or "fleet_migration_scheduler_state" in shutdown_region
            )
        )
        assert has_stop_call, (
            "Bug: lifespan.py never calls fleet_migration_scheduler.stop() "
            "during shutdown -- the daemon thread started at "
            "fleet_migration_scheduler.start() is silently abandoned on "
            "server shutdown/restart, unlike every sibling scheduler "
            "(data_retention_scheduler, research_cleanup_scheduler, "
            "activated_reaper_scheduler, "
            "embedding_stats_retention_scheduler, "
            "description_refresh_scheduler), each of which has an "
            "explicit getattr(app.state, ...) + .stop() shutdown block."
        )
