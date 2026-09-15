"""Bug #1864: the HNSW orphan repair sweep stats endpoint must be able to
distinguish a scheduler that is DEAD from a pass that is genuinely IN
PROGRESS.

Before this fix the endpoint returned the durable state row and nothing
else::

    state = backend_registry.hnsw_orphan_sweep_state.get_state()
    return {**state, "current_cursor": state.get("last_completed_key")}

which means these three operator-relevant situations render BYTE-IDENTICAL:

  1. scheduler alive, pass genuinely in progress
  2. scheduler alive, pass wedged
  3. scheduler never started / dead

That is why a production node whose ``last_full_pass_completed_at`` was two
months stale raised no signal anywhere.

The discriminating assertion in every test below is an inequality between
two responses built over the SAME durable state row -- the tests fail on the
pre-fix code because the endpoint cannot TELL THE CASES APART, not merely
because some named field is absent.

Cluster correctness (CLAUDE.md "Cluster-Aware State"): the liveness block is
per-PROCESS RAM and is deliberately nested under ``local_scheduler`` with an
explicit ``scope`` marker so it can never be misread as a fleet-wide answer.

Typing note: ``Dict[str, Any]`` below is confined to JSON-shaped payloads --
the durable sweep state row and the endpoint's response body. Their values
are genuinely heterogeneous (``int``/``str``/``None``/nested object) and are
produced by a SQL row / consumed as JSON, so no narrower static type exists
without inventing a TypedDict that would have to be kept in lockstep with
two storage backends. Every other signature here is concretely typed.
"""

from types import SimpleNamespace
from typing import Any, Dict, Optional

# A pass mid-flight: a non-NULL cursor and a stale last-full-pass timestamp.
# This single row is what BOTH a healthy in-progress sweep and a stone-dead
# scheduler produce -- it is the ambiguity this bug is about.
_MID_PASS_STATE: Dict[str, Any] = {
    "pass_id": 7,
    "last_completed_key": "golden:alpha:.code-indexer/index/c1/hnsw_index.bin",
    "pass_indexes_checked": 31,
    "pass_orphaned_found": 1,
    "pass_repaired": 1,
    "pass_errors": 0,
    "pass_transient_skips": 0,
    "last_full_pass_completed_at": "2026-07-16T03:11:00+00:00",
    "total_orphans_repaired_lifetime": 12,
}


class _FakeStateBackend:
    """Controlled stand-in for HNSWOrphanSweepStateSqliteBackend's read side."""

    def __init__(self, state: Dict[str, Any]) -> None:
        self._state = state

    def get_state(self) -> Dict[str, Any]:
        return dict(self._state)


class _FakeLiveScheduler:
    """Stand-in for a real HNSWOrphanRepairSweepScheduler whose liveness
    snapshot is fully controlled by the test."""

    def __init__(self, liveness: Dict[str, Any]) -> None:
        self._liveness = liveness

    def get_liveness(self) -> Dict[str, Any]:
        return dict(self._liveness)


def _liveness(
    *,
    running: bool = True,
    last_tick_at: Optional[str] = "2026-09-15T12:00:00+00:00",
    last_tick_completed_at: Optional[str] = "2026-09-15T12:00:04+00:00",
    tick_in_progress: bool = False,
    last_tick_error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "scope": "local_process",
        "scheduler_running": running,
        "started_at": "2026-09-15T09:00:00+00:00",
        "last_loop_cycle_at": "2026-09-15T12:00:00+00:00",
        "last_cycle_skipped_reason": None,
        "last_tick_at": last_tick_at,
        "last_tick_completed_at": last_tick_completed_at,
        "last_tick_error": last_tick_error,
        "ticks_started": 9,
        "ticks_completed": 8 if tick_in_progress else 9,
        "tick_in_progress": tick_in_progress,
    }


def _make_request(
    state: Dict[str, Any],
    *,
    scheduler: Optional[_FakeLiveScheduler] = None,
    startup_error: Optional[str] = None,
) -> SimpleNamespace:
    """Build the minimal ``request.app.state`` surface the endpoint reads.

    Mirrors the established shape used by
    ``test_hnsw_orphan_sweep_admin_1360.py`` -- a real ``fastapi.Request``
    cannot be constructed without an ASGI scope, and the endpoint touches
    nothing but ``request.app.state``.
    """
    app_state = SimpleNamespace(
        backend_registry=SimpleNamespace(
            hnsw_orphan_sweep_state=_FakeStateBackend(state)
        ),
        hnsw_orphan_repair_sweep_scheduler=scheduler,
        hnsw_orphan_repair_sweep_startup_error=startup_error,
    )
    return SimpleNamespace(app=SimpleNamespace(state=app_state))


def _call(request: SimpleNamespace) -> Dict[str, Any]:
    from typing import cast

    from fastapi import Request

    from code_indexer.server.auth.user_manager import User
    from code_indexer.server.routers.hnsw_orphan_sweep_admin import (
        get_hnsw_orphan_sweep_stats,
    )

    # The endpoint touches nothing on `request` but `app.state`, and
    # `current_user` (the admin-auth dependency's result) is unused by the
    # body entirely -- passing None matches the Story #1360 tests. Both casts
    # exist only to state that deliberate test-double substitution to mypy;
    # neither changes what is actually passed.
    return get_hnsw_orphan_sweep_stats(
        cast(Request, request), current_user=cast(User, None)
    )


