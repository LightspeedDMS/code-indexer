"""Memory-Pressure-Aware Index-Cache Governor (Story #1213 Story 1).

Provides a node-level memory pressure signal and a band state machine
(GREEN / YELLOW / RED) with hysteresis and RED min-dwell.  A single sampler
thread per process recomputes the band on a configurable interval; the query
path reads the current band atomically (never polls psutil/cgroup directly).

FAIL-SAFE CONTRACT (Anti-Fallback §3.2):
  - band == RED before the first successful sample
  - band reverts to RED on any reader exception
  Never defaults to GREEN/YELLOW on error.

Story 1 scope: governor is BUILT but consulted by nothing yet (no behavior change).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentinel threshold for cgroup v1 "no limit"
# Kernel uses (uint64_t)-1 >> 1 or PAGE_COUNTER_MAX which is >= 2^62 on 64-bit.
# ---------------------------------------------------------------------------
_CGROUP_V1_UNLIMITED_THRESHOLD = 2**62


class MemoryBand(Enum):
    """Memory pressure band — ordered by severity."""

    GREEN = "GREEN"  # used_pct < yellow  → retain shards, no eviction
    YELLOW = "YELLOW"  # yellow <= used_pct < red → proactive LRU eviction
    RED = "RED"  # used_pct >= red OR swap-in activity → evict-after-use


@dataclass
class MemorySample:
    """A single observation from the memory signal layer."""

    basis: str  # "cgroup_v2" | "cgroup_v1" | "host"
    used_pct: float
    effective_limit: int  # bytes
    effective_used: int  # bytes
    pswpin_rate: int  # swap-IN pages per sample interval (delta, not absolute)


@dataclass
class GovernorCounters:
    """Transition and action counters exposed by get_stats().

    Story 1 initialises all to zero; Story 3 increments action counters.
    """

    # Band-transition counters
    green_to_yellow: int = 0
    yellow_to_red: int = 0
    red_to_yellow: int = 0
    yellow_to_green: int = 0

    # Action counters (wired in Story 3)
    shards_evicted_after_use: int = 0
    lru_evictions: int = 0
    trim_calls: int = 0


class _MemoryReaders:
    """Default readers that call real cgroup/psutil/proc paths."""

    def read_cgroup_v2_max(self) -> str:
        with open("/sys/fs/cgroup/memory.max") as f:
            return f.read().strip()

    def read_cgroup_v2_current(self) -> int:
        with open("/sys/fs/cgroup/memory.current") as f:
            return int(f.read().strip())

    def read_cgroup_v1_limit(self) -> int:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
            return int(f.read().strip())

    def read_cgroup_v1_usage(self) -> int:
        with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as f:
            return int(f.read().strip())

    def read_host_memory(self) -> Any:
        import psutil

        return psutil.virtual_memory()

    def read_pswpin(self) -> int:
        with open("/proc/vmstat") as f:
            for line in f:
                if line.startswith("pswpin "):
                    return int(line.split()[1])
        return 0


class MemoryGovernor:
    """Node-level memory pressure governor with hysteresis and dwell.

    Constructor args:
        readers:               injectable reader object (default: real cgroup/psutil)
        enabled:               False => should_evict_after_shard() always True (safe)
        start_sampler:         True => start() called in __init__
        yellow_pct:            entry threshold for YELLOW band (default 70.0)
        red_pct:               entry threshold for RED band (default 85.0)
        hysteresis_pct:        gap subtracted from entry thresholds for exit (default 10.0)
        red_min_dwell_seconds: minimum seconds to remain in RED before exit (default 30)
        sample_interval_seconds: sampler thread sleep between samples (default 2.0)
        swap_forces_red:       True => positive pswpin delta forces RED (default True)
        rss_inflation_factor:  multiplier for LRU-cap inflation helper (default 2.0)
        time_fn:               injectable monotonic clock (default time.monotonic)
    """

    def __init__(
        self,
        *,
        readers: Any = None,
        enabled: bool = True,
        start_sampler: bool = False,
        yellow_pct: float = 70.0,
        red_pct: float = 85.0,
        hysteresis_pct: float = 10.0,
        red_min_dwell_seconds: float = 30.0,
        sample_interval_seconds: float = 2.0,
        swap_forces_red: bool = True,
        rss_inflation_factor: float = 2.0,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._readers: Any = readers if readers is not None else _MemoryReaders()
        self._enabled = enabled
        self._yellow_pct = yellow_pct
        self._red_pct = red_pct
        self._hysteresis_pct = hysteresis_pct
        self._red_min_dwell_seconds = float(red_min_dwell_seconds)
        self._sample_interval_seconds = sample_interval_seconds
        self._swap_forces_red = swap_forces_red
        self._rss_inflation_factor = rss_inflation_factor
        self._time_fn: Callable[[], float] = (
            time_fn if time_fn is not None else time.monotonic
        )

        # Band state — fail-safe RED before first successful sample.
        # _red_entry_time is initialised to current time so that the dwell
        # check can expire normally even on the pre-init RED band.  Tests with
        # red_min_dwell_seconds=0 can exit RED on the very first sample.
        self._band: MemoryBand = MemoryBand.RED
        self._band_lock = threading.Lock()
        self._red_entry_time: Optional[float] = self._time_fn()

        # Counters
        self.counters = GovernorCounters()

        # pswpin baseline for delta computation (None until first sample)
        self._prev_pswpin: Optional[int] = None

        # On the very first tick we cascade to the correct band WITHOUT
        # incrementing transition counters (pre-init convergence, not a
        # real operational transition).  Cleared to False in _tick() after
        # the first successful sample is processed.
        self._first_tick: bool = True

        # cgroup detection cached after first successful _detect_basis()
        self._cached_basis: Optional[str] = None

        # Sampler thread
        self._stop_event = threading.Event()
        self._sampler_thread: Optional[threading.Thread] = None

        if start_sampler:
            self.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def band(self) -> MemoryBand:
        with self._band_lock:
            return self._band

    @property
    def rss_inflation_factor(self) -> float:
        return self._rss_inflation_factor

    def should_evict_after_shard(self) -> bool:
        """True iff the governor says evict-after-use is required.

        Returns True when:
        - governor disabled (enabled=False)
        - band is RED
        - pre-first-sample (band starts at RED by fail-safe contract)
        """
        if not self._enabled:
            return True
        return self.band == MemoryBand.RED

    def get_stats(self) -> dict:
        """Return a snapshot dict for the admin endpoint (Story 4)."""
        with self._band_lock:
            band = self._band
        return {
            "band": band.value,
            "used_pct": getattr(self, "_last_used_pct", 0.0),
            "effective_limit_mb": getattr(self, "_last_effective_limit", 0)
            // (1024 * 1024),
            "effective_used_mb": getattr(self, "_last_effective_used", 0)
            // (1024 * 1024),
            "basis": getattr(self, "_last_basis", "unknown"),
            "pswpin_rate": getattr(self, "_last_pswpin_rate", 0),
            "enabled": self._enabled,
            "counters": {
                "green_to_yellow": self.counters.green_to_yellow,
                "yellow_to_red": self.counters.yellow_to_red,
                "red_to_yellow": self.counters.red_to_yellow,
                "yellow_to_green": self.counters.yellow_to_green,
                "shards_evicted_after_use": self.counters.shards_evicted_after_use,
                "lru_evictions": self.counters.lru_evictions,
                "trim_calls": self.counters.trim_calls,
            },
            "pid": os.getpid(),
        }

    def start(self) -> None:
        """Start the sampler thread if not already running."""
        with self._band_lock:
            if self._sampler_thread is not None and self._sampler_thread.is_alive():
                return  # idempotent
            self._stop_event.clear()
            self._sampler_thread = threading.Thread(
                target=self._sampler_loop,
                name="memory-governor-sampler",
                daemon=True,
            )
            self._sampler_thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the sampler thread and join within `timeout` seconds."""
        self._stop_event.set()
        thread = None
        with self._band_lock:
            thread = self._sampler_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._band_lock:
            self._sampler_thread = None

    def is_running(self) -> bool:
        """True iff the sampler thread is alive."""
        with self._band_lock:
            return self._sampler_thread is not None and self._sampler_thread.is_alive()

    # ------------------------------------------------------------------
    # Internal — signal computation
    # ------------------------------------------------------------------

    def _compute_sample(self) -> MemorySample:
        """Read memory state and return a MemorySample.

        Detection order: cgroup v2 => cgroup v1 => host.
        Only FileNotFoundError/ValueError are expected on hosts without cgroup
        namespaces; any other OSError is logged as a warning before falling through.
        pswpin read errors propagate to the caller so _tick() can apply fail-safe.
        """
        host_vm = self._readers.read_host_memory()
        host_total: int = host_vm.total
        host_used: int = host_vm.used

        cgroup_limit: Optional[int] = None
        cgroup_used: Optional[int] = None
        basis: str = "host"

        # --- Try cgroup v2 ---
        try:
            v2_max_str = self._readers.read_cgroup_v2_max()
            if v2_max_str.strip().lower() != "max":
                cgroup_v2_limit = int(v2_max_str.strip())
                cgroup_v2_current = self._readers.read_cgroup_v2_current()
                cgroup_limit = cgroup_v2_limit
                cgroup_used = cgroup_v2_current
                basis = "cgroup_v2"
        except (FileNotFoundError, ValueError):
            # cgroup v2 not available on this host — expected, fall through
            pass
        except OSError as exc:
            # Unexpected I/O error reading cgroup v2; log and fall through
            logger.warning("GOV cgroup v2 read error, falling back: %s", exc)

        # --- Try cgroup v1 (only if v2 not found) ---
        if cgroup_limit is None:
            try:
                v1_limit = self._readers.read_cgroup_v1_limit()
                if v1_limit < _CGROUP_V1_UNLIMITED_THRESHOLD:
                    cgroup_v1_usage = self._readers.read_cgroup_v1_usage()
                    cgroup_limit = v1_limit
                    cgroup_used = cgroup_v1_usage
                    basis = "cgroup_v1"
            except (FileNotFoundError, ValueError):
                # cgroup v1 not available on this host — expected, fall through
                pass
            except OSError as exc:
                # Unexpected I/O error reading cgroup v1; log and fall through
                logger.warning("GOV cgroup v1 read error, falling back: %s", exc)

        # --- Compute effective limit and used ---
        if cgroup_limit is not None and cgroup_used is not None:
            effective_limit = min(host_total, cgroup_limit)
            effective_used = cgroup_used
        else:
            effective_limit = host_total
            effective_used = host_used
            basis = "host"

        used_pct = (
            100.0 * effective_used / effective_limit if effective_limit > 0 else 0.0
        )

        # --- pswpin delta: propagates exceptions to caller for fail-safe ---
        pswpin_now = self._readers.read_pswpin()

        if self._prev_pswpin is None:
            pswpin_rate = 0
        else:
            pswpin_rate = max(0, pswpin_now - self._prev_pswpin)
        self._prev_pswpin = pswpin_now

        return MemorySample(
            basis=basis,
            used_pct=used_pct,
            effective_limit=effective_limit,
            effective_used=effective_used,
            pswpin_rate=pswpin_rate,
        )

    def _tick(self) -> None:
        """Compute one sample and advance the band state machine.

        On any exception from the readers, applies the fail-safe (band = RED).
        Called by the sampler thread; also callable directly in tests.
        """
        first_tick = self._first_tick
        try:
            sample = self._compute_sample()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "GOV memory reader error — fail-safe RED: %s", exc, exc_info=True
            )
            self._apply_fail_safe_red()
            return

        # Cache last sample for get_stats()
        self._last_used_pct = sample.used_pct
        self._last_effective_limit = sample.effective_limit
        self._last_effective_used = sample.effective_used
        self._last_basis = sample.basis
        self._last_pswpin_rate = sample.pswpin_rate

        self._advance_band(sample, first_tick=first_tick)
        # Clear first-tick flag after successful processing so subsequent
        # ticks accumulate real operational transition counters.
        self._first_tick = False

    def _advance_band(self, sample: MemorySample, *, first_tick: bool = False) -> None:
        """Advance the band state machine based on the current sample.

        Cascades transitions within a single tick so that the final band
        reflects the current sample without requiring multiple _tick() calls.
        Example: RED (50%) -> YELLOW -> GREEN all in one tick when dwell=0.

        No direct RED<->GREEN edges exist; YELLOW is always visited.

        first_tick: when True, band state is updated but transition counters
            are NOT incremented (pre-init convergence from fail-safe RED).
        """
        swap_forces_red = self._swap_forces_red and sample.pswpin_rate > 0
        used_pct = sample.used_pct
        now = self._time_fn()
        yellow_exit = self._yellow_pct - self._hysteresis_pct
        red_exit = self._red_pct - self._hysteresis_pct

        with self._band_lock:
            # --- RED state ---
            if self._band == MemoryBand.RED:
                dwell_ok = self._red_min_dwell_seconds <= 0.0 or (
                    self._red_entry_time is not None
                    and (now - self._red_entry_time) >= self._red_min_dwell_seconds
                )
                if used_pct < red_exit and not swap_forces_red and dwell_ok:
                    self._band = MemoryBand.YELLOW
                    if not first_tick:
                        self.counters.red_to_yellow += 1
                    logger.debug("GOV-001 band RED->YELLOW used_pct=%.1f", used_pct)
                    # Cascade: check whether YELLOW should also exit to GREEN
                    if used_pct < yellow_exit:
                        self._band = MemoryBand.GREEN
                        if not first_tick:
                            self.counters.yellow_to_green += 1
                        logger.debug(
                            "GOV-001 band YELLOW->GREEN used_pct=%.1f (cascade from RED)",
                            used_pct,
                        )
                # No other RED transitions in a single tick

            # --- YELLOW state ---
            elif self._band == MemoryBand.YELLOW:
                if used_pct >= self._red_pct or swap_forces_red:
                    self._band = MemoryBand.RED
                    self._red_entry_time = now
                    if not first_tick:
                        self.counters.yellow_to_red += 1
                    logger.debug(
                        "GOV-001 band YELLOW->RED used_pct=%.1f swap=%s",
                        used_pct,
                        swap_forces_red,
                    )
                elif used_pct < yellow_exit:
                    self._band = MemoryBand.GREEN
                    if not first_tick:
                        self.counters.yellow_to_green += 1
                    logger.debug("GOV-001 band YELLOW->GREEN used_pct=%.1f", used_pct)

            # --- GREEN state ---
            elif self._band == MemoryBand.GREEN:
                if used_pct >= self._red_pct or swap_forces_red:
                    # No direct GREEN->RED; step through YELLOW first
                    self._band = MemoryBand.YELLOW
                    if not first_tick:
                        self.counters.green_to_yellow += 1
                    logger.debug(
                        "GOV-001 band GREEN->YELLOW used_pct=%.1f swap=%s",
                        used_pct,
                        swap_forces_red,
                    )
                    # Cascade: check whether YELLOW should immediately enter RED
                    if used_pct >= self._red_pct or swap_forces_red:
                        self._band = MemoryBand.RED
                        self._red_entry_time = now
                        if not first_tick:
                            self.counters.yellow_to_red += 1
                        logger.debug(
                            "GOV-001 band YELLOW->RED used_pct=%.1f (cascade from GREEN)",
                            used_pct,
                        )
                elif used_pct >= self._yellow_pct:
                    self._band = MemoryBand.YELLOW
                    if not first_tick:
                        self.counters.green_to_yellow += 1
                    logger.debug("GOV-001 band GREEN->YELLOW used_pct=%.1f", used_pct)

    def _apply_fail_safe_red(self) -> None:
        """Force band to RED (fail-safe on reader error)."""
        with self._band_lock:
            if self._band != MemoryBand.RED:
                logger.warning(
                    "GOV memory reader error — reverting band to RED (fail-safe)"
                )
            self._band = MemoryBand.RED
            self._red_entry_time = self._time_fn()

    def _sampler_loop(self) -> None:
        """Background thread: sample memory every sample_interval_seconds."""
        while not self._stop_event.is_set():
            self._tick()
            self._stop_event.wait(timeout=self._sample_interval_seconds)


