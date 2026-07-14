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

# Minimum seconds between successive GOV-002 log entries (rate-limit guard).
_GOV002_MIN_INTERVAL_SECONDS = 5.0

# Floor entry count for YELLOW proactive LRU eviction: retain at least this
# many (hottest) HNSW entries so repeated queries stay warm.
_YELLOW_LRU_FLOOR = 1

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
        enabled:               Fallback when no config_service is supplied.
                               Ignored when config_service is set (live read wins).
        start_sampler:         True => start() called in __init__
        yellow_pct:            Fallback threshold for YELLOW band (default 70.0).
                               Ignored when config_service is set (live read wins).
        red_pct:               Fallback threshold for RED band (default 85.0).
        hysteresis_pct:        Fallback hysteresis gap (default 10.0).
        red_min_dwell_seconds: minimum seconds to remain in RED before exit (default 30)
        sample_interval_seconds: sampler thread sleep between samples (default 2.0)
        swap_forces_red:               Fallback swap-in override flag (default True).
        swap_pswpin_red_threshold:     Minimum swap-in rate (pages/interval) required
                                       to force RED via the swap_forces_red path.
                                       Default 100: above idle OS noise (1-3) but
                                       well below a death-spiral (observed 3630).
        rss_inflation_factor:          multiplier for LRU-cap inflation helper (default 2.0)
        config_service:        Optional live-config provider.  When set, yellow_pct,
                               red_pct, hysteresis_pct, swap_forces_red, enabled, and
                               rss_inflation_factor are all read LIVE from
                               config_service.get_config().cache_config on every
                               _tick().  A read failure applies fail-safe RED.
                               (Story #1213 Story 2 — hot-reload support)
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
        swap_pswpin_red_threshold: int = 100,
        rss_inflation_factor: float = 2.0,
        config_service: Any = None,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._readers: Any = readers if readers is not None else _MemoryReaders()
        # Fallback values used when no config_service is set
        self._enabled = enabled
        self._yellow_pct = yellow_pct
        self._red_pct = red_pct
        self._hysteresis_pct = hysteresis_pct
        self._red_min_dwell_seconds = float(red_min_dwell_seconds)
        self._sample_interval_seconds = sample_interval_seconds
        self._swap_forces_red = swap_forces_red
        self._swap_pswpin_red_threshold = swap_pswpin_red_threshold
        self._rss_inflation_factor = rss_inflation_factor
        # Live config provider (Story #1213 Story 2): when set, watermarks are
        # read from config_service.get_config().cache_config on every _tick().
        self._config_service: Any = config_service
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

        # Rate-limiting state for GOV-002 log entries
        self._gov002_last_log_time: float = 0.0

        # Cache reference for YELLOW proactive LRU eviction (Story 4).
        # Set via attach_cache(); None until service_init wires it.
        self._attached_cache: Optional[Any] = None

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

    @property
    def last_used_pct(self) -> float:
        """Most-recently sampled used_pct (0.0 before the first tick)."""
        return getattr(self, "_last_used_pct", 0.0)

    def attach_cache(self, cache: Any) -> None:
        """Attach a cache for YELLOW proactive LRU eviction (Story 4).

        Called from service_init after initialize_caches() so the sampler
        can call evict_lru_to_floor() on each YELLOW tick.  None-safe: if
        never called the YELLOW eviction path is skipped silently.
        """
        self._attached_cache = cache

    # ------------------------------------------------------------------
    # Live config helper (Story #1213 Story 2)
    # ------------------------------------------------------------------

    def _read_live_config(self) -> Optional[Any]:
        """Return cache_config from config_service, or None on any failure.

        Returns None when:
        - No config_service is set (CLI/pre-init path — use constructor defaults).
        - config_service.get_config() raises (fail-safe: callers must apply RED).

        Never raises.
        """
        if self._config_service is None:
            return None
        try:
            cfg = self._config_service.get_config()
            return cfg.cache_config
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "GOV config read failure — fail-safe RED will be applied: %s",
                exc,
            )
            return None

    def maybe_trim(self) -> None:
        """Best-effort malloc_trim after a cache eviction.

        Called from the temporal dispatch finally-block when the governor has
        instructed an evict-after-use.  Increments counters.trim_calls regardless
        of whether the trim actually freed pages (malloc_trim return value is
        advisory only — never assert it lowered RSS per design §5).

        Emits GOV-004 on every call (Story 4): released=True when malloc_trim
        reported freed pages, False on non-linux, call failure, or trim returned 0.

        Never raises: any OS/ctypes error is logged at WARNING and swallowed so
        an eviction call-site is never interrupted by a trim failure.
        """
        released = False
        try:
            import ctypes
            import sys

            if sys.platform == "linux":
                try:
                    libc = ctypes.CDLL("libc.so.6", use_errno=True)
                    released = bool(libc.malloc_trim(0))
                except Exception as trim_exc:  # noqa: BLE001
                    logger.warning(
                        "GOV maybe_trim: malloc_trim call failed (best-effort, "
                        "non-glibc or stripped libc): %s",
                        trim_exc,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("GOV maybe_trim failed (best-effort, ignoring): %s", exc)
        finally:
            self.log_gov004_trim(released=released)
            self.counters.trim_calls += 1

    def should_evict_after_shard(self) -> bool:
        """True iff the governor says evict-after-use is required.

        When a config_service is set, reads enabled LIVE from
        config_service.get_config().cache_config.memory_governor_enabled.
        Falls back to the constructor value when no config_service is set.

        Returns True when:
        - governor disabled (live config or constructor)
        - band is RED
        - pre-first-sample (band starts at RED by fail-safe contract)
        - config_service read fails (fail-safe: treat as disabled/RED)
        """
        if self._config_service is not None:
            cache_cfg = self._read_live_config()
            if cache_cfg is None:
                # Config read failure: fail-safe — treat as evict-required
                return True
            if not cache_cfg.memory_governor_enabled:
                return True
        elif not self._enabled:
            return True
        return self.band == MemoryBand.RED

    def get_snapshot(self) -> dict:
        """Return the full §3.5 snapshot dict for the admin endpoint (Story 4).

        All counter fields are top-level (not nested) for direct REST serialisation.
        """
        with self._band_lock:
            band = self._band

        # Read swap usage.  psutil is a hard dependency in server mode but may
        # be absent in minimal test environments — tolerate ImportError silently.
        # Any other (unexpected) error is logged at WARNING so it is visible.
        swap_used_mb: float = 0.0
        try:
            import psutil

            swap_used_mb = psutil.swap_memory().used / (1024 * 1024)
        except ImportError:
            pass  # psutil not installed — swap_used_mb stays 0.0
        except Exception as exc:  # noqa: BLE001
            logger.warning("GOV get_snapshot: swap_memory() read failed: %s", exc)

        # Echo live config watermarks when a config_service is set.
        # Falls back to constructor-frozen defaults when no config_service is
        # configured (CLI/pre-init) or when the read fails (fail-soft: this is
        # a display-only read; never apply fail-safe RED here).
        live_cfg = self._read_live_config()
        if live_cfg is not None:
            echo_enabled = bool(live_cfg.memory_governor_enabled)
            echo_yellow_pct = float(live_cfg.memory_governor_yellow_pct)
            echo_red_pct = float(live_cfg.memory_governor_red_pct)
            echo_hysteresis_pct = float(live_cfg.memory_governor_hysteresis_pct)
            echo_red_min_dwell = float(live_cfg.memory_governor_red_min_dwell_seconds)
            echo_sample_interval = float(
                live_cfg.memory_governor_sample_interval_seconds
            )
            echo_swap_forces_red = bool(live_cfg.memory_governor_swap_forces_red)
            echo_swap_pswpin_threshold = int(
                live_cfg.memory_governor_swap_pswpin_red_threshold
            )
            echo_rss_inflation = float(live_cfg.memory_governor_rss_inflation_factor)
        else:
            echo_enabled = self._enabled
            echo_yellow_pct = self._yellow_pct
            echo_red_pct = self._red_pct
            echo_hysteresis_pct = self._hysteresis_pct
            echo_red_min_dwell = self._red_min_dwell_seconds
            echo_sample_interval = self._sample_interval_seconds
            echo_swap_forces_red = self._swap_forces_red
            echo_swap_pswpin_threshold = self._swap_pswpin_red_threshold
            echo_rss_inflation = self._rss_inflation_factor

        return {
            # Signal fields
            "band": band.value,
            "used_pct": getattr(self, "_last_used_pct", 0.0),
            "effective_limit_mb": getattr(self, "_last_effective_limit", 0)
            // (1024 * 1024),
            "effective_used_mb": getattr(self, "_last_effective_used", 0)
            // (1024 * 1024),
            "basis": getattr(self, "_last_basis", "unknown"),
            "pswpin_rate": getattr(self, "_last_pswpin_rate", 0),
            "swap_used_mb": swap_used_mb,
            # Transition counters (flat)
            "green_to_yellow": self.counters.green_to_yellow,
            "yellow_to_red": self.counters.yellow_to_red,
            "red_to_yellow": self.counters.red_to_yellow,
            "yellow_to_green": self.counters.yellow_to_green,
            # Action counters (flat)
            "shards_evicted_after_use": self.counters.shards_evicted_after_use,
            "lru_evictions": self.counters.lru_evictions,
            "trim_calls": self.counters.trim_calls,
            # Config echoes — live values when config_service is set, else constructor defaults
            "enabled": echo_enabled,
            "yellow_pct": echo_yellow_pct,
            "red_pct": echo_red_pct,
            "hysteresis_pct": echo_hysteresis_pct,
            "red_min_dwell_seconds": echo_red_min_dwell,
            "sample_interval_seconds": echo_sample_interval,
            "swap_forces_red": echo_swap_forces_red,
            "swap_pswpin_red_threshold": echo_swap_pswpin_threshold,
            "rss_inflation_factor": echo_rss_inflation,
            # Process identity
            "pid": os.getpid(),
        }

    def get_stats(self) -> dict:
        """Backward-compatible alias for get_snapshot()."""
        return self.get_snapshot()

    # ------------------------------------------------------------------
    # Structured log helpers (GOV-002 / GOV-003 / GOV-004)
    # ------------------------------------------------------------------

    def log_gov002_evict(self, *, shard: str, freed_mb: float) -> None:
        """Emit GOV-002 when a shard is evicted after use (RED band action).

        Rate-limited to at most one entry per _GOV002_MIN_INTERVAL_SECONDS to
        prevent log storms during sustained RED pressure.
        """
        now = time.monotonic()
        if now - self._gov002_last_log_time < _GOV002_MIN_INTERVAL_SECONDS:
            return
        self._gov002_last_log_time = now
        logger.warning(
            "GOV-002 evict_after_use shard=%s freed_mb=%.1f band=%s",
            shard,
            freed_mb,
            self.band.value,
        )

    def log_gov003_lru_evict(self, *, count: int, freed_mb: float) -> None:
        """Emit GOV-003 when proactive LRU eviction runs (YELLOW band action)."""
        logger.info(
            "GOV-003 lru_evict count=%d freed_mb=%.1f band=%s",
            count,
            freed_mb,
            self.band.value,
        )

    def log_gov004_trim(self, *, released: bool) -> None:
        """Emit GOV-004 after a malloc_trim attempt."""
        logger.info(
            "GOV-004 malloc_trim released=%s band=%s",
            released,
            self.band.value,
        )

    def evict_lru_to_floor(self, cache: Any, *, floor_entries: int) -> None:
        """Evict entries from `cache` down to `floor_entries` (YELLOW proactive action).

        Calls `cache.get_stats()["size"]` to determine current occupancy, then
        calls `cache.evict_lru_entries(n)` with the deficit count.  Does nothing
        when size <= floor_entries.  Increments `lru_evictions` by the count
        returned by the cache and always calls `maybe_trim()`.

        Never raises: all cache errors are logged at WARNING and swallowed so
        the caller's hot path is never interrupted.
        """
        try:
            stats = cache.get_stats()
            # HNSWIndexCacheStats is a dataclass — use attribute access, NOT subscript.
            size = stats.cached_repositories
            to_evict = size - floor_entries
            if to_evict > 0:
                evicted = cache.evict_lru_entries(to_evict)
                self.counters.lru_evictions += evicted if evicted is not None else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("GOV evict_lru_to_floor: cache error (best-effort): %s", exc)
        finally:
            self.maybe_trim()

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

        Story #1213 Story 2: reads watermarks LIVE from config_service before
        computing the sample, so config changes take effect on the next tick.
        If config read fails, fail-safe RED is applied immediately.
        """
        first_tick = self._first_tick

        # --- Live config read (Story #1213 Story 2) ---
        # Resolve effective watermarks: prefer live config, fall back to
        # constructor values (CLI/pre-init / no config_service).
        live_cache = self._read_live_config()
        if self._config_service is not None and live_cache is None:
            # config_service is set but read failed — fail-safe
            self._apply_fail_safe_red()
            return

        if live_cache is not None:
            yellow_pct = float(live_cache.memory_governor_yellow_pct)
            red_pct = float(live_cache.memory_governor_red_pct)
            hysteresis_pct = float(live_cache.memory_governor_hysteresis_pct)
            swap_forces_red_cfg = bool(live_cache.memory_governor_swap_forces_red)
            swap_pswpin_threshold = int(
                live_cache.memory_governor_swap_pswpin_red_threshold
            )
            red_min_dwell = float(live_cache.memory_governor_red_min_dwell_seconds)
        else:
            yellow_pct = self._yellow_pct
            red_pct = self._red_pct
            hysteresis_pct = self._hysteresis_pct
            swap_forces_red_cfg = self._swap_forces_red
            swap_pswpin_threshold = self._swap_pswpin_red_threshold
            red_min_dwell = self._red_min_dwell_seconds

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

        self._advance_band(
            sample,
            first_tick=first_tick,
            yellow_pct=yellow_pct,
            red_pct=red_pct,
            hysteresis_pct=hysteresis_pct,
            swap_forces_red_cfg=swap_forces_red_cfg,
            swap_pswpin_red_threshold=swap_pswpin_threshold,
            red_min_dwell=red_min_dwell,
        )

        # Emit GOV-005 when swap-in activity forced the band to RED.
        # Checked after _advance_band() so the band reflects the transition.
        # Only fires when pswpin_rate meets the configured minimum threshold
        # (default 100) so trivial OS noise does not spam the log.
        if (
            swap_forces_red_cfg
            and sample.pswpin_rate >= swap_pswpin_threshold
            and sample.used_pct >= yellow_pct
            and self.band == MemoryBand.RED
        ):
            logger.warning(
                "GOV-005 swap_forced_red pswpin_rate=%d used_pct=%.1f",
                sample.pswpin_rate,
                sample.used_pct,
            )

        # YELLOW proactive LRU eviction (Story 4 Critical 2).
        # When the band is YELLOW and a cache has been attached via attach_cache(),
        # evict the least-recently-used entries down to _YELLOW_LRU_FLOOR so the
        # hottest entries are retained.  Skipped silently when no cache is attached
        # (CLI/solo / pre-lifespan-wiring).
        if self.band == MemoryBand.YELLOW and self._attached_cache is not None:
            before_lru = self.counters.lru_evictions
            self.evict_lru_to_floor(
                self._attached_cache, floor_entries=_YELLOW_LRU_FLOOR
            )
            evicted_this_tick = self.counters.lru_evictions - before_lru
            self.log_gov003_lru_evict(count=evicted_this_tick, freed_mb=0.0)

        # Clear first-tick flag after successful processing so subsequent
        # ticks accumulate real operational transition counters.
        self._first_tick = False

    def _advance_band(
        self,
        sample: MemorySample,
        *,
        first_tick: bool = False,
        yellow_pct: Optional[float] = None,
        red_pct: Optional[float] = None,
        hysteresis_pct: Optional[float] = None,
        swap_forces_red_cfg: Optional[bool] = None,
        swap_pswpin_red_threshold: Optional[int] = None,
        red_min_dwell: Optional[float] = None,
    ) -> None:
        """Advance the band state machine based on the current sample.

        Cascades transitions within a single tick so that the final band
        reflects the current sample without requiring multiple _tick() calls.
        Example: RED (50%) -> YELLOW -> GREEN all in one tick when dwell=0.

        No direct RED<->GREEN edges exist; YELLOW is always visited.

        first_tick: when True, band state is updated but transition counters
            are NOT incremented (pre-init convergence from fail-safe RED).

        yellow_pct / red_pct / hysteresis_pct / swap_forces_red_cfg /
        swap_pswpin_red_threshold / red_min_dwell:
            When provided (by _tick() after a live config read), these override
            the constructor-frozen values so hot-reloaded watermarks take effect.
            When None, fall back to constructor values (backward compat / tests
            that call _advance_band() directly).
        """
        _yellow_pct = yellow_pct if yellow_pct is not None else self._yellow_pct
        _red_pct = red_pct if red_pct is not None else self._red_pct
        _hysteresis_pct = (
            hysteresis_pct if hysteresis_pct is not None else self._hysteresis_pct
        )
        _swap_forces_red = (
            swap_forces_red_cfg
            if swap_forces_red_cfg is not None
            else self._swap_forces_red
        )
        _swap_pswpin_threshold = (
            swap_pswpin_red_threshold
            if swap_pswpin_red_threshold is not None
            else self._swap_pswpin_red_threshold
        )
        _red_min_dwell = (
            red_min_dwell if red_min_dwell is not None else self._red_min_dwell_seconds
        )

        used_pct = sample.used_pct
        now = self._time_fn()
        swap_forces_red = (
            _swap_forces_red
            and sample.pswpin_rate >= _swap_pswpin_threshold
            and used_pct >= _yellow_pct
        )
        yellow_exit = _yellow_pct - _hysteresis_pct
        red_exit = _red_pct - _hysteresis_pct

        with self._band_lock:
            # --- RED state ---
            if self._band == MemoryBand.RED:
                dwell_ok = _red_min_dwell <= 0.0 or (
                    self._red_entry_time is not None
                    and (now - self._red_entry_time) >= _red_min_dwell
                )
                if used_pct < red_exit and not swap_forces_red and dwell_ok:
                    self._band = MemoryBand.YELLOW
                    if not first_tick:
                        self.counters.red_to_yellow += 1
                    logger.info("GOV-001 band RED->YELLOW used_pct=%.1f", used_pct)
                    # Cascade: check whether YELLOW should also exit to GREEN
                    if used_pct < yellow_exit:
                        self._band = MemoryBand.GREEN
                        if not first_tick:
                            self.counters.yellow_to_green += 1
                        logger.info(
                            "GOV-001 band YELLOW->GREEN used_pct=%.1f (cascade from RED)",
                            used_pct,
                        )
                # No other RED transitions in a single tick

            # --- YELLOW state ---
            elif self._band == MemoryBand.YELLOW:
                if used_pct >= _red_pct or swap_forces_red:
                    self._band = MemoryBand.RED
                    self._red_entry_time = now
                    if not first_tick:
                        self.counters.yellow_to_red += 1
                    logger.warning(
                        "GOV-001 band YELLOW->RED used_pct=%.1f swap=%s",
                        used_pct,
                        swap_forces_red,
                    )
                elif used_pct < yellow_exit:
                    self._band = MemoryBand.GREEN
                    if not first_tick:
                        self.counters.yellow_to_green += 1
                    logger.info("GOV-001 band YELLOW->GREEN used_pct=%.1f", used_pct)

            # --- GREEN state ---
            elif self._band == MemoryBand.GREEN:
                if used_pct >= _red_pct or swap_forces_red:
                    # No direct GREEN->RED; step through YELLOW first
                    self._band = MemoryBand.YELLOW
                    if not first_tick:
                        self.counters.green_to_yellow += 1
                    logger.info(
                        "GOV-001 band GREEN->YELLOW used_pct=%.1f swap=%s",
                        used_pct,
                        swap_forces_red,
                    )
                    # Cascade: check whether YELLOW should immediately enter RED
                    if used_pct >= _red_pct or swap_forces_red:
                        self._band = MemoryBand.RED
                        self._red_entry_time = now
                        if not first_tick:
                            self.counters.yellow_to_red += 1
                        logger.warning(
                            "GOV-001 band YELLOW->RED used_pct=%.1f (cascade from GREEN)",
                            used_pct,
                        )
                elif used_pct >= _yellow_pct:
                    self._band = MemoryBand.YELLOW
                    if not first_tick:
                        self.counters.green_to_yellow += 1
                    logger.info("GOV-001 band GREEN->YELLOW used_pct=%.1f", used_pct)

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
    config_service: Any = None,
) -> MemoryGovernor:
    """Build and return a MemoryGovernor with default production readers.

    Called from service_init.py after initialize_caches(). The returned
    governor is NOT started here — the caller must call start() and register
    stop() in the lifespan shutdown hook.

    Story #1213 Story 2: when config_service is provided, the governor reads
    all watermarks LIVE from config_service.get_config().cache_config on each
    tick so Web UI changes are picked up without a server restart.  The scalar
    constructor args (yellow_pct etc.) become fallbacks used only when
    config_service is absent (CLI / unit tests without a config_service).
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
        config_service=config_service,
    )
    return gov
