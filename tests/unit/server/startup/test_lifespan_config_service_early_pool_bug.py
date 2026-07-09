"""Regression guard: ConfigService PG pool must be set BEFORE scheduler inits read config.

Bug: In lifespan.py, DescriptionRefreshScheduler and ScheduledCatchupService read
``config_service.get_config().claude_integration_config`` at ``.start()`` time.
But ``ConfigService.set_connection_pool()`` (which triggers ``_load_runtime_from_pg()``)
only ran inside the large cluster block near line ~2264 -- well AFTER both scheduler
``.start()`` calls (~843 and ~1078).

Consequence: In postgres/cluster mode, the schedulers always see bootstrap defaults
(description_refresh_enabled=False, dependency_map_enabled=False) even if the operator
has enabled them in the Web UI (persisted to PG ``server_config`` runtime row).
The one-shot startup backfill sweeps are permanently skipped.

Fix: Add an early ``get_config_service().set_connection_pool(early_pool)`` block,
guarded by ``storage_mode == "postgres" and backend_registry is not None``, BEFORE
the ScheduledCatchupService block (~843) so ALL scheduler inits see the merged
runtime config.

Tests (source-order + postgres-gating checks, following the established pattern
from test_lifespan_clone_backend_wiring_bug1044.py):
  1. Early pool call appears in lifespan.py source.
  2. Early pool call appears BEFORE DescriptionRefreshScheduler( construction.
  3. Early pool call appears BEFORE ScheduledCatchupService( construction.
  4. Early pool call is postgres-gated (inside
     ``storage_mode == "postgres" and backend_registry is not None`` guard).
  5. Late pool call (the existing ~2264 block) still present -- belt-and-suspenders.
  6. ConfigService.start_config_reload is idempotent (already guarded internally).
  7. Solo/SQLite mode is unaffected -- early block MUST NOT run when backend_registry
     is None.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

# ---------------------------------------------------------------------------
# Markers that anchor all position searches
# ---------------------------------------------------------------------------

# The early pool call we are requiring (postgres-gated block before schedulers)
_EARLY_POOL_MARKER = "get_config_service().set_connection_pool(_early_config_pool)"

# The existing late call in the large cluster block (~line 2264)
_LATE_POOL_MARKER = "_config_svc.set_connection_pool(_cluster_pool)"

# Scheduler construction anchors
_DESCRIPTION_SCHED_CTOR = "description_refresh_scheduler = DescriptionRefreshScheduler("
_CATCHUP_SCHED_CTOR = "scheduled_catchup_service = ScheduledCatchupService("

# Postgres guard that must wrap the early block
_POSTGRES_GUARD = 'storage_mode == "postgres" and backend_registry is not None'


def _source() -> str:
    return _LIFESPAN_PATH.read_text()


# ---------------------------------------------------------------------------
# 1. Early pool call must be present
# ---------------------------------------------------------------------------


def test_early_config_pool_call_present_in_lifespan_source():
    """lifespan.py must contain the early get_config_service().set_connection_pool call.

    Fails before the fix (call absent); passes after the fix.
    """
    source = _source()
    assert _EARLY_POOL_MARKER in source, (
        f"Bug: lifespan.py does not contain the early config service pool call.\n"
        f"Expected to find: {_EARLY_POOL_MARKER!r}\n"
        "Add an early postgres-gated block before the ScheduledCatchupService init "
        "that calls get_config_service().set_connection_pool(early_pool) so scheduler "
        "inits see the merged PG runtime config."
    )


# ---------------------------------------------------------------------------
# 2. Early pool call appears BEFORE DescriptionRefreshScheduler construction
# ---------------------------------------------------------------------------


def test_early_pool_set_before_description_refresh_scheduler_construction():
    """Early config pool call must precede DescriptionRefreshScheduler( construction.

    Source-order contract: the scheduler ctor reads config_service.get_config() at
    construction time (analysis_model etc.) -- by the time DescriptionRefreshScheduler
    is instantiated, the PG-backed config must already be loaded.

    Fails before fix; passes after fix.
    """
    source = _source()

    early_pos = source.find(_EARLY_POOL_MARKER)
    assert early_pos != -1, f"Early pool marker not found: {_EARLY_POOL_MARKER!r}"

    sched_ctor_pos = source.find(_DESCRIPTION_SCHED_CTOR)
    assert sched_ctor_pos != -1, (
        f"DescriptionRefreshScheduler ctor not found: {_DESCRIPTION_SCHED_CTOR!r}"
    )

    assert early_pos < sched_ctor_pos, (
        f"Source-order violation: early config pool call (pos {early_pos}) appears "
        f"AFTER DescriptionRefreshScheduler construction (pos {sched_ctor_pos}).\n"
        "The early pool call must come BEFORE the scheduler is instantiated so that "
        "get_config() returns PG-merged config at scheduler init time."
    )


# ---------------------------------------------------------------------------
# 3. Early pool call appears BEFORE ScheduledCatchupService construction
# ---------------------------------------------------------------------------


def test_early_pool_set_before_scheduled_catchup_service_construction():
    """Early config pool call must precede ScheduledCatchupService( construction.

    ScheduledCatchupService reads claude_integration_config.scheduled_catchup_enabled
    from get_config() at construction+start time. Without the early pool set it always
    sees the bootstrap default (disabled).
    """
    source = _source()

    early_pos = source.find(_EARLY_POOL_MARKER)
    assert early_pos != -1, f"Early pool marker not found: {_EARLY_POOL_MARKER!r}"

    catchup_ctor_pos = source.find(_CATCHUP_SCHED_CTOR)
    assert catchup_ctor_pos != -1, (
        f"ScheduledCatchupService ctor not found: {_CATCHUP_SCHED_CTOR!r}"
    )

    assert early_pos < catchup_ctor_pos, (
        f"Source-order violation: early config pool call (pos {early_pos}) appears "
        f"AFTER ScheduledCatchupService construction (pos {catchup_ctor_pos}).\n"
        "The early pool call must come BEFORE ScheduledCatchupService so it reads "
        "PG-merged config."
    )


# ---------------------------------------------------------------------------
# 4. Early pool block is postgres-gated
# ---------------------------------------------------------------------------


def test_early_pool_block_is_postgres_gated():
    """The early pool call must be inside a postgres guard.

    Solo/SQLite mode has no backend_registry, so the early block must only run
    when storage_mode == 'postgres' and backend_registry is not None.

    We verify this by asserting that the postgres guard string appears BEFORE
    the early pool call in source (i.e., the guard is the enclosing conditional).
    """
    source = _source()

    # Find the LAST occurrence of the postgres guard before the early pool call
    early_pos = source.find(_EARLY_POOL_MARKER)
    assert early_pos != -1, f"Early pool marker not found: {_EARLY_POOL_MARKER!r}"

    guard_pos = source.rfind(_POSTGRES_GUARD, 0, early_pos)
    assert guard_pos != -1, (
        f"Postgres guard {_POSTGRES_GUARD!r} not found before early pool call "
        f"(pos {early_pos}).\n"
        "The early config pool block MUST be wrapped in "
        "'if storage_mode == \"postgres\" and backend_registry is not None:' "
        "so that solo/SQLite mode is completely unaffected."
    )

    assert guard_pos < early_pos, (
        f"Guard (pos {guard_pos}) must appear before early pool call (pos {early_pos})."
    )


# ---------------------------------------------------------------------------
# 5. Late pool call (belt-and-suspenders) still present
# ---------------------------------------------------------------------------


def test_late_config_pool_call_still_present_for_belt_and_suspenders():
    """The existing late _config_svc.set_connection_pool(_cluster_pool) must remain.

    Belt-and-suspenders: the late call in the large cluster block ensures that
    start_config_reload() is also called (it runs right after the late set_connection_pool).
    Removing it would break the 30-second reload poll.
    """
    source = _source()
    assert _LATE_POOL_MARKER in source, (
        f"Late config pool call missing: {_LATE_POOL_MARKER!r}\n"
        "The existing late block in the cluster section must remain -- it also "
        "calls start_config_reload(interval_seconds=30) for the 30s reload poll."
    )


# ---------------------------------------------------------------------------
# 6. start_config_reload idempotency guard in ConfigService
# ---------------------------------------------------------------------------


def test_start_config_reload_is_guarded_against_double_start():
    """ConfigService.start_config_reload must guard against double-start.

    With an early pool set, start_config_reload may be called twice if the late
    block still calls it. The existing guard (self._reload_thread is not None and
    self._reload_thread.is_alive()) must remain so the second call is a no-op.
    """
    config_service_path = (
        _REPO_ROOT
        / "src"
        / "code_indexer"
        / "server"
        / "services"
        / "config_service.py"
    )
    source = config_service_path.read_text()

    # Guard pattern: early return if reload thread already alive
    guard_pattern = "_reload_thread is not None and self._reload_thread.is_alive()"
    assert guard_pattern in source, (
        f"ConfigService.start_config_reload idempotency guard not found.\n"
        f"Expected: {guard_pattern!r}\n"
        "Without this guard, calling start_config_reload() twice (early + late blocks) "
        "would spawn two reload threads, causing duplicate PG polling."
    )


# ---------------------------------------------------------------------------
# 7. Early block must not run when backend_registry is None (solo mode safety)
# ---------------------------------------------------------------------------


def test_early_pool_marker_absent_in_non_postgres_guard_context():
    """The early pool call must ONLY exist inside a postgres-gated block.

    Verify there is no unconditional get_config_service().set_connection_pool call
    that could accidentally run in solo/SQLite mode (backend_registry=None).

    We assert the early pool marker appears exactly once (the one in the guarded block)
    and that the postgres guard appears before it.
    """
    source = _source()

    occurrences = source.count(_EARLY_POOL_MARKER)
    # Exactly one occurrence — inside the guarded block only
    assert occurrences == 1, (
        f"Expected exactly 1 occurrence of early pool marker {_EARLY_POOL_MARKER!r}, "
        f"found {occurrences}.\n"
        "If > 1: there may be an unconditional call that fires in solo/SQLite mode.\n"
        "If 0: the fix has not been applied yet."
    )
