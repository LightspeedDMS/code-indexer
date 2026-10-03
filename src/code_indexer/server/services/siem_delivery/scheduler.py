"""SIEM delivery scheduler: a daemon loop in EVERY server process.

Each cycle reads the COMMITTED SIEM configuration (never ``get_config()``),
applies the version fence and the atomic arming statement, publishes this
process's capture snapshot, keeps its readiness row fresh, probes its
credentials, refreshes the fleet stats (one process per interval), and
submits ONE single-flight tick job when due work exists and this process can
mint tokens.  Waits use ``stop_event.wait`` -- never ``time.sleep``.

Liveness, last-known-good configuration and the probe result are per-process
telemetry, not shared state; delivery state lives only in the database.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from code_indexer.server.services.siem_delivery import capture, state_store, stats
from code_indexer.server.services.siem_delivery.capture import CaptureSnapshot
from code_indexer.server.services.siem_delivery.claim import EngineContext
from code_indexer.server.services.siem_delivery.credential import SiemCredentialStore
from code_indexer.server.services.siem_delivery.db import SiemDb
from code_indexer.server.services.siem_delivery.destination import (
    Destination,
    resolve_destination,
)
from code_indexer.server.services.siem_delivery.probe import (
    probe_due,
    requeue_after_mapping_change,
    run_tick,
)
from code_indexer.server.services.siem_delivery.sender import (
    CredentialProvider,
    ProbeResult,
)
from code_indexer.server.services.siem_delivery.timings import (
    SiemTimings,
    timings_for,
)
from code_indexer.server.services.siem_delivery.udm import MAPPING_VERSION, UDM_MAPPING
from code_indexer.server.utils.siem_delivery_config import SiemDeliveryConfig

logger = logging.getLogger(__name__)

SECTION_ATTR = "siem_delivery_config"
_STOP_JOIN_SECONDS = 10.0
_STALL_CYCLES = 3


@dataclass(frozen=True)
class CycleView:
    """One applied committed configuration."""

    version: int
    section: SiemDeliveryConfig
    destination: Optional[Destination]


@dataclass
class _Liveness:
    started_at: Optional[datetime] = None
    last_loop_cycle_at: Optional[datetime] = None
    last_loop_cycle_monotonic: Optional[float] = None
    last_cycle_error: Optional[str] = None
    last_tick_at: Optional[datetime] = None
    last_tick_error: Optional[str] = None
    ticks_started: int = 0
    ticks_completed: int = 0
    config_error: Optional[str] = None
    config_error_since: Optional[datetime] = None
    lkg_at: Optional[datetime] = None
    probe_result: ProbeResult = ProbeResult.PENDING
    probed_monotonic: Optional[float] = None
    last_state: Dict[str, Any] = field(default_factory=dict)
    processes: List[Dict[str, Any]] = field(default_factory=list)
    due_work: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


# A key-file path field saved by an earlier release.  It is IGNORED: the key
# is configured only through the Web UI credential (stored encrypted); the
# file it names is never read.
LEGACY_KEY_PATH_FIELD = "service_account_key_path"


def section_from_raw(raw: Mapping[str, Any]) -> SiemDeliveryConfig:
    allowed = {f.name for f in fields(SiemDeliveryConfig)}
    return SiemDeliveryConfig(**{k: v for k, v in raw.items() if k in allowed})


class SiemDeliveryScheduler:
    OPERATION_TYPE = "siem_delivery_tick"
    REPO_ALIAS_SENTINEL = "siem-delivery"

    def __init__(
        self,
        *,
        db: SiemDb,
        config_service: Any,
        background_job_manager: Any,
        http_client_factory: Any,
        harness_active: bool,
        node_id: Optional[str],
        credential_store: SiemCredentialStore,
        timings: Optional[SiemTimings] = None,
        mapping: Optional[Mapping[str, str]] = None,
        mapping_version: int = MAPPING_VERSION,
    ) -> None:
        self.db = db
        self._config_service = config_service
        self._bgm = background_job_manager
        self._http_factory = http_client_factory
        self.harness_active = harness_active
        self.node_id = node_id or "solo"
        self.process_id = f"{self.node_id}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self.timings = timings or timings_for(harness_active)
        self.mapping = dict(mapping or UDM_MAPPING)
        self.mapping_version = mapping_version
        self.credential_store = credential_store
        self.credentials = CredentialProvider(
            http_client_factory,
            credential_store.load,
            token_timeout=self.timings.token_timeout_seconds,
        )
        # Non-secret identity of the stored key, as of this process's last
        # cycle (or its own last change): the config page reads it, no I/O.
        self._credential_identity: Optional[Dict[str, Any]] = None
        self._legacy_key_path_warned = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._live = _Liveness()
        self._view: Optional[CycleView] = None
        self._lkg: Optional[CycleView] = None
        self._registered_dest: Optional[str] = None
        # held across the registration DB write so a deregistration and a
        # still-running loop's refresh can never interleave
        self._registration_lock = threading.Lock()
        self._deregistered = False
        self.registered = False
        from code_indexer.server.services.siem_delivery.health import AlertSignals

        self.alerts = AlertSignals(self.timings)

    # --- lifecycle ---------------------------------------------------------

    def register_process(self) -> None:
        """The registration barrier (sync DB I/O; lifespan offloads it)."""
        with self._registration_lock:
            state_store.register_process(
                self.db,
                self.process_id,
                node_id=self.node_id,
                ttl_seconds=self.timings.process_ttl_seconds,
            )
            self._deregistered = False
            self.registered = True

    def start(self) -> None:
        """Start the daemon thread only (no I/O here)."""
        if not self.registered:
            raise RuntimeError("register_process() must complete before start()")
        self._stop_event.clear()
        with self._lock:
            self._live.started_at = _now()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="SiemDeliveryScheduler"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=_STOP_JOIN_SECONDS)

    def deregister_process(self) -> None:
        """Remove this process's row for good: a loop that outlived stop()'s
        bounded join can no longer refresh (re-insert) it."""
        with self._registration_lock:
            self._deregistered = True
            state_store.deregister_process(self.db, self.process_id)

    def _refresh_registration(self) -> None:
        with self._registration_lock:
            if self._deregistered:
                return
            state_store.refresh_process(
                self.db,
                self.process_id,
                node_id=self.node_id,
                ttl_seconds=self.timings.process_ttl_seconds,
                refresh_when_remaining=self.timings.process_refresh_when_remaining_seconds,
            )

    # --- loop --------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            busy = False
            try:
                busy = self.run_cycle()
                error = None
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                error = type(exc).__name__
                logger.error(
                    "SIEM delivery loop cycle failed: %s", error, exc_info=True
                )
            with self._lock:
                self._live.last_cycle_error = error
            wait = (
                self.timings.cycle_busy_seconds
                if busy
                else self.timings.cycle_idle_seconds
            )
            self._stop_event.wait(timeout=wait)

    def _warn_legacy_key_path(self, raw: Mapping[str, Any]) -> None:
        """One WARNING per process when the committed section still carries
        the removed key-file path (its value is never logged or read)."""
        if not raw.get(LEGACY_KEY_PATH_FIELD) or self._legacy_key_path_warned:
            return
        self._legacy_key_path_warned = True
        logger.warning(
            "SIEM delivery: the stored siem_delivery.%s setting is IGNORED (the "
            "key file is never read); upload the service-account key in the "
            "Web UI SIEM Delivery section. Until then the destination has no "
            "credential.",
            LEGACY_KEY_PATH_FIELD,
        )

    def _committed_view_parts(self) -> Any:
        version, raw = self._config_service.read_committed_section(SECTION_ATTR)
        self._warn_legacy_key_path(raw)
        return version, section_from_raw(raw)

    def _read_cycle_config(self) -> Optional[CycleView]:
        try:
            version, section = self._committed_view_parts()
            destination = resolve_destination(
                section, harness_active=self.harness_active
            )
        except Exception as exc:  # noqa: BLE001 - keep the last-known-good config
            with self._lock:
                if self._live.config_error is None:
                    self._live.config_error_since = _now()
                self._live.config_error = type(exc).__name__
            return self._lkg
        view = CycleView(version, section, destination)
        with self._lock:
            self._live.config_error = None
            self._live.config_error_since = None
            self._live.lkg_at = _now()
        self._lkg = view
        return view

    def run_cycle(self) -> bool:
        """One loop cycle; True when due work was found (busy cadence)."""
        started = capture.monotonic_now()
        view = self._read_cycle_config()
        with self._lock:
            self._live.last_loop_cycle_at = _now()
            self._live.last_loop_cycle_monotonic = time.monotonic()
        if view is None:
            capture.publish_capture_state(capture.NOT_LOADED)
            return False
        self._view = view
        dest = view.destination
        if dest is not None and dest.key != self._registered_dest:
            state_store.upsert_destination(self.db, dest.key, view.section)
            self._registered_dest = dest.key
        state = state_store.fence_and_arm(
            self.db,
            version=view.version,
            enabled=view.section.enabled,
            destination_key=dest.key if dest else None,
            mapping_version=self.mapping_version,
            config_epoch=view.section.arming_epoch,
            probe_fresh_seconds=self.timings.probe_fresh_seconds,
        )
        active = state_store.capture_active(
            state,
            version=view.version,
            enabled=view.section.enabled,
            destination_key=dest.key if dest else None,
        )
        capture.publish_capture_state(
            CaptureSnapshot(True, active, dest.key if dest else None, started)
        )
        self._refresh_registration()
        self.note_credential_identity(self.credential_store.identity())
        self._maybe_probe_credentials(dest)
        stats.maybe_refresh_stats(
            self.db,
            refresh_seconds=self.timings.stats_refresh_seconds,
            destination_key=dest.key if dest else None,
        )
        due, now = self.db.read(
            lambda tx: (stats.due_work(tx, dest.key if dest else None), tx.now())
        )
        state = state_store.read_state(self.db)
        processes = state_store.live_processes(self.db)
        ctx = self.engine_context()
        halted_probe = ctx is not None and probe_due(ctx, state, now)
        # A release with a newer mapping requeues rows older code quarantined;
        # that must not wait for unrelated due work (all may be quarantined).
        requeue_due = (
            int(state.get("requeued_mapping_version") or 0) < self.mapping_version
        )
        with self._lock:
            self._live.last_state = state
            self._live.processes = processes
            self._live.due_work = due
            probe_ok = self._live.probe_result is ProbeResult.OK
        self.alerts.evaluate(self.health_inputs())
        if dest is not None and (due or halted_probe or requeue_due) and probe_ok:
            self.trigger_now()
            return bool(due)
        return False

    def _maybe_probe_credentials(self, dest: Optional[Destination]) -> None:
        if dest is None:
            return
        with self._lock:
            last = self._live.probed_monotonic
            previous = self._live.probe_result
        due = (
            last is None
            or previous is not ProbeResult.OK
            or time.monotonic() - last >= self.timings.credential_probe_every_seconds
        )
        if not due:
            return
        result = self.credentials.probe(dest)
        state_store.record_probe(
            self.db, self.process_id, destination_key=dest.key, result=result.value
        )
        if result is not ProbeResult.OK and result is not previous:
            logger.warning(
                "SIEM delivery: this process cannot mint SecOps tokens (%s)",
                result.value,
            )
        with self._lock:
            self._live.probe_result = result
            self._live.probed_monotonic = time.monotonic()

    # --- tick ----------------------------------------------------------------

    def committed_view(self) -> CycleView:
        """The committed configuration read NOW (one committed read), never
        this process's last cycle.  Raises on an unreadable/invalid config."""
        version, section = self._committed_view_parts()
        destination = resolve_destination(section, harness_active=self.harness_active)
        return CycleView(version, section, destination)

    def committed_context(self) -> Optional[EngineContext]:
        """An engine context from the committed configuration read NOW
        (admin actions must see a destination saved a moment ago, not wait
        for the loop's next cycle).  Raises on an unreadable/invalid config."""
        return self.engine_context(self.committed_view())

    def engine_context(
        self, view: Optional[CycleView] = None
    ) -> Optional[EngineContext]:
        view = view if view is not None else self._view
        if view is None or view.destination is None:
            return None
        return EngineContext(
            db=self.db,
            timings=self.timings,
            http_factory=self._http_factory,
            credentials=self.credentials,
            process_id=self.process_id,
            destination=view.destination,
            max_batch_events=view.section.max_batch_events,
            source_instance_label=view.section.source_instance_label,
            config_epoch=view.section.arming_epoch,
            mapping=self.mapping,
            mapping_version=self.mapping_version,
        )

    def trigger_now(self) -> Optional[str]:
        from code_indexer.server.jobs.exceptions import MaintenanceModeError
        from code_indexer.server.repositories.background_jobs import DuplicateJobError

        try:
            job_id: str = self._bgm.submit_job(
                self.OPERATION_TYPE,
                self._run_tick,
                submitter_username="system",
                is_admin=True,
                repo_alias=self.REPO_ALIAS_SENTINEL,
            )
        except (DuplicateJobError, MaintenanceModeError):
            return None  # another process owns the tick, or maintenance
        return job_id

    def _run_tick(self) -> Dict[str, Any]:
        with self._lock:
            self._live.ticks_started += 1
            self._live.last_tick_at = _now()
        try:
            result = self._run_tick_impl()
        except Exception as exc:
            with self._lock:
                self._live.ticks_completed += 1
                self._live.last_tick_error = type(exc).__name__
            raise
        with self._lock:
            self._live.ticks_completed += 1
            self._live.last_tick_error = None
        return result

    def _run_tick_impl(self) -> Dict[str, Any]:
        ctx = self.engine_context()
        if ctx is None:
            return {"skipped": "no_destination"}
        count = requeue_after_mapping_change(ctx)  # one state read when done
        if count:
            from code_indexer.server.services.siem_delivery.admin import (
                record_requeue_event,
            )

            record_requeue_event(self, count, trigger="mapping_version_change")
        return run_tick(ctx)

    # --- views ---------------------------------------------------------------

    @property
    def view(self) -> Optional[CycleView]:
        return self._view

    def note_credential_identity(self, identity: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self._credential_identity = dict(identity) if identity else None

    @property
    def credential_identity(self) -> Optional[Dict[str, Any]]:
        """The stored key's non-secret identity as this process last saw it."""
        with self._lock:
            ident = self._credential_identity
            return dict(ident) if ident else None

    def health_inputs(self) -> Dict[str, Any]:
        with self._lock:
            live = self._live
            return {
                "view": self._view,
                "config_error": live.config_error,
                "config_error_since": live.config_error_since,
                "lkg_at": live.lkg_at,
                "last_loop_cycle_monotonic": live.last_loop_cycle_monotonic,
                "state": dict(live.last_state),
                "processes": list(live.processes),
                "probe_result": live.probe_result,
                "process_id": self.process_id,
                "mapping_version": self.mapping_version,
                "capture_failures": capture.capture_failures_since_boot(),
                "timings": self.timings,
            }

    def get_liveness(self) -> Dict[str, Any]:
        with self._lock:
            live = self._live
            thread = self._thread
            return {
                "scope": "this_process",
                "process_id": self.process_id,
                "node_id": self.node_id,
                "pid": os.getpid(),
                "scheduler_running": thread is not None
                and thread.is_alive()
                and not self._stop_event.is_set(),
                "started_at": _iso(live.started_at),
                "last_loop_cycle_at": _iso(live.last_loop_cycle_at),
                "last_cycle_error": live.last_cycle_error,
                "last_tick_at": _iso(live.last_tick_at),
                "last_tick_error": live.last_tick_error,
                "ticks_started": live.ticks_started,
                "ticks_completed": live.ticks_completed,
                "probe_result": live.probe_result.value,
                "config_lkg_status": (
                    "ok"
                    if live.config_error is None and self._view is not None
                    else ("never_loaded" if self._lkg is None else "last_known_good")
                ),
                "config_error": live.config_error,
                "harness_mode": self.harness_active,
                "capture_failures_since_boot": capture.capture_failures_since_boot(),
                "capture_skipped_snapshot_expired": capture.capture_skipped_snapshot_expired(),
            }
