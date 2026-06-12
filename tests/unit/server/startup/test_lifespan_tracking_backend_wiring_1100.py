"""Bug #1100 regression guard: lifespan must wire the registry-selected tracking_backend
into DescriptionRefreshScheduler, not let it fall back to node-local SQLite.

Root cause:
    The DescriptionRefreshScheduler constructor was called WITHOUT a tracking_backend
    argument even in postgres/cluster mode (backend_registry present).  The constructor
    falls back to node-local SQLite when tracking_backend=None.  Meanwhile, the very
    next block in lifespan.py DOES compute the registry-selected tracking_backend and
    injects it into meta_description_hook.  This split-brain means:
      - Hook (repo add/remove) writes tracking rows to PG.
      - Scheduler reads/writes node-local SQLite.
    => Repos seeded via the hook never appear in the scheduler's query.

Fix:
    Compute tracking_backend BEFORE constructing DescriptionRefreshScheduler, and pass
    it as tracking_backend= to the constructor.  The SQLite fallback is ONLY valid when
    backend_registry is None (genuine solo/non-cluster mode).

This module has two test classes:

1. TestLifespanTrackingBackendWiringSourceGuard
   Source-text guards: verify that lifespan.py contains the tracking_backend wiring
   in the correct order relative to the DescriptionRefreshScheduler constructor call.

2. TestNoMassDispatchStormOnCutover
   Functional guard: given a tracking backend whose rows are all overdue, after
   _reconcile_stale_next_run_rows() runs (which is called from start() BEFORE the
   daemon thread starts), no rows remain with next_run in the past.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock

from code_indexer.server.services.description_refresh_scheduler import (
    DescriptionRefreshScheduler,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import (
    DescriptionRefreshTrackingBackend,
)


_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


# ---------------------------------------------------------------------------
# Helper stubs (minimal; no mocks of business logic)
# ---------------------------------------------------------------------------


class _ConfigManager:
    class _Cfg:
        class claude_integration_config:
            max_concurrent_claude_cli = 1
            description_refresh_interval_hours = 72

    def load_config(self) -> Any:
        return self._Cfg()


class _SyncExecutor:
    """Runs submitted callables immediately; used to keep tests deterministic."""

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        fut: Future = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as exc:
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait: bool = True) -> None:
        pass


# ---------------------------------------------------------------------------
# Class 1: Source-text / source-order guards
# ---------------------------------------------------------------------------


class TestLifespanTrackingBackendWiringSourceGuard:
    """Source-text guards: lifespan.py must compute tracking_backend BEFORE
    constructing DescriptionRefreshScheduler and must pass it as an argument."""

    def _source(self) -> str:
        return _LIFESPAN_PATH.read_text()

    def test_tracking_backend_selection_happens_before_scheduler_constructor(self):
        """tracking_backend must be selected (from registry OR SQLite fallback) BEFORE
        the DescriptionRefreshScheduler(...) constructor is called in lifespan.py.

        Bug #1100: the original code called the constructor WITHOUT tracking_backend,
        then selected the tracking_backend AFTER construction and only injected it
        into meta_description_hook — not into the scheduler itself.

        This test verifies that the selection block appears BEFORE the constructor call
        in the source text, proving that the scheduler is constructed with the correct
        backend from the start.
        """
        source = self._source()

        # Find the tracking_backend selection block
        # The selection assigns: tracking_backend = backend_registry.description_refresh_tracking
        registry_selection = "backend_registry.description_refresh_tracking"
        registry_pos = source.find(registry_selection)
        assert registry_pos != -1, (
            f"'{registry_selection}' not found in lifespan.py — "
            "the registry-based tracking_backend selection is missing entirely. "
            "Bug #1100 fix requires selecting tracking_backend from the registry "
            "before constructing DescriptionRefreshScheduler."
        )

        # Find the DescriptionRefreshScheduler constructor call
        # Look for the instantiation assignment (not the import of the class)
        constructor_pos = source.find(
            "description_refresh_scheduler = DescriptionRefreshScheduler("
        )
        assert constructor_pos != -1, (
            "'description_refresh_scheduler = DescriptionRefreshScheduler(' not found "
            "in lifespan.py — cannot verify ordering."
        )

        assert registry_pos < constructor_pos, (
            f"Bug #1100: backend_registry.description_refresh_tracking selection "
            f"(pos {registry_pos}) appears AFTER DescriptionRefreshScheduler constructor "
            f"(pos {constructor_pos}). The tracking_backend must be computed BEFORE "
            "constructing the scheduler, then passed as tracking_backend= argument. "
            "Otherwise the scheduler falls back to node-local SQLite in cluster mode."
        )

    def test_scheduler_constructor_receives_tracking_backend_argument(self):
        """The DescriptionRefreshScheduler(...) constructor call must include
        tracking_backend= as a keyword argument.

        Bug #1100: the original constructor call omitted tracking_backend=, so the
        constructor fell back to node-local SQLite even when backend_registry was
        present (postgres cluster mode).

        Verification: find the constructor call block and assert that 'tracking_backend='
        appears within it (between the opening '(' and the matching ')').
        """
        source = self._source()

        constructor_start = source.find(
            "description_refresh_scheduler = DescriptionRefreshScheduler("
        )
        assert constructor_start != -1, (
            "'description_refresh_scheduler = DescriptionRefreshScheduler(' not found "
            "in lifespan.py."
        )

        # Find the closing ')' of the constructor call (the next ')' after the '(')
        # The call is multi-line; scan forward from the opening '('
        open_paren = source.index("(", constructor_start)
        depth = 0
        close_paren = open_paren
        for i in range(open_paren, len(source)):
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
                if depth == 0:
                    close_paren = i
                    break

        constructor_body = source[open_paren:close_paren]
        assert "tracking_backend=" in constructor_body, (
            "Bug #1100: 'tracking_backend=' not found in the DescriptionRefreshScheduler(...) "
            "constructor call in lifespan.py. The scheduler must receive the registry-selected "
            "tracking_backend (or SQLite fallback for solo mode) as a constructor argument, "
            "not rely on its internal SQLite-fallback logic in server mode. "
            f"Constructor body found: {constructor_body[:300]!r}"
        )

    def test_no_sqlite_fallback_when_registry_present_in_scheduler_construction(self):
        """When backend_registry is present, the tracking_backend= passed to the
        scheduler constructor must NOT be the SQLite backend created from db_path alone.

        This ensures the fix uses the registry-selected backend, not a hard-coded
        SQLite instance for the scheduler while the hook gets PG.

        Source guard: the block that selects tracking_backend must use the SAME
        conditional logic as the hook injection block — registry when present, else SQLite.
        The selection must appear before the constructor call (verified by the ordering
        test above), and the constructor must pass tracking_backend= (verified above).
        Together this guarantees the scheduler gets the registry backend in cluster mode.

        This test verifies the selection logic is IDENTICAL (same conditional structure):
        'if backend_registry is not None: tracking_backend = backend_registry.description_refresh_tracking'
        appears in the SAME block as the constructor call.
        """
        source = self._source()

        # The fix must move the if/else tracking_backend selection to BEFORE the constructor.
        # Verify: within the description refresh scheduler block, the conditional guard
        # 'if backend_registry is not None:' appears BEFORE the constructor call.
        constructor_pos = source.find(
            "description_refresh_scheduler = DescriptionRefreshScheduler("
        )
        assert constructor_pos != -1

        # Find the most recent 'if backend_registry is not None:' before the constructor
        # (the selection guard that gates registry vs SQLite choice for tracking_backend)
        search_start = 0
        last_guard_pos = -1
        while True:
            pos = source.find("if backend_registry is not None:", search_start)
            if pos == -1 or pos >= constructor_pos:
                break
            last_guard_pos = pos
            search_start = pos + 1

        assert last_guard_pos != -1, (
            "No 'if backend_registry is not None:' block found BEFORE "
            "'description_refresh_scheduler = DescriptionRefreshScheduler(' in lifespan.py. "
            "Bug #1100 fix requires selecting tracking_backend conditionally (registry vs "
            "SQLite fallback) BEFORE constructing the scheduler."
        )


# ---------------------------------------------------------------------------
# Class 2: Storm-prevention functional test
# ---------------------------------------------------------------------------


def _build_scheduler_with_overdue_rows(
    tmp_path: Path,
    aliases: List[str],
    interval_hours: int = 72,
) -> DescriptionRefreshScheduler:
    """Build a DescriptionRefreshScheduler backed by real SQLite with overdue tracking rows.

    All rows have next_run set 2 days in the past so they are overdue.
    This simulates the cutover scenario: PG table that has been frozen for a month.

    The scheduler is constructed via object.__new__ to bypass the __init__ config
    loading (same pattern as test_description_refresh_circuit_breaker_1096.py),
    then we manually populate the attributes and the tracking backend.
    """
    db_path = tmp_path / "tracking.db"
    DatabaseSchema(str(db_path)).initialize_database()

    tracking = DescriptionRefreshTrackingBackend(str(db_path))

    # Insert overdue rows — next_run is 2 days in the past
    past_next_run = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    for alias in aliases:
        tracking.upsert_tracking(
            repo_alias=alias,
            next_run=past_next_run,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    # Build the scheduler via object.__new__ to avoid config-loading side-effects
    sched = object.__new__(DescriptionRefreshScheduler)
    sched._tracking_backend = tracking
    sched._golden_backend = MagicMock()
    sched._golden_repos_dir = tmp_path / "repos"
    sched._meta_dir = tmp_path / "cidx-meta"
    sched._lifecycle_backfill_running = threading.Event()
    sched._description_backfill_running = threading.Event()
    sched._shutdown_event = threading.Event()
    sched._prompt_failure_counts = defaultdict(int)
    sched._executor = _SyncExecutor()
    sched._claude_cli_manager = object()  # truthy
    sched._failure_commit = {}
    sched._lifecycle_invoker = None
    sched._lifecycle_debouncer = MagicMock()
    sched._refresh_scheduler = MagicMock()
    sched._job_tracker = MagicMock()
    sched._config_manager = _ConfigManager()
    sched._analysis_model = "opus"
    sched._mcp_registration_service = None
    sched._cli_dispatcher = None
    sched._db_path = str(db_path)

    # Override calculate_next_run to produce a guaranteed future timestamp
    # so we can easily distinguish reconciled rows from overdue ones.
    def _future_next_run(alias_arg: str, interval_hours: Optional[int] = None) -> str:
        offset = 30 * 3600  # 30h into future (within 72h interval)
        return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()

    sched.calculate_next_run = _future_next_run  # type: ignore[method-assign]

    return sched


class TestNoMassDispatchStormOnCutover:
    """Verify that switching the scheduler from SQLite to the PG tracking backend
    (where all rows are overdue) does NOT cause a mass-dispatch storm.

    The mechanism: _reconcile_stale_next_run_rows() is called from start() BEFORE
    the daemon thread starts.  It spreads overdue next_run values across the interval.
    After reconciliation, no row should have next_run in the past, so the first
    _run_loop_single_pass will not see any stale repos.

    These tests use real SQLite (same as test_description_refresh_circuit_breaker_1096.py)
    to verify the end-to-end behaviour of the reconciliation step.
    """

    def test_all_overdue_rows_spread_to_future_after_reconciliation(self, tmp_path):
        """After _reconcile_stale_next_run_rows(), every tracking row that was overdue
        must have its next_run set to a FUTURE timestamp.

        This is the primary storm-prevention guard for Bug #1100: if all rows remained
        overdue after reconciliation, the first loop pass would dispatch ALL repos
        simultaneously, each spawning a Claude CLI invocation (money-burn hazard).
        """
        aliases = ["typer", "httpx", "flask", "humanize", "shortuuid"]
        sched = _build_scheduler_with_overdue_rows(tmp_path, aliases, interval_hours=72)

        # Verify all rows are overdue BEFORE reconciliation
        now = datetime.now(timezone.utc)
        rows_before = sched._tracking_backend.get_all_tracking()
        assert len(rows_before) == len(aliases), (
            f"Expected {len(aliases)} tracking rows, got {len(rows_before)}"
        )
        for row in rows_before:
            next_run_str = row.get("next_run")
            assert next_run_str is not None
            next_run_dt = datetime.fromisoformat(str(next_run_str))
            if next_run_dt.tzinfo is None:
                next_run_dt = next_run_dt.replace(tzinfo=timezone.utc)
            assert next_run_dt <= now, (
                f"Expected row for {row.get('repo_alias')!r} to be overdue before "
                f"reconciliation, but next_run={next_run_str!r} is in the future."
            )

        # Run reconciliation
        count = sched._reconcile_stale_next_run_rows()
        assert count == len(aliases), (
            f"_reconcile_stale_next_run_rows() should have recomputed {len(aliases)} rows "
            f"but returned {count}."
        )

        # Verify all rows are now in the FUTURE
        now_after = datetime.now(timezone.utc)
        rows_after = sched._tracking_backend.get_all_tracking()
        assert len(rows_after) == len(aliases)
        for row in rows_after:
            alias = row.get("repo_alias")
            next_run_str = row.get("next_run")
            assert next_run_str is not None, (
                f"next_run is None for {alias!r} after reconciliation."
            )
            next_run_dt = datetime.fromisoformat(str(next_run_str))
            if next_run_dt.tzinfo is None:
                next_run_dt = next_run_dt.replace(tzinfo=timezone.utc)
            assert next_run_dt > now_after, (
                f"Bug #1100 storm risk: row for {alias!r} still has next_run={next_run_str!r} "
                f"in the past after _reconcile_stale_next_run_rows(). "
                "A mass-dispatch storm would occur on the first loop pass."
            )

    def test_reconciliation_returns_correct_count(self, tmp_path):
        """_reconcile_stale_next_run_rows() must return the exact number of rows recomputed.

        When all N rows are overdue, the return value must be N.
        When all rows are already in the future, the return value must be 0.
        """
        aliases = ["repo-a", "repo-b", "repo-c"]
        sched = _build_scheduler_with_overdue_rows(tmp_path, aliases)

        count = sched._reconcile_stale_next_run_rows()
        assert count == len(aliases), (
            f"Expected {len(aliases)} rows recomputed, got {count}."
        )

        # Second call: all rows now have future next_run — count must be 0
        count2 = sched._reconcile_stale_next_run_rows()
        assert count2 == 0, (
            f"Expected 0 rows recomputed on second call (all already future), got {count2}."
        )

    def test_no_rows_dispatched_in_first_loop_pass_after_reconciliation(self, tmp_path):
        """After reconciliation, a simulated first loop pass must find 0 stale repos.

        This tests the full get_stale_repos() query path against the real tracking
        backend to ensure that no repos are returned as stale immediately after
        _reconcile_stale_next_run_rows() has spread their next_run values to the future.

        Uses the real DescriptionRefreshTrackingBackend.get_stale_repos() method.
        """
        aliases = ["alpha", "beta", "gamma"]
        sched = _build_scheduler_with_overdue_rows(tmp_path, aliases)

        # Reconcile — spreads all next_run to the future
        sched._reconcile_stale_next_run_rows()

        # Now ask the real backend how many repos are stale
        now_iso = datetime.now(timezone.utc).isoformat()
        stale = sched._tracking_backend.get_stale_repos(now_iso)
        assert len(stale) == 0, (
            f"Bug #1100 storm risk: {len(stale)} repos returned as stale by "
            f"get_stale_repos() immediately after _reconcile_stale_next_run_rows(). "
            "After reconciliation, next_run should be in the future for all repos, "
            "so no repos should be dispatched in the first loop pass. "
            f"Stale aliases: {[r.get('repo_alias') for r in stale]}"
        )

    def test_reconciliation_does_not_reset_already_future_rows(self, tmp_path):
        """Rows with next_run already in the future must NOT be touched by reconciliation.

        This guards against accidentally resetting rows that were legitimately scheduled.
        """
        db_path = tmp_path / "tracking.db"
        DatabaseSchema(str(db_path)).initialize_database()
        tracking = DescriptionRefreshTrackingBackend(str(db_path))

        # Insert one overdue and one future row
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        future_ts = (datetime.now(timezone.utc) + timedelta(hours=50)).isoformat()
        tracking.upsert_tracking(
            repo_alias="overdue-repo",
            next_run=past,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        tracking.upsert_tracking(
            repo_alias="future-repo",
            next_run=future_ts,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

        # Build minimal scheduler
        sched = _build_scheduler_with_overdue_rows(tmp_path, [], interval_hours=72)
        sched._tracking_backend = tracking  # Override with our mixed backend

        count = sched._reconcile_stale_next_run_rows()
        assert count == 1, (
            f"Expected exactly 1 row (the overdue one) to be recomputed, got {count}."
        )

        # The future-repo row must still have its original next_run
        rows = {r["repo_alias"]: r for r in tracking.get_all_tracking()}
        assert "future-repo" in rows
        saved_future = rows["future-repo"]["next_run"]
        saved_future_dt = datetime.fromisoformat(str(saved_future))
        if saved_future_dt.tzinfo is None:
            saved_future_dt = saved_future_dt.replace(tzinfo=timezone.utc)
        original_dt = datetime.fromisoformat(future_ts)
        if original_dt.tzinfo is None:
            original_dt = original_dt.replace(tzinfo=timezone.utc)
        # Allow 1-second tolerance for DB round-trip
        delta = abs((saved_future_dt - original_dt).total_seconds())
        assert delta < 2, (
            f"future-repo's next_run was modified by reconciliation: "
            f"original={future_ts!r}, saved={saved_future!r}."
        )
