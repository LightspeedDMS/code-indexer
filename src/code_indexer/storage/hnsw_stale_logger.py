"""Bug #896: rate-limited, context-enriched stale-HNSW warning emitter.

Provides HnswStaleTracker -- an instance-level LRU cache keyed by collection path:

- First miss  -> WARNING with full context (alias, path, collection_name).
- Subsequent misses within cooldown_s -> DEBUG (suppressed within cooldown).
- Persistent staleness past escalate_after_s with continued misses -> one-shot ERROR.

Cache is bounded by max_size (default 1024) to prevent unbounded growth.
Thread-safe via a per-instance lock.

The clock is injectable via the constructor for deterministic unit testing.

Module-level singleton (_DEFAULT_TRACKER) and convenience function
log_hnsw_stale() are provided for production call sites.
"""

import math
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

_DEFAULT_MAX_SIZE = 1024
_DEFAULT_COOLDOWN_S = 60.0
_DEFAULT_ESCALATE_AFTER_S = 600.0

# All logger methods called in _emit_stale_log() -- validated upfront in log_stale().
_REQUIRED_LOGGER_METHODS = ("error", "warning", "debug")


def _require_int(name: str, value) -> int:
    """Validate that value is a plain int (not bool) and return it."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            f"{name} must be an integer, got {type(value).__name__!r}: {value!r}"
        )
    return value


def _require_number(name: str, value) -> float:
    """Validate that value is a real number (int or float, not bool/None) and return float."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(
            f"{name} must be a number (int or float), "
            f"got {type(value).__name__!r}: {value!r}"
        )
    return float(value)