def _without_observation_time(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Drop the wall-clock observation stamp so two payloads can be compared
    for real informational difference rather than for having been produced a
    microsecond apart."""
    scrubbed = dict(payload)
    local = scrubbed.get("local_scheduler")
    if isinstance(local, dict):
        local_copy = dict(local)
        local_copy.pop("observed_at", None)
        scrubbed["local_scheduler"] = local_copy
    return scrubbed


class TestDeadSchedulerIsDistinguishableFromPassInProgress:
    def test_dead_and_in_progress_do_not_render_identically(self) -> None:
        """THE bug. Same durable row, opposite realities, one payload."""
        in_progress = _call(
            _make_request(
                _MID_PASS_STATE,
                scheduler=_FakeLiveScheduler(_liveness(tick_in_progress=True)),
            )
        )
        # The easiest real trigger of a dead scheduler: lifespan's
        # `backend_registry is None` guard raised, the whole construct-and-
        # start block was swallowed by its `except Exception`, and nothing
        # but one WARNING at boot recorded it.
        dead = _call(
            _make_request(
                _MID_PASS_STATE,
                scheduler=None,
                startup_error="backend_registry is not available",
            )
        )

        assert _without_observation_time(in_progress) != _without_observation_time(
            dead
        ), (
            "stats endpoint renders a dead scheduler identically to a sweep "
            "with a pass in progress -- an operator cannot tell them apart"
        )

    def test_dead_scheduler_says_it_is_not_running_and_why(self) -> None:
        dead = _call(
            _make_request(
                _MID_PASS_STATE,
                scheduler=None,
                startup_error="backend_registry is not available",
            )
        )

        local = dead["local_scheduler"]
        assert local["scheduler_running"] is False
        assert "backend_registry" in local["startup_error"]

    def test_running_scheduler_says_it_is_running_with_no_startup_error(self) -> None:
        alive = _call(
            _make_request(_MID_PASS_STATE, scheduler=_FakeLiveScheduler(_liveness()))
        )

        local = alive["local_scheduler"]
        assert local["scheduler_running"] is True
        assert local["startup_error"] is None
        assert local["last_tick_at"] == "2026-09-15T12:00:00+00:00"

    def test_durable_cross_pass_counters_are_preserved_unchanged(self) -> None:
        """The liveness block is strictly ADDITIVE -- Story #1360's durable
        fleet counters must survive byte-for-byte."""
        payload = _call(
            _make_request(_MID_PASS_STATE, scheduler=_FakeLiveScheduler(_liveness()))
        )

        for key, value in _MID_PASS_STATE.items():
            assert payload[key] == value
        assert payload["current_cursor"] == _MID_PASS_STATE["last_completed_key"]

    def test_liveness_block_is_scoped_to_this_process_not_the_fleet(self) -> None:
        """CLAUDE.md cluster rule: per-process RAM must never be presented as
        a fleet-wide answer."""
        payload = _call(
            _make_request(_MID_PASS_STATE, scheduler=_FakeLiveScheduler(_liveness()))
        )

        assert payload["local_scheduler"]["scope"] == "local_process"


class TestWedgedTickIsDistinguishableFromCompletedTick:
    def test_tick_stuck_in_progress_differs_from_tick_that_finished(self) -> None:
        """Case 2 vs case 1: both schedulers are alive and the durable row is
        the same; only whether a tick ever came back differs."""
        finished = _call(
            _make_request(
                _MID_PASS_STATE,
                scheduler=_FakeLiveScheduler(_liveness(tick_in_progress=False)),
            )
        )
        wedged = _call(
            _make_request(
                _MID_PASS_STATE,
                scheduler=_FakeLiveScheduler(
                    _liveness(
                        tick_in_progress=True,
                        last_tick_at="2026-07-16T03:10:00+00:00",
                        last_tick_completed_at=None,
                    )
                ),
            )
        )

        assert _without_observation_time(finished) != _without_observation_time(wedged)
        assert wedged["local_scheduler"]["tick_in_progress"] is True
        assert wedged["local_scheduler"]["last_tick_completed_at"] is None
        assert finished["local_scheduler"]["tick_in_progress"] is False

    def test_response_carries_an_observation_timestamp_for_staleness_math(
        self,
    ) -> None:
        """``last_tick_at`` alone cannot be judged stale without a reference
        clock the consumer can trust; the server supplies its own."""
        payload = _call(
            _make_request(_MID_PASS_STATE, scheduler=_FakeLiveScheduler(_liveness()))
        )

        assert payload["local_scheduler"]["observed_at"]


class TestAbsentSchedulerAttributesAreTolerated:
    def test_app_state_without_the_scheduler_attributes_still_answers(self) -> None:
        """A process that never reached the sweep-startup block at all (older
        app.state shape) must still get a well-formed, honest answer rather
        than a 500."""
        app_state = SimpleNamespace(
            backend_registry=SimpleNamespace(
                hnsw_orphan_sweep_state=_FakeStateBackend(_MID_PASS_STATE)
            )
        )
        request = SimpleNamespace(app=SimpleNamespace(state=app_state))

        payload = _call(request)

        assert payload["local_scheduler"]["scheduler_running"] is False
        assert payload["local_scheduler"]["startup_error"] is None
        assert payload["pass_id"] == 7