# ---------------------------------------------------------------------------
# Process-level singleton — None until server lifespan builds it
# ---------------------------------------------------------------------------

_governor: Optional[MemoryGovernor] = None
_governor_lock = threading.Lock()


def get_memory_governor() -> Optional[MemoryGovernor]:
    """Return the process-level governor, or None (CLI/pre-init case)."""
    with _governor_lock:
        return _governor


def set_memory_governor(governor: MemoryGovernor) -> None:
    """Install the process-level governor (called once in server service_init)."""
    global _governor
    with _governor_lock:
        _governor = governor


def clear_memory_governor() -> None:
    """Clear the process-level governor (lifespan shutdown / test isolation)."""
    global _governor
    with _governor_lock:
        _governor = None


def build_memory_governor(
    *,
    enabled: bool = True,
    yellow_pct: float = 70.0,
    red_pct: float = 85.0,
    hysteresis_pct: float = 10.0,
    red_min_dwell_seconds: float = 30.0,
    sample_interval_seconds: float = 2.0,
    swap_forces_red: bool = True,
    rss_inflation_factor: float = 2.0,
) -> MemoryGovernor:
    """Build and return a MemoryGovernor with default production readers.

    Called from service_init.py after initialize_caches(). The returned
    governor is NOT started here — the caller must call start() and register
    stop() in the lifespan shutdown hook.
    """
    gov = MemoryGovernor(
        enabled=enabled,
        start_sampler=False,
        yellow_pct=yellow_pct,
        red_pct=red_pct,
        hysteresis_pct=hysteresis_pct,
        red_min_dwell_seconds=red_min_dwell_seconds,
        sample_interval_seconds=sample_interval_seconds,
        swap_forces_red=swap_forces_red,
        rss_inflation_factor=rss_inflation_factor,
    )
    return gov