def _require_finite_float(name: str, value) -> float:
    """Validate that value is a finite real number and return float.

    Rejects NaN and Inf -- a clock returning those would corrupt age calculations.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(
            f"{name} must return a finite numeric timestamp, "
            f"got {type(value).__name__!r}: {value!r}"
        )
    fval = float(value)
    if not math.isfinite(fval):
        raise ValueError(
            f"{name} must return a finite numeric timestamp, got non-finite: {fval!r}"
        )
    return fval


def _validate_logger(logger) -> None:
    """Raise ValueError if logger is None or missing any required callable method."""
    if logger is None:
        raise ValueError(
            "logger must be a Python logger instance with error/warning/debug methods, got None"
        )
    for method_name in _REQUIRED_LOGGER_METHODS:
        attr = getattr(logger, method_name, None)
        if not callable(attr):
            raise ValueError(
                f"logger.{method_name} must be callable; "
                f"got {type(attr).__name__!r} on {type(logger).__name__!r}"
            )


def _validate_collection_path(collection_path) -> str:
    """Validate that collection_path is str or PathLike and return filesystem path string.

    Uses os.fspath() to guarantee correct path extraction from custom PathLike
    implementations (str() may return repr() for non-Path objects).
    """
    if collection_path is None:
        raise ValueError("collection_path must not be None")
    if not isinstance(collection_path, (str, os.PathLike)):
        raise ValueError(
            f"collection_path must be a str or os.PathLike, "
            f"got {type(collection_path).__name__!r}: {collection_path!r}"
        )
    return os.fspath(collection_path)


def _validate_collection_name(collection_name) -> str:
    """Validate that collection_name is a non-empty str and return it."""
    if not isinstance(collection_name, str):
        raise ValueError(
            f"collection_name must be a non-empty str, "
            f"got {type(collection_name).__name__!r}: {collection_name!r}"
        )
    if collection_name == "":
        raise ValueError("collection_name must be a non-empty str, got empty string")
    return collection_name


def _emit_stale_log(
    logger,
    level: str,
    key: str,
    collection_name: str,
    alias: Optional[str],
    now: float,
    state: "_StaleState",
) -> None:
    """Format and emit the log record at the chosen level.

    Module-level function so HnswStaleTracker stays at 3 methods.
    """
    ctx = f"alias={alias!r}, path={key}, model={collection_name}"
    base = "HNSW index is stale and missing"

    if level == "ERROR":
        age_s = int(now - state.first_seen_at)
        logger.error(
            f"{base} for {age_s} seconds (persistent staleness). "
            f"{ctx} -- Run 'cidx index' to rebuild."
        )
    elif level == "WARNING":
        logger.warning(
            f"{base}. {ctx} -- Run 'cidx index' to build the index. "
            "Returning empty results."
        )
    else:
        logger.debug(f"{base} (suppressed within cooldown). {ctx}")


@dataclass
class _StaleState:
    first_seen_at: float
    last_logged_at: float
    escalated: bool = field(default=False)


class HnswStaleTracker:
    """Per-instance rate-limiter for stale-HNSW warnings.

    Args:
        clock:    Callable returning a finite numeric timestamp (float seconds).
                  Must be callable. Defaults to time.time. Inject a deterministic
                  callable in tests.
        max_size: Maximum number of collection paths tracked before the oldest
                  entry is evicted (LRU). Must be an integer >= 1. Default 1024.

    Raises:
        ValueError: If clock is not callable, or max_size is invalid.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        max_size: int = _DEFAULT_MAX_SIZE,
    ) -> None:
        if not callable(clock):
            raise ValueError(
                f"clock must be callable, got {type(clock).__name__!r}: {clock!r}"
            )
        validated_max = _require_int("max_size", max_size)
        if validated_max < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size!r}")
        self._clock = clock
        self._max_size = validated_max
        self._cache: "OrderedDict[str, _StaleState]" = OrderedDict()
        self._lock = threading.Lock()

    def log_stale(
        self,
        logger,
        *,
        collection_path: Union[str, "os.PathLike[str]"],
        collection_name: str,
        alias: Optional[str] = None,
        cooldown_s: float = _DEFAULT_COOLDOWN_S,
        escalate_after_s: float = _DEFAULT_ESCALATE_AFTER_S,
    ) -> None:
        """Emit a rate-limited log entry for a stale HNSW collection.

        Args:
            logger:           Python logger instance with callable error/warning/debug.
            collection_path:  Path (str or PathLike) to the collection directory.
            collection_name:  Embedding model / collection identifier. Non-empty str.
            alias:            Repository alias (optional; pass None if unavailable).
            cooldown_s:       Seconds between repeated WARNINGs per collection >= 0.
            escalate_after_s: Seconds of continuous staleness before one-shot ERROR >= 0.

        Raises:
            ValueError: If any parameter fails validation, or clock returns non-finite.
        """
        _validate_logger(logger)
        key = _validate_collection_path(collection_path)
        _validate_collection_name(collection_name)

        validated_cooldown = _require_number("cooldown_s", cooldown_s)
        if validated_cooldown < 0:
            raise ValueError(f"cooldown_s must be >= 0, got {cooldown_s!r}")

        validated_escalate = _require_number("escalate_after_s", escalate_after_s)
        if validated_escalate < 0:
            raise ValueError(f"escalate_after_s must be >= 0, got {escalate_after_s!r}")

        now = _require_finite_float("clock", self._clock())
        level, state = self._get_or_create_level(
            key, now, validated_cooldown, validated_escalate
        )
        _emit_stale_log(logger, level, key, collection_name, alias, now, state)

    def _get_or_create_level(
        self,
        key: str,
        now: float,
        cooldown_s: float,
        escalate_after_s: float,
    ) -> tuple:
        """Return (level, state) under lock; mutates cache as needed."""
        with self._lock:
            state = self._cache.get(key)
            if state is None:
                state = _StaleState(first_seen_at=now, last_logged_at=now)
                self._cache[key] = state
                while len(self._cache) > self._max_size:
                    self._cache.popitem(last=False)
                return "WARNING", state

            self._cache.move_to_end(key)
            age = now - state.first_seen_at
            since_last = now - state.last_logged_at

            if state.escalated:
                # Bug #896: post-escalation, stay quiet at DEBUG — the problem has already
                # been flagged at ERROR level once, further WARNINGs would re-introduce storm.
                return "DEBUG", state

            if age >= escalate_after_s:
                state.escalated = True
                state.last_logged_at = now
                return "ERROR", state

            if since_last < cooldown_s:
                return "DEBUG", state

            state.last_logged_at = now
            return "WARNING", state


_DEFAULT_TRACKER = HnswStaleTracker()


def log_hnsw_stale(
    logger,
    *,
    collection_path: Union[str, "os.PathLike[str]"],
    collection_name: str,
    alias: Optional[str] = None,
    cooldown_s: float = _DEFAULT_COOLDOWN_S,
    escalate_after_s: float = _DEFAULT_ESCALATE_AFTER_S,
) -> None:
    """Convenience wrapper around the module-level HnswStaleTracker singleton.

    Suitable for direct use in production code. For testing, instantiate
    HnswStaleTracker directly with an injectable clock.
    """
    _DEFAULT_TRACKER.log_stale(
        logger,
        collection_path=collection_path,
        collection_name=collection_name,
        alias=alias,
        cooldown_s=cooldown_s,
        escalate_after_s=escalate_after_s,
    )
