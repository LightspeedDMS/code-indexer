"""CleanupManager minimum-retention-age LIVE config sourcing (Story #1457
AC13, PT-13 follow-up).

AC13's floor value existed only as a hardcoded/constructor-frozen number
(``MIN_RETENTION_AGE_SECONDS = 900.0``). Per this codebase's "No Environment
Variables for Server Settings" / "Runtime settings via Web UI Config Screen"
invariant -- and mirroring the sibling ``snapshot_retention_keep_last``
config value, which ``refresh_scheduler.py`` reads LIVE via
``get_config_service().get_config()`` at the point of use rather than baking
it in at construction -- the floor must be re-readable at runtime without a
server restart.

Design: an optional ``min_retention_age_getter: Callable[[], float]``
constructor parameter. When provided, it is called LIVE on every retention
check (never cached), taking priority over the static
``min_retention_age_seconds`` value. When omitted (default), behavior is
BYTE-IDENTICAL to today -- the static value is used, exactly as every
existing CleanupManager test already expects. This keeps cleanup_manager.py
free of any direct import of the server-only ConfigService (avoiding the risk
of a background-thread hot-path accidentally constructing a real
ConfigService() inside a pure unit-test context that has no server
fixtures) -- the live wiring is injected as a callable, not looked up
in-module.
"""

from __future__ import annotations

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker


def test_getter_omitted_uses_static_value_byte_identical_to_today(tmp_path):
    """No getter provided -> unchanged behavior, using the static value."""
    target = tmp_path / "superseded-version"
    target.mkdir()

    tracker = QueryTracker()
    manager = CleanupManager(query_tracker=tracker, min_retention_age_seconds=0.2)

    manager.schedule_cleanup(str(target))
    manager._process_cleanup_queue()  # too soon
    assert target.exists()


def test_getter_provided_is_called_live_and_takes_priority_over_static_value(
    tmp_path,
):
    """A provided getter overrides the static constructor value and is
    consulted freshly on each check (not cached at construction time),
    proving true runtime reconfigurability."""
    target = tmp_path / "superseded-version"
    target.mkdir()

    tracker = QueryTracker()
    live_value = {
        "seconds": 100.0
    }  # static value would allow deletion; getter blocks it
    manager = CleanupManager(
        query_tracker=tracker,
        min_retention_age_seconds=0.0,  # would permit immediate deletion if used
        min_retention_age_getter=lambda: live_value["seconds"],
    )

    manager.schedule_cleanup(str(target))
    manager._process_cleanup_queue()
    assert target.exists(), (
        "the getter's larger value (100.0s) must override the static "
        "constructor value (0.0s) -- the getter takes priority"
    )

    # Live re-read: lowering the getter's return value takes effect on the
    # VERY NEXT check, with no re-construction of CleanupManager.
    live_value["seconds"] = 0.0
    manager._process_cleanup_queue()
    assert not target.exists(), (
        "the getter must be called LIVE on each check, not cached at "
        "construction time -- a lowered getter value must take effect "
        "immediately"
    )
