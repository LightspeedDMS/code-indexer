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

    def test_live_set_is_backend_aware_not_sqlite_only(self):
        """Bug #1085 BLOCKING-1 guard: the live set must be sourced from the
        ACTIVE research_sessions backend (postgres in cluster / sqlite in solo),
        via ``make_backend_live_folder_provider`` over ``research_sessions``.

        A SQLite-only provider read an EMPTY table in postgres mode and caused
        mass-deletion of live sessions. This guard fails if the wiring regresses
        to using ONLY make_db_live_folder_provider as the live source.
        """
        source = _LIFESPAN_PATH.read_text()
        assert "make_backend_live_folder_provider" in source, (
            "lifespan must wire the backend-aware live-set provider "
            "(make_backend_live_folder_provider)"
        )
        assert "backend_registry.research_sessions" in source, (
            "lifespan must source the live set from backend_registry.research_sessions"
        )

        ctor_args = self._scheduler_ctor_args(source)
        arg_value = self._kwarg_value(ctor_args, "live_folder_provider")
        assert arg_value is not None, (
            "ResearchCleanupScheduler must receive a live_folder_provider= argument"
        )
        assert "make_backend_live_folder_provider" in arg_value, (
            "the backend-aware provider must be the scheduler's "
            "live_folder_provider= argument value (got: " + arg_value + ")"
        )

    @staticmethod
    def _scheduler_ctor_args(source: str) -> str:
        """Return the argument text inside ResearchCleanupScheduler(...).

        Parenthesis-matched from the ctor open paren to its matching close
        paren -- no distance heuristic / magic number.
        """
        ctor_open = source.find("ResearchCleanupScheduler(")
        assert ctor_open != -1, "ResearchCleanupScheduler(...) must be constructed"
        scan = ctor_open + len("ResearchCleanupScheduler(")
        depth = 1
        for i in range(scan, len(source)):
            ch = source[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return source[scan:i]
        raise AssertionError("unterminated ResearchCleanupScheduler( call")

    @staticmethod
    def _kwarg_value(ctor_args: str, name: str):
        """Extract the value expression of ``name=...`` from a ctor arg block.

        Bounded by the next TOP-LEVEL comma (depth 0 across () [] {}) or the end
        of the block, so the returned expression is exactly that one argument's
        value -- never bleeding into sibling arguments.
        """
        key = name + "="
        start = ctor_args.find(key)
        if start == -1:
            return None
        i = start + len(key)
        depth = 0
        out = []
        while i < len(ctor_args):
            ch = ctor_args[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                break
            out.append(ch)
            i += 1
        return "".join(out).strip()

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
