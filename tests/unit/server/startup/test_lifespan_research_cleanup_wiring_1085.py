"""Bug #1085 regression guard: lifespan wires the research cleanup scheduler.

The Research Assistant workspace GC (``ResearchCleanupScheduler``) must be
imported, constructed and ``.start()``-ed during server STARTUP (before the
``yield`` boundary) so startup reconciliation removes orphaned
``~/.cidx-server/research/<uuid>`` dirs, and ``.stop()``-ed during SHUTDOWN
(after the yield), stored on ``app.state.research_cleanup_scheduler``.

Source-text + source-order guards (mirroring the Bug #1044 / Story #1079
wiring guards). They MUST fail before the lifespan wiring and pass after.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanResearchCleanupWiring:
    def test_scheduler_imported_constructed_and_stored(self):
        source = _LIFESPAN_PATH.read_text()
        assert "services.research_cleanup_service import" in source, (
            "lifespan.py must import from services.research_cleanup_service"
        )
        assert "ResearchCleanupScheduler" in source, (
            "lifespan.py must import the ResearchCleanupScheduler symbol"
        )
        assert "ResearchCleanupScheduler(" in source, (
            "lifespan.py must construct ResearchCleanupScheduler(...)"
        )
        assert "app.state.research_cleanup_scheduler" in source, (
            "lifespan.py must store the scheduler on "
            "app.state.research_cleanup_scheduler"
        )

    def test_construct_and_start_before_yield(self):
        source = _LIFESPAN_PATH.read_text()
        yield_pos = source.find("yield  # Server is now running")
        ctor_pos = source.find("ResearchCleanupScheduler(")
        assert yield_pos != -1, "could not locate the lifespan yield boundary"
        assert ctor_pos != -1, "ResearchCleanupScheduler(...) must be constructed"
        assert ctor_pos < yield_pos, (
            "ResearchCleanupScheduler must be constructed during STARTUP "
            "(before the yield)"
        )

        # The .start() call must appear after construction and before the yield.
        start_pos = source.find(".start()", ctor_pos)
        assert start_pos != -1, "scheduler.start() must be called"
        assert start_pos < yield_pos, (
            "research cleanup scheduler .start() must run during STARTUP "
            "(before the yield)"
        )

    def test_stop_after_yield(self):
        source = _LIFESPAN_PATH.read_text()
        yield_pos = source.find("yield  # Server is now running")
        state_pos = source.find('app.state, "research_cleanup_scheduler"')
        if state_pos == -1:
            state_pos = source.find("app.state.research_cleanup_scheduler", yield_pos)
        assert state_pos != -1, (
            "lifespan.py must reference app.state.research_cleanup_scheduler on "
            "shutdown"
        )
        assert state_pos > yield_pos, (
            "research cleanup scheduler shutdown handling must run AFTER the yield"
        )

        # A .stop() call must appear after the shutdown state lookup.
        stop_pos = source.find(".stop()", state_pos)
        assert stop_pos != -1 and stop_pos > yield_pos, (
            "research cleanup scheduler .stop() must run during SHUTDOWN "
            "(after the yield)"
        )
