"""Refresh embedding volume alarm (Story #2118 slice 1b).

The safety net against a runaway re-embed of one golden repo (2026-10-06: a
staging cluster re-embedded a large non-git repository about once over
through repeated interrupted refreshes). Every server-started indexing run
records its repository alias on ``embedding_call_stats`` (slice 1a); this
module sums the refresh spend per alias from that shared table, so every
node derives the same decision from the shared DB -- no per-node state
decides anything.

Rule (fixed constants, owner decision 2026-10-09, no setting): alias A
alarms when ``refresh_items_24h(A) > max(FLOOR_ITEMS, FRACTION *
published_points(A))``. NULL published points (never published) uses the
floor. While A alarms, SYSTEM-triggered refreshes of A are deferred, never
dropped: a strike-free pending trigger due ``DEFERRAL_SECONDS`` later is
persisted on the Bug #2022 pending-trigger store, and the refresh
scheduler's deferred-trigger loop re-submits it, which re-checks this gate.
Admin refreshes and ``force_reset`` always run (the caller's gate).

The alarm is "blind" -- DEGRADED health, no pause target -- when the
aggregate read fails, when stats recording is disabled, or when an
index/refresh/temporal_refresh row of the last hour has no alias. A blind
alarm never reports "0 spend" and never pauses anything.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from code_indexer.global_repos.refresh_failure_recovery import RefreshDeferredError

logger = logging.getLogger(__name__)

#: Absolute floor: below this many refresh items per 24 h nothing alarms, so
#: small repos' ordinary churn never pauses them.
FLOOR_ITEMS = 10_000
#: Owner decision: a quarter of the published size per 24 h. The incident
#: re-embedded about 1x its repo in total, so the design's 1.5x would never
#: have fired; 0.25 does, while initial registration and add-index (purposes
#: index/temporal) never count.
FRACTION = 0.25
WINDOW_SECONDS = 24 * 3_600
#: Unattributed index-time rows are looked for in the last hour only, so one
#: old row does not keep the alarm blind for a day.
UNATTRIBUTED_WINDOW_SECONDS = 3_600
#: A deferred system trigger fires (and re-checks the gate) one hour later.
DEFERRAL_SECONDS = 3_600
REPORT_TTL_SECONDS = 60.0
#: The pause / blind ERROR is logged at most once per alias per hour per
#: node (per-node by design: it is only log volume control).
ERROR_LOG_INTERVAL_SECONDS = 3_600.0
#: Health lists at most this many alarming aliases in its one reason.
MAX_ALIASES_IN_REASON = 10

REFRESH_PURPOSES = ("refresh", "temporal_refresh")
ATTRIBUTED_PURPOSES = ("index", "refresh", "temporal_refresh")
_BLIND_KEY = "<volume-alarm-blind>"


def alarm_threshold(published_points: Optional[int]) -> float:
    """Items per 24 h above which an alias alarms."""
    if published_points is None:
        return float(FLOOR_ITEMS)
    return max(float(FLOOR_ITEMS), FRACTION * published_points)


def count_published_points(snapshot_path: Any) -> Optional[int]:
    """Points in a published snapshot: the sum of ``count()`` over every
    CHUNKS_DB collection of its index, each ``chunks.db`` opened read-only.
    None when the snapshot holds no CHUNKS_DB collection (nothing countable
    without walking legacy shards). Any read error propagates."""
    from pathlib import Path

    from code_indexer.storage.shared.chunk_layout import (
        ChunkLayout,
        resolve_chunk_layout,
    )
    from code_indexer.storage.shared.collection_migration import CHUNKS_DB_FILENAME
    from code_indexer.storage.sqlite_chunk_store import ChunkStore

    index_dir = Path(snapshot_path) / ".code-indexer" / "index"
    total: Optional[int] = None
    for collection_dir in sorted(p for p in index_dir.iterdir() if p.is_dir()):
        if resolve_chunk_layout(collection_dir) is not ChunkLayout.CHUNKS_DB:
            continue
        with ChunkStore(collection_dir / CHUNKS_DB_FILENAME, read_only=True) as store:
            total = (total or 0) + store.count()
    return total


def record_published_points(
    metadata: Any, alias_name: str, published_path: Any
) -> None:
    """After a verified publish: store *published_path*'s point count as the
    golden repo's reference size. Runs in the refresh worker thread. The
    alias is already published, so a failure is logged, never raised; the
    previous value (or NULL: the floor) stays in force."""
    try:
        points = count_published_points(published_path)
        if points is None:
            logger.info(
                f"Story #2118: {alias_name} published no chunks.db collection; "
                f"its volume-alarm reference size is unchanged"
            )
            return
        if not metadata.set_published_points(_bare_alias(alias_name), points):
            logger.warning(
                f"Story #2118: no golden-repo metadata row for {alias_name}; "
                f"its volume alarm uses the {FLOOR_ITEMS}-item floor"
            )
    except Exception as exc:
        logger.error(
            f"Story #2118: failed to record the published size of {alias_name}: "
            f"{type(exc).__name__}: {exc}"
        )


def _bare_alias(alias: str) -> str:
    return alias.removesuffix("-global")


@dataclass(frozen=True)
class AliasSpend:
    alias: str
    items_24h: int
    threshold: float


@dataclass(frozen=True)
class VolumeAlarmReport:
    alarms: Tuple[AliasSpend, ...] = ()
    blind_reasons: Tuple[str, ...] = ()

    def health_reasons(self) -> List[str]:
        """DEGRADED reasons for /health: one naming every alarming alias
        with its 24 h sum (capped), plus one per blind condition."""
        reasons: List[str] = []
        if self.alarms:
            shown = [
                f"{a.alias} ({a.items_24h} items in 24 h > {int(a.threshold)})"
                for a in self.alarms[:MAX_ALIASES_IN_REASON]
            ]
            extra = len(self.alarms) - len(shown)
            if extra > 0:
                shown.append(f"+{extra} more")
            reasons.append(
                "Refresh embedding volume alarm: system refreshes paused for "
                + ", ".join(shown)
            )
        reasons.extend(
            f"Refresh embedding volume alarm blind: {reason}"
            for reason in self.blind_reasons
        )
        return reasons


class _HourlyLogLimiter:
    """True at most once per key per interval. Bounded: entries older than
    the interval are pruned on every call (at most one per repo)."""

    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def should_log(self, key: str) -> bool:
        now = self._monotonic()
        with self._lock:
            self._last = {
                k: t
                for k, t in self._last.items()
                if now - t < ERROR_LOG_INTERVAL_SECONDS
            }
            if key in self._last:
                return False
            self._last[key] = now
            return True


class IndexSpendAlarm:
    """Evaluates the rule from the shared stats and metadata stores."""

    def __init__(
        self,
        stats_backend: Any,
        metadata_backend: Any,
        stats_enabled: Callable[[], bool],
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stats = stats_backend
        self._metadata = metadata_backend
        self._stats_enabled = stats_enabled
        self._clock = clock
        self._monotonic = monotonic
        self._log_limiter = _HourlyLogLimiter(monotonic)
        self._cache_lock = threading.Lock()
        self._cached: Optional[Tuple[float, VolumeAlarmReport]] = None

    def evaluate(self) -> VolumeAlarmReport:
        """Fleet view: one grouped aggregate over the 24 h window, one over
        the last hour for unattributed rows, and a published-points read
        only for aliases above the floor."""
        if not self._stats_enabled():
            return VolumeAlarmReport(
                blind_reasons=("embedding stats recording is disabled",)
            )
        now = self._clock()
        blind: List[str] = []
        try:
            sums = self._stats.sum_items_since(REFRESH_PURPOSES, now - WINDOW_SECONDS)
        except Exception as exc:
            return VolumeAlarmReport(
                blind_reasons=(
                    f"refresh volume read failed: {type(exc).__name__}: {exc}",
                )
            )
        try:
            recent = self._stats.sum_items_since(
                ATTRIBUTED_PURPOSES, now - UNATTRIBUTED_WINDOW_SECONDS
            )
            if None in recent:
                blind.append(
                    f"{recent[None]} index/refresh items in the last hour carry "
                    f"no repository alias"
                )
        except Exception as exc:
            blind.append(f"unattributed-spend read failed: {type(exc).__name__}: {exc}")
        # Only aliases above the floor can alarm; their published sizes are
        # read in ONE metadata query.
        candidates = sorted(
            a for a, total in sums.items() if a is not None and total > FLOOR_ITEMS
        )
        alarms: List[AliasSpend] = []
        if not candidates:
            return VolumeAlarmReport(blind_reasons=tuple(blind))
        try:
            sizes = self._metadata.get_published_points_many(
                [_bare_alias(alias) for alias in candidates]
            )
        except Exception as exc:
            blind.append(
                f"published sizes of {', '.join(candidates)} unreadable: "
                f"{type(exc).__name__}: {exc}"
            )
            return VolumeAlarmReport(blind_reasons=tuple(blind))
        for alias in candidates:
            threshold = alarm_threshold(sizes.get(_bare_alias(alias)))
            if sums[alias] > threshold:
                alarms.append(AliasSpend(alias, sums[alias], threshold))
        return VolumeAlarmReport(alarms=tuple(alarms), blind_reasons=tuple(blind))

    def cached_report(self) -> VolumeAlarmReport:
        """``evaluate()`` reused for REPORT_TTL_SECONDS (health is polled;
        the read is a DB aggregate)."""
        now = self._monotonic()
        with self._cache_lock:
            if self._cached is not None and now - self._cached[0] < REPORT_TTL_SECONDS:
                return self._cached[1]
        report = self.evaluate()
        with self._cache_lock:
            self._cached = (now, report)
        return report

    def _alias_spend(self, alias: str) -> Optional[AliasSpend]:
        """The alarming spend of *alias*, or None. A read failure raises."""
        now = self._clock()
        total = self._stats.sum_items_since(
            REFRESH_PURPOSES, now - WINDOW_SECONDS, golden_repo_alias=alias
        ).get(alias, 0)
        if total <= FLOOR_ITEMS:
            return None
        threshold = alarm_threshold(
            self._metadata.get_published_points(_bare_alias(alias))
        )
        return AliasSpend(alias, total, threshold) if total > threshold else None

    def defer_if_alarming(self, alias: str) -> None:
        """System-refresh gate. Raises RefreshDeferredError after persisting
        a strike-free pending trigger due DEFERRAL_SECONDS later when *alias*
        alarms. A blind alarm (disabled stats, failed read) never pauses."""
        if not self._stats_enabled():
            return
        try:
            spend = self._alias_spend(alias)
        except Exception as exc:
            if self._log_limiter.should_log(f"{_BLIND_KEY}:{alias}"):
                logger.error(
                    f"Story #2118: refresh volume alarm blind for {alias} "
                    f"(refresh not paused): {type(exc).__name__}: {exc}"
                )
            return
        if spend is None:
            return
        due = self._clock() + DEFERRAL_SECONDS
        self._metadata.mark_volume_alarm_deferral(alias, due)
        if self._log_limiter.should_log(alias):
            logger.error(
                f"Story #2118: refresh embedding volume alarm for {alias}: "
                f"{spend.items_24h} refresh items in 24 h exceed "
                f"{int(spend.threshold)}; system refreshes are deferred (next "
                f"check in {DEFERRAL_SECONDS}s); admin refreshes still run"
            )
        raise RefreshDeferredError(
            alias, due, reason="refresh embedding volume alarm active"
        )


def build_index_spend_alarm(backend_registry: Any) -> IndexSpendAlarm:
    """The server's alarm over the SHARED backends (SQLite solo, PostgreSQL
    cluster), so every node decides from the same rows. The stats switch
    (``embedding_stats_config.enabled``) is read live on every evaluation.
    Raises when the registry lacks either backend (lifespan then publishes
    None, which health reports as blind)."""
    stats = getattr(backend_registry, "embedding_call_stats", None)
    metadata = getattr(backend_registry, "golden_repo_metadata", None)
    if stats is None or metadata is None:
        raise RuntimeError(
            "backend registry lacks embedding_call_stats or golden_repo_metadata"
        )

    def _stats_enabled() -> bool:
        from code_indexer.server.services.config_service import get_config_service

        section = get_config_service().get_config().embedding_stats_config
        # An absent section records rows (EmbeddingStatsWriter._is_enabled).
        return True if section is None else bool(section.enabled)

    return IndexSpendAlarm(
        stats_backend=stats, metadata_backend=metadata, stats_enabled=_stats_enabled
    )
