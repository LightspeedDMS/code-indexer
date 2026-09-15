"""HNSW orphan repair fleet sweep scheduler (Story #1360, Epic #1333 S3).

Component 3 of the discovery mechanism: the paced/resumable job that
composes Component 1/2 discovery (discovery.py) and the per-item
check+repair executor (repair_executor.py) into a durable, cluster-safe
background sweep.

Dashboard pattern (settled 2026-07-11, see the issue's AC4 section): ONE
short BackgroundJobManager/JobTracker job PER TICK -- mirrors
``ActivatedReaperScheduler.trigger_now()`` exactly. The multi-tick PASS is
never itself a job (the job-tracker model auto-force-fails anything running
past a 24h stale threshold and unconditionally kills running/pending jobs on
restart -- a multi-day job would break against both). Cross-pass accumulated
stats live in the durable state backend, read via ``get_stats()`` --
independent of JobTracker, exposed on the admin stats surface.

Cluster correctness (AC3): single-flight ONLY via
``register_job_if_no_conflict`` (through ``background_job_manager.submit_job``,
identical to every other scheduler in this codebase). Deliberately NOT
filtered by ``ShardOwnership.owns()`` -- see discovery.py's module docstring
for why that would create a coverage gap under this story's model.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, cast

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
    enumerate_sweep_candidates,
)
from code_indexer.server.services.hnsw_orphan_sweep.repair_executor import (
    SweepOutcome,
    process_candidate,
)

logger = logging.getLogger(__name__)

# Granularity of the sleep loop: check stop_event this often (seconds) --
# mirrors ActivatedReaperScheduler's _TICK_SECONDS pattern.
_TICK_SECONDS = 60

# Poll cadence used while the sweep is disabled/outside its operating-hours
# window, so re-enabling it (or re-entering the window) takes effect
# promptly without a server restart.
_DISABLED_POLL_SECONDS = 60

# Safe fallback cadence when config cannot be read.
_DEFAULT_TICK_INTERVAL_MINUTES = 7
_DEFAULT_BATCH_SIZE = 15

# Bug #1529: _PERSISTENCE_OUTCOME_ALIASES existed solely to map the two
# now-deleted sister-temporal outcomes onto the state backend's original
# Story #1360 vocabulary. Fixed-path temporal shards produce ordinary
# in-place outcomes, so no aliasing is needed any more.

# Fail-open default operating-hours window: (0, 0) means "always on" (24x7),
# matching the pre-#1397 default behavior.
_DEFAULT_WINDOW_START_UTC = 0
_DEFAULT_WINDOW_END_UTC = 0

# Bug #1864: the liveness snapshot describes THIS SERVER PROCESS only -- the
# scheduler runs in every worker of every node, and per-process RAM is the
# only honest place for "is my loop thread alive". CLAUDE.md's Cluster-Aware
# State rule forbids presenting such a value as a fleet-wide answer, so the
# snapshot carries its own scope marker and the admin endpoint nests it under
# a `local_scheduler` key rather than mixing it into the durable fleet
# counters.
_LIVENESS_SCOPE = "local_process"

# Reasons a loop cycle deliberately submitted no tick (Story #1397 gates).
# Distinguishing these from "the loop is not running at all" is the whole
# point: a sweep that is OFF by configuration is a decision, not a failure.
CYCLE_SKIPPED_DISABLED = "disabled"
CYCLE_SKIPPED_OUTSIDE_WINDOW = "outside_operating_window"


def absent_local_scheduler_liveness() -> Dict[str, Any]:
    """Liveness snapshot for a process where the scheduler object does not
    exist at all -- it was never constructed, or its construction/``start()``
    failed at boot and ``lifespan.py`` recorded the reason on ``app.state``.

    This is ALSO the key template ``get_liveness()`` fills in, so both render
    the identical key set and a monitoring consumer never has to branch on
    the payload's shape. Returns a fresh dict on every call: the admin
    endpoint stamps per-request fields into it.
    """
    return {
        "scope": _LIVENESS_SCOPE,
        "scheduler_running": False,
        "started_at": None,
        "last_loop_cycle_at": None,
        "last_cycle_skipped_reason": None,
        "last_tick_at": None,
        "last_tick_completed_at": None,
        "last_tick_error": None,
        "ticks_started": 0,
        "ticks_completed": 0,
        "tick_in_progress": False,
    }


def _iso(value: Optional[datetime]) -> Optional[str]:
    """JSON-safe timestamp rendering; None passes through unchanged."""
    return None if value is None else value.isoformat()


def _is_within_operating_window(current_hour_utc: int, start: int, end: int) -> bool:
    """Pure, thread/clock-free UTC operating-hours window check (Story #1397).

    All arguments are integers 0-23. ``start == end`` means "always run"
    (24x7) -- this is the locked design decision and covers the (0, 0)
    default. ``start < end`` is a same-day, half-open window
    ``[start, end)``. ``start > end`` is an overnight wrap-around window
    (e.g. 22 -> 6 includes hours 22, 23, 0, 1, ..., 5).
    """
    if start == end:
        return True
    if start < end:
        return start <= current_hour_utc < end
    return current_hour_utc >= start or current_hour_utc < end


class HNSWOrphanRepairSweepScheduler:
    """Paced, resumable, cluster-safe HNSW fleet orphan-repair sweep.

    Each tick: claim the single global tick job via
    ``register_job_if_no_conflict`` (through
    ``background_job_manager.submit_job``), enumerate candidates in stable
    sort-key order, process up to ``batch_size`` items whose key is greater
    than the durable cursor, persisting the cursor after EACH item. On
    exhaustion (no key greater than the cursor across the full current
    enumeration), record pass stats and start a new pass.
    """

    OPERATION_TYPE = "hnsw_orphan_repair_sweep"

    def __init__(
        self,
        *,
        golden_repo_manager: Any,
        activated_repo_manager: Any,
        state_backend: Any,
        background_job_manager: Optional[Any],
        config_service: Any,
        process_fn: Callable[[Any], SweepOutcome] = process_candidate,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        """
        Args:
            golden_repo_manager: Object satisfying discovery.py's
                golden_repo_manager surface.
            activated_repo_manager: Object satisfying discovery.py's
                activated_repo_manager surface.
            state_backend: HNSWOrphanSweepStateSqliteBackend or
                HNSWOrphanSweepStatePostgresBackend instance (durable cursor
                + pass stats).
            background_job_manager: BackgroundJobManager instance used to
                submit one short job per tick (dashboard visibility +
                cross-worker single-flight). May be None only when the
                scheduler is used purely for direct ``_run_tick()`` calls in
                tests -- ``trigger_now()``/``start()`` require a real one.
            config_service: Object with ``get_config()`` returning a config
                exposing ``hnsw_orphan_repair_sweep_config`` (enabled,
                batch_size, tick_interval_minutes).
            process_fn: Injectable per-item processor for golden/activated
                (in-repo) candidates (defaults to the real
                ``process_candidate``); tests may inject a spy/fake.
            now_fn: Injectable clock hook returning the current time (defaults
                to the real UTC wall clock). Used by the operating-hours
                window gate (Story #1397) to determine the current UTC hour;
                tests inject a fixed value for deterministic window checks.
        """
        self._golden_repo_manager = golden_repo_manager
        self._activated_repo_manager = activated_repo_manager
        self._state_backend = state_backend
        self._background_job_manager = background_job_manager
        self._config_service = config_service
        self._process_fn = process_fn
        self._now_fn = now_fn

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Bug #1864 liveness state. Three different threads touch these --
        # the scheduler loop thread (cycle heartbeat), a bgm-worker thread
        # (tick start/finish, since trigger_now() submits _run_tick to the
        # BackgroundJobManager) and an HTTP request thread (get_liveness) --
        # so every read and write is taken under this lock. All operations
        # are O(1) with no I/O: the admin stats endpoint must never walk
        # repos (CLAUDE.md design-for-900-repos).
        self._liveness_lock = threading.Lock()
        self._started_at: Optional[datetime] = None
        self._last_loop_cycle_at: Optional[datetime] = None
        self._last_cycle_skipped_reason: Optional[str] = None
        self._last_tick_at: Optional[datetime] = None
        self._last_tick_completed_at: Optional[datetime] = None
        self._last_tick_error: Optional[str] = None
        self._ticks_started = 0
        self._ticks_completed = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the daemon thread."""
        self._stop_event.clear()
        with self._liveness_lock:
            self._started_at = self._now_fn()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="HNSWOrphanRepairSweepScheduler",
        )
        self._thread.start()
        logger.info("HNSWOrphanRepairSweepScheduler started")

    def stop(self) -> None:
        """Signal the scheduler to stop and wait for the thread to finish."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("HNSWOrphanRepairSweepScheduler stopped")

    # ------------------------------------------------------------------
    # Manual trigger / tick job submission
    # ------------------------------------------------------------------

    def trigger_now(self) -> Optional[str]:
        """Submit one tick job immediately.

        Returns:
            job_id, or None when another worker already claimed this tick
            (DuplicateJobError) -- benign and expected under multi-worker
            deployments, mirroring every other scheduler in this codebase.
        """
        assert self._background_job_manager is not None, (
            "trigger_now() requires a real background_job_manager"
        )
        try:
            job_id: str = self._background_job_manager.submit_job(
                self.OPERATION_TYPE,
                self._run_tick,
                submitter_username="system",
                is_admin=True,
                repo_alias="server",
            )
        except DuplicateJobError:
            logger.debug(
                "HNSWOrphanRepairSweepScheduler: tick already claimed by "
                "another worker; skipping"
            )
            return None

        logger.info(
            "HNSWOrphanRepairSweepScheduler: triggered tick (job_id=%s)", job_id
        )
        return job_id

    # ------------------------------------------------------------------
    # Tick execution
    # ------------------------------------------------------------------

    def _batch_size(self) -> int:
        try:
            return int(
                self._config_service.get_config().hnsw_orphan_repair_sweep_config.batch_size
            )
        except Exception as exc:
            logger.warning(
                "HNSWOrphanRepairSweepScheduler: failed to read batch_size from "
                "config, using default %d: %s",
                _DEFAULT_BATCH_SIZE,
                exc,
            )
            return _DEFAULT_BATCH_SIZE

    def _run_tick(self) -> Dict[str, Any]:
        """Thin Bug #1864 liveness wrapper around :meth:`_run_tick_impl`.

        Mirrors the established wrapper pattern used by Story #1586's
        ``RefreshScheduler._execute_refresh``: the real body is renamed to
        ``*_impl`` and this wrapper only times/records it, so the tick's own
        contract (its per-outcome counts dict) is returned untouched.

        A tick that raises is recorded as FINISHED-with-error and then
        re-raised unchanged -- swallowing it here would hide the failure from
        the BackgroundJobManager, and leaving ``tick_in_progress`` latched
        True forever would make every later snapshot claim a wedge that is
        not happening.
        """
        self._record_tick_started()
        try:
            result = self._run_tick_impl()
        except Exception as exc:
            self._record_tick_finished(error=f"{type(exc).__name__}: {exc}")
            raise
        self._record_tick_finished(error=None)
        return result

    def _record_tick_started(self) -> None:
        with self._liveness_lock:
            self._ticks_started += 1
            self._last_tick_at = self._now_fn()

    def _record_tick_finished(self, *, error: Optional[str]) -> None:
        with self._liveness_lock:
            self._ticks_completed += 1
            self._last_tick_completed_at = self._now_fn()
            self._last_tick_error = error

    def _run_tick_impl(self) -> Dict[str, Any]:
        """Process up to ``batch_size`` candidates beyond the durable
        cursor, persisting progress after EACH item. Returns per-tick
        outcome counts."""
        batch_size = self._batch_size()
        state = self._state_backend.get_state()
        cursor = state["last_completed_key"]

        all_candidates = list(
            enumerate_sweep_candidates(
                self._golden_repo_manager, self._activated_repo_manager
            )
        )
        # Bug #1529: no separate sister enumeration. Server-context temporal
        # shards live at a FIXED, mutable path and are yielded by
        # enumerate_sweep_candidates above (kind="golden_temporal"), so they
        # flow through the SAME in-place repair as every other collection.
        candidates = sorted(all_candidates, key=lambda c: c.sort_key)
        pending = [c for c in candidates if cursor is None or c.sort_key > cursor]
        batch = pending[:batch_size]

        counts = {
            SweepOutcome.CLEAN.value: 0,
            SweepOutcome.REPAIRED.value: 0,
            SweepOutcome.TRANSIENT_SKIP.value: 0,
            SweepOutcome.ERROR.value: 0,
            # Bug #1415: must be pre-seeded -- counts[outcome.value] += 1
            # below would KeyError on this outcome otherwise.
            SweepOutcome.CAPABILITY_UNAVAILABLE.value: 0,
        }

        for candidate in batch:
            outcome = self._process_one(candidate)
            counts[outcome.value] += 1
            self._state_backend.record_item_processed(candidate.sort_key, outcome.value)

        # Pass is complete when this tick's batch consumed the ENTIRE
        # pending list -- i.e. no candidate remains whose key is greater
        # than the new cursor. `pending` is the untruncated list (before
        # the batch_size slice), so `len(pending) <= batch_size` means
        # everything pending was just processed. Guarded by `candidates`
        # being non-empty (code review finding): an EMPTY fleet (nothing
        # enumerated at all -- no golden repos, no activated repos) has no
        # real sweep work to conclude, so it must never be treated as "a
        # pass just completed" -- that would spuriously churn pass_id and
        # last_full_pass_completed_at on every idle tick.
        if candidates and len(pending) <= batch_size:
            self._state_backend.complete_pass()
            logger.info("HNSWOrphanRepairSweepScheduler: pass complete")

        return {"processed": len(batch), **counts}

    def _process_one(self, candidate: Any) -> SweepOutcome:
        """Fail-soft wrapper: any unexpected exception from the per-item
        processor is loud (logged) but counted as ERROR, never aborting the
        tick (AC2: a failure on one index does not abort the pass).

        Bug #1529: there is no longer a second processor. Every candidate --
        golden, activated, and the fixed-path ``golden_temporal`` shards --
        is repaired IN PLACE by the same ``process_fn``.

        Bug #1542 (Codex-review Q2 follow-up): the REAL ``process_candidate``
        accepts an optional ``activated_repo_manager`` kwarg it uses to
        resolve an activated-repo candidate's ``activation_id`` for correct
        cache-key composition (Story #1458 AC11). Passing that kwarg to an
        arbitrary injected ``process_fn`` (test fakes declared as
        ``Callable[[Any], SweepOutcome]``, e.g. ``spy_process(candidate)``)
        would break the single-argument injection contract those tests
        rely on.

        Dispatch is decided via a STRUCTURAL capability marker
        (``process_candidate.supports_activated_repo_manager``), NOT an
        identity check (``fn is process_candidate``) -- an identity check
        is a silent-failure trap: any legitimate wrapper, ``functools
        .partial``, decorator, or instrumentation layer around
        ``process_candidate`` would fail it and silently fall back to the
        "no activation_id" branch with zero diagnostic, even in real
        production code. A plain function attribute survives
        ``functools.wraps``-based wrapping (its ``WRAPPER_UPDATES`` copies
        ``__dict__``, where this attribute lives), so a wrapper decorated
        with ``@functools.wraps(process_candidate)`` inherits the marker
        automatically and is dispatched exactly like the bare function --
        test fakes that don't opt in (plain functions/lambdas with no such
        attribute) fall through to the plain single-argument call below.
        """
        try:
            if getattr(self._process_fn, "supports_activated_repo_manager", False):
                # `self._process_fn` is declared `Callable[[Any],
                # SweepOutcome]`, which mypy takes literally (no extra
                # kwarg) -- cast is safe here because the runtime check
                # above is the actual contract enforcement, not the static
                # type.
                activation_aware_fn = cast(
                    Callable[..., SweepOutcome], self._process_fn
                )
                return activation_aware_fn(
                    candidate, activated_repo_manager=self._activated_repo_manager
                )
            return self._process_fn(candidate)
        except Exception:
            logger.error(
                "HNSWOrphanRepairSweepScheduler: unexpected error processing %s",
                candidate.sort_key,
                exc_info=True,
            )
            return SweepOutcome.ERROR

    # ------------------------------------------------------------------
    # Admin stats surface (AC4: independent of JobTracker)
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Return the durable cross-pass fleet stats (last full pass time,
        total orphans repaired to date, current cursor position) -- backed
        by the same state_backend as the tick cursor, read independently of
        JobTracker."""
        return self._state_backend.get_state()  # type: ignore[no-any-return]

    def get_liveness(self) -> Dict[str, Any]:
        """Return this PROCESS's sweep liveness snapshot (Bug #1864).

        Complements :meth:`get_stats`, which answers "what has the fleet
        accomplished" from durable shared state. This answers the question
        that had no answer anywhere: "is the thing that advances those
        counters actually alive here, and when did it last do anything".

        Deliberately per-process and never persisted -- a node's loop-thread
        health is not fleet state, and writing it to the shared backend would
        make the last node to write appear to speak for all of them.

        ``tick_in_progress`` is the wedge discriminator: a snapshot showing a
        tick still in flight with an old ``last_tick_at`` is a stuck tick,
        while the same durable row with no tick in flight and a recent
        ``last_tick_at`` is simply a long pass making normal progress.

        O(1), lock-guarded, no I/O -- safe to call on every admin request.
        """
        snapshot = absent_local_scheduler_liveness()
        with self._liveness_lock:
            thread = self._thread
            snapshot.update(
                {
                    "scheduler_running": (
                        thread is not None
                        and thread.is_alive()
                        and not self._stop_event.is_set()
                    ),
                    "started_at": _iso(self._started_at),
                    "last_loop_cycle_at": _iso(self._last_loop_cycle_at),
                    "last_cycle_skipped_reason": self._last_cycle_skipped_reason,
                    "last_tick_at": _iso(self._last_tick_at),
                    "last_tick_completed_at": _iso(self._last_tick_completed_at),
                    "last_tick_error": self._last_tick_error,
                    "ticks_started": self._ticks_started,
                    "ticks_completed": self._ticks_completed,
                    "tick_in_progress": self._ticks_started > self._ticks_completed,
                }
            )
        return snapshot

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _read_cycle_config(self) -> Dict[str, Any]:
        """Read enabled/interval/operating-hours-window from config for one
        loop cycle. Fail-open (Story #1397 gotcha #2): if config cannot be
        read, defaults to enabled=True, the default tick interval, and an
        always-on (0, 0) window -- a transient config-read glitch must
        never silently stop the sweep."""
        try:
            cfg = self._config_service.get_config().hnsw_orphan_repair_sweep_config
            return {
                "enabled": bool(cfg.enabled),
                "interval_minutes": int(cfg.tick_interval_minutes),
                "window_start": int(cfg.operating_hours_start_utc),
                "window_end": int(cfg.operating_hours_end_utc),
            }
        except Exception as exc:
            logger.warning(
                "HNSWOrphanRepairSweepScheduler: failed to read config, "
                "using defaults: %s",
                exc,
            )
            return {
                "enabled": True,
                "interval_minutes": _DEFAULT_TICK_INTERVAL_MINUTES,
                "window_start": _DEFAULT_WINDOW_START_UTC,
                "window_end": _DEFAULT_WINDOW_END_UTC,
            }

    def _loop(self) -> None:
        """Main loop: submit a tick job (if enabled AND within the
        configured UTC operating-hours window), then wait for the
        configured interval, repeat. Re-reads enabled/interval/window from
        config each cycle so Web UI changes take effect without a restart
        (Story #1397)."""
        while not self._stop_event.is_set():
            cycle_cfg = self._read_cycle_config()
            enabled = cycle_cfg["enabled"]
            interval_minutes = cycle_cfg["interval_minutes"]
            current_hour = self._now_fn().hour
            within_window = _is_within_operating_window(
                current_hour, cycle_cfg["window_start"], cycle_cfg["window_end"]
            )

            # Bug #1864: heartbeat every cycle, including the cycles that
            # deliberately submit nothing. Without it, a sweep switched off
            # in the Web UI and a sweep whose loop thread died look the same
            # from outside -- both simply stop producing ticks.
            if enabled and within_window:
                skipped_reason = None
            elif not enabled:
                skipped_reason = CYCLE_SKIPPED_DISABLED
            else:
                skipped_reason = CYCLE_SKIPPED_OUTSIDE_WINDOW
            with self._liveness_lock:
                self._last_loop_cycle_at = self._now_fn()
                self._last_cycle_skipped_reason = skipped_reason

            if enabled and within_window:
                try:
                    self.trigger_now()
                except Exception as exc:
                    logger.error(
                        "HNSWOrphanRepairSweepScheduler: error submitting tick: %s",
                        exc,
                        exc_info=True,
                    )
                wait_seconds = interval_minutes * 60
            else:
                wait_seconds = _DISABLED_POLL_SECONDS

            elapsed = 0
            while elapsed < wait_seconds and not self._stop_event.is_set():
                self._stop_event.wait(timeout=_TICK_SECONDS)
                elapsed += _TICK_SECONDS
