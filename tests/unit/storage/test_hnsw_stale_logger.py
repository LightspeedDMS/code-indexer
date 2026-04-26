"""Tests for Bug #896: rate-limited, context-enriched stale-HNSW warning emitter.

Tests the HnswStaleTracker public class which provides:
- First-miss WARNING with full context (alias, path, collection_name)
- Per-collection dedup within cooldown -> DEBUG
- Independent cadence for distinct collections
- Persistent-staleness escalation to ERROR (one-shot)
- LRU cache bounded by max_size constructor parameter
- Fresh instance per test (no shared state between tests)

All time injection and cache sizing go through the public constructor API.
No private module attributes are patched.
"""
import logging
import pytest

from code_indexer.storage.hnsw_stale_logger import HnswStaleTracker


# ---------------------------------------------------------------------------
# Test 1: single miss -> WARNING with full context
# ---------------------------------------------------------------------------

def test_single_miss_emits_warning_with_context(caplog, tmp_path):
    """First call for a collection path must emit WARNING with alias, path, model."""
    tracker = HnswStaleTracker(clock=lambda: 0.0)
    collection_path = tmp_path / "repos" / "my-repo" / "voyage-3"
    alias = "my-repo"
    collection_name = "voyage-3"

    logger = logging.getLogger("test_logger_single")
    with caplog.at_level(logging.DEBUG, logger="test_logger_single"):
        tracker.log_stale(
            logger,
            collection_path=collection_path,
            collection_name=collection_name,
            alias=alias,
        )

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1, f"Expected 1 WARNING, got {len(warning_records)}"

    msg = warning_records[0].message
    assert alias in msg, f"alias {alias!r} not in message: {msg!r}"
    assert str(collection_path) in msg, f"collection_path not in message: {msg!r}"
    assert collection_name in msg, f"collection_name (model) not in message: {msg!r}"


# ---------------------------------------------------------------------------
# Test 2: 100 misses within cooldown -> 1 WARNING + 99 DEBUGs
# ---------------------------------------------------------------------------

def test_100_misses_in_cooldown_emit_1_warning_and_99_debugs(caplog, tmp_path):
    """Within 60s cooldown, only the first call is WARNING; the rest are DEBUG."""
    # All calls happen at t=0.0 (within the 60s default cooldown)
    tracker = HnswStaleTracker(clock=lambda: 0.0)
    collection_path = tmp_path / "repos" / "repo-a"
    collection_name = "voyage-3"

    logger = logging.getLogger("test_logger_100")
    with caplog.at_level(logging.DEBUG, logger="test_logger_100"):
        for _ in range(100):
            tracker.log_stale(
                logger,
                collection_path=collection_path,
                collection_name=collection_name,
                alias="repo-a",
            )

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]

    assert len(warning_records) == 1, f"Expected 1 WARNING, got {len(warning_records)}"
    assert len(debug_records) == 99, f"Expected 99 DEBUGs, got {len(debug_records)}"


# ---------------------------------------------------------------------------
# Test 3: two distinct collections -> independent cadence
# ---------------------------------------------------------------------------

def test_independent_cadence_for_two_collections(caplog, tmp_path):
    """Each collection gets its own WARNING on first call; then DEBUG within cooldown."""
    tracker = HnswStaleTracker(clock=lambda: 0.0)
    path_a = tmp_path / "repos" / "repo-a"
    path_b = tmp_path / "repos" / "repo-b"

    logger = logging.getLogger("test_logger_two")
    with caplog.at_level(logging.DEBUG, logger="test_logger_two"):
        # First call for each collection -> WARNING
        tracker.log_stale(logger, collection_path=path_a, collection_name="voyage-3", alias="repo-a")
        tracker.log_stale(logger, collection_path=path_b, collection_name="voyage-3", alias="repo-b")
        # Second call for each (within cooldown) -> DEBUG
        tracker.log_stale(logger, collection_path=path_a, collection_name="voyage-3", alias="repo-a")
        tracker.log_stale(logger, collection_path=path_b, collection_name="voyage-3", alias="repo-b")

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]

    assert len(warning_records) == 2, (
        f"Expected 2 WARNINGs (one per collection first call), got {len(warning_records)}"
    )
    assert len(debug_records) == 2, (
        f"Expected 2 DEBUGs (second call per collection within cooldown), got {len(debug_records)}"
    )


# ---------------------------------------------------------------------------
# Test 4: persistent staleness -> one-shot ERROR escalation
# ---------------------------------------------------------------------------

def test_persistent_staleness_escalates_once_to_error(caplog, tmp_path):
    """After escalate_after_s of continuous staleness, exactly one ERROR is emitted.

    Sequence (using a mutable time container injected via clock lambda):
      t=0   -> WARNING (first miss)
      t=30  -> DEBUG (within 60s cooldown)
      t=700 -> ERROR (past escalate_after_s=600, first and only escalation)
      t=1400 -> DEBUG (escalated=True sticks; no second ERROR)
    """
    current_time = [0.0]
    tracker = HnswStaleTracker(clock=lambda: current_time[0])
    collection_path = tmp_path / "repos" / "stale-repo"
    collection_name = "voyage-3"

    logger = logging.getLogger("test_logger_escalate")
    with caplog.at_level(logging.DEBUG, logger="test_logger_escalate"):
        # t=0: first miss -> WARNING
        tracker.log_stale(
            logger,
            collection_path=collection_path,
            collection_name=collection_name,
            alias="stale-repo",
            cooldown_s=60.0,
            escalate_after_s=600.0,
        )

        # t=30: within cooldown -> DEBUG
        current_time[0] = 30.0
        tracker.log_stale(
            logger,
            collection_path=collection_path,
            collection_name=collection_name,
            alias="stale-repo",
            cooldown_s=60.0,
            escalate_after_s=600.0,
        )

        # t=700: past escalate_after_s -> ERROR (one-shot)
        current_time[0] = 700.0
        tracker.log_stale(
            logger,
            collection_path=collection_path,
            collection_name=collection_name,
            alias="stale-repo",
            cooldown_s=60.0,
            escalate_after_s=600.0,
        )

        # t=1400: escalated=True sticks -> DEBUG (not another ERROR)
        current_time[0] = 1400.0
        tracker.log_stale(
            logger,
            collection_path=collection_path,
            collection_name=collection_name,
            alias="stale-repo",
            cooldown_s=60.0,
            escalate_after_s=600.0,
        )

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]

    assert len(warning_records) == 1, f"Expected 1 WARNING, got {len(warning_records)}"
    assert len(error_records) == 1, f"Expected 1 ERROR, got {len(error_records)}"
    assert len(debug_records) == 2, f"Expected 2 DEBUGs (t=30 and t=1400), got {len(debug_records)}"


# ---------------------------------------------------------------------------
# Test 5: LRU cache bounded by max_size
# ---------------------------------------------------------------------------

def test_cache_bounded_by_max_evicts_oldest(caplog, tmp_path):
    """With max_size=5, feeding 6 distinct paths evicts the oldest.

    After eviction, calling the oldest path again is treated as a first miss -> WARNING.
    """
    tracker = HnswStaleTracker(clock=lambda: 0.0, max_size=5)
    logger = logging.getLogger("test_logger_bounded")

    paths = [tmp_path / f"repo-{i}" for i in range(6)]

    with caplog.at_level(logging.DEBUG, logger="test_logger_bounded"):
        # Insert 6 paths; oldest (paths[0]) gets evicted when paths[5] is inserted
        for path in paths:
            tracker.log_stale(logger, collection_path=path, collection_name="voyage-3", alias=None)

        # All 6 first-calls should be WARNINGs
        warning_before = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_before) == 6, (
            f"Expected 6 WARNINGs for 6 distinct paths, got {len(warning_before)}"
        )

        caplog.clear()

        # Re-call paths[0] (evicted) -> treated as first miss -> WARNING again
        tracker.log_stale(logger, collection_path=paths[0], collection_name="voyage-3", alias=None)
        # Re-call paths[5] (still in cache, within cooldown) -> DEBUG
        tracker.log_stale(logger, collection_path=paths[5], collection_name="voyage-3", alias=None)

    after_records = caplog.records
    warning_after = [r for r in after_records if r.levelno == logging.WARNING]
    debug_after = [r for r in after_records if r.levelno == logging.DEBUG]

    assert len(warning_after) == 1, (
        f"Expected 1 WARNING (evicted path treated as first miss), got {len(warning_after)}"
    )
    assert len(debug_after) == 1, (
        f"Expected 1 DEBUG (paths[5] still in cache within cooldown), got {len(debug_after)}"
    )


# ---------------------------------------------------------------------------
# Test 6: fresh tracker instance has no state
# ---------------------------------------------------------------------------

def test_fresh_tracker_has_no_state(caplog, tmp_path):
    """A new HnswStaleTracker instance always treats every key as first miss -> WARNING.

    This replaces the _reset_cache_for_testing() helper from the original plan.
    Because state lives in the instance, tests simply use a fresh instance.
    """
    collection_path = tmp_path / "repos" / "any-repo"
    collection_name = "voyage-3"

    logger = logging.getLogger("test_logger_fresh")
    with caplog.at_level(logging.DEBUG, logger="test_logger_fresh"):
        tracker_a = HnswStaleTracker(clock=lambda: 0.0)
        # First call on tracker_a -> WARNING
        tracker_a.log_stale(logger, collection_path=collection_path, collection_name=collection_name, alias=None)
        # Second call on tracker_a (same key, cooldown not expired) -> DEBUG
        tracker_a.log_stale(logger, collection_path=collection_path, collection_name=collection_name, alias=None)

        caplog.clear()

        # Fresh tracker_b has no state for this key -> WARNING again
        tracker_b = HnswStaleTracker(clock=lambda: 0.0)
        tracker_b.log_stale(logger, collection_path=collection_path, collection_name=collection_name, alias=None)

    after_records = caplog.records
    warning_after = [r for r in after_records if r.levelno == logging.WARNING]
    assert len(warning_after) == 1, (
        f"Expected 1 WARNING from fresh tracker (first miss), got {len(warning_after)}"
    )


# ---------------------------------------------------------------------------
# Test 7: invalid constructor parameters raise ValueError
# ---------------------------------------------------------------------------

def test_invalid_max_size_raises_value_error():
    """HnswStaleTracker rejects non-positive max_size."""
    with pytest.raises(ValueError, match="max_size"):
        HnswStaleTracker(max_size=0)
    with pytest.raises(ValueError, match="max_size"):
        HnswStaleTracker(max_size=-1)


def test_invalid_cooldown_raises_value_error(tmp_path):
    """log_stale rejects negative cooldown_s."""
    tracker = HnswStaleTracker()
    logger = logging.getLogger("test_validation")
    with pytest.raises(ValueError, match="cooldown_s"):
        tracker.log_stale(
            logger,
            collection_path=tmp_path / "repo",
            collection_name="voyage-3",
            alias=None,
            cooldown_s=-1.0,
        )


def test_invalid_escalate_after_raises_value_error(tmp_path):
    """log_stale rejects negative escalate_after_s."""
    tracker = HnswStaleTracker()
    logger = logging.getLogger("test_validation")
    with pytest.raises(ValueError, match="escalate_after_s"):
        tracker.log_stale(
            logger,
            collection_path=tmp_path / "repo",
            collection_name="voyage-3",
            alias=None,
            escalate_after_s=-1.0,
        )


# ---------------------------------------------------------------------------
# Test 10: invalid parameter types raise ValueError (not TypeError)
# ---------------------------------------------------------------------------

def test_invalid_max_size_type_raises_value_error():
    """HnswStaleTracker raises ValueError (not TypeError) for non-int max_size.

    Deliberately passes wrong types to verify the validator converts TypeError
    to ValueError before it can propagate.
    """
    # str "10" would raise TypeError on comparison — must be caught as ValueError
    with pytest.raises(ValueError, match="max_size"):
        HnswStaleTracker(max_size="10")  # type: ignore[arg-type]  # intentional wrong type for validation test
    # None would raise TypeError on comparison — must be caught as ValueError
    with pytest.raises(ValueError, match="max_size"):
        HnswStaleTracker(max_size=None)  # type: ignore[arg-type]  # intentional wrong type for validation test


def test_invalid_cooldown_type_raises_value_error(tmp_path):
    """log_stale raises ValueError (not TypeError) for non-numeric cooldown_s.

    Both str and None are tested to cover the two common mis-use patterns.
    """
    tracker = HnswStaleTracker()
    logger = logging.getLogger("test_type_validation")
    # str "60" would raise TypeError on comparison — must be caught as ValueError
    with pytest.raises(ValueError, match="cooldown_s"):
        tracker.log_stale(
            logger,
            collection_path=tmp_path / "repo",
            collection_name="voyage-3",
            alias=None,
            cooldown_s="60",  # type: ignore[arg-type]  # intentional wrong type for validation test
        )
    # None would raise TypeError on comparison — must be caught as ValueError
    with pytest.raises(ValueError, match="cooldown_s"):
        tracker.log_stale(
            logger,
            collection_path=tmp_path / "repo",
            collection_name="voyage-3",
            alias=None,
            cooldown_s=None,  # type: ignore[arg-type]  # intentional wrong type for validation test
        )


def test_invalid_escalate_after_type_raises_value_error(tmp_path):
    """log_stale raises ValueError (not TypeError) for non-numeric escalate_after_s.

    Both str and None are tested to cover the two common mis-use patterns.
    """
    tracker = HnswStaleTracker()
    logger = logging.getLogger("test_type_validation")
    # str "600" would raise TypeError on comparison — must be caught as ValueError
    with pytest.raises(ValueError, match="escalate_after_s"):
        tracker.log_stale(
            logger,
            collection_path=tmp_path / "repo",
            collection_name="voyage-3",
            alias=None,
            escalate_after_s="600",  # type: ignore[arg-type]  # intentional wrong type for validation test
        )
    # None would raise TypeError on comparison — must be caught as ValueError
    with pytest.raises(ValueError, match="escalate_after_s"):
        tracker.log_stale(
            logger,
            collection_path=tmp_path / "repo",
            collection_name="voyage-3",
            alias=None,
            escalate_after_s=None,  # type: ignore[arg-type]  # intentional wrong type for validation test
        )


# ---------------------------------------------------------------------------
# Test 11: clock, logger, collection_path, collection_name validation
# ---------------------------------------------------------------------------

def test_non_callable_clock_raises_value_error():
    """HnswStaleTracker raises ValueError when clock is not callable."""
    with pytest.raises(ValueError, match="clock"):
        HnswStaleTracker(clock="not_a_function")  # type: ignore[arg-type]  # intentional wrong type for validation test
    with pytest.raises(ValueError, match="clock"):
        HnswStaleTracker(clock=None)  # type: ignore[arg-type]  # intentional wrong type for validation test


def test_none_logger_raises_value_error(tmp_path):
    """log_stale raises ValueError when logger is None or missing required methods."""
    tracker = HnswStaleTracker()

    # None logger
    with pytest.raises(ValueError, match="logger"):
        tracker.log_stale(
            None,  # type: ignore[arg-type]  # intentional None for validation test
            collection_path=tmp_path / "repo",
            collection_name="voyage-3",
            alias=None,
        )

    class _NoWarning:
        """Logger stub missing the warning() method."""
        def error(self, msg): pass
        def debug(self, msg): pass

    with pytest.raises(ValueError, match="logger"):
        tracker.log_stale(
            _NoWarning(),
            collection_path=tmp_path / "repo",
            collection_name="voyage-3",
            alias=None,
        )

    class _NoDebug:
        """Logger stub missing the debug() method."""
        def error(self, msg): pass
        def warning(self, msg): pass

    with pytest.raises(ValueError, match="logger"):
        tracker.log_stale(
            _NoDebug(),
            collection_path=tmp_path / "repo",
            collection_name="voyage-3",
            alias=None,
        )


def test_none_collection_path_raises_value_error(tmp_path):
    """log_stale raises ValueError when collection_path is None."""
    tracker = HnswStaleTracker()
    logger = logging.getLogger("test_path_validation")
    with pytest.raises(ValueError, match="collection_path"):
        tracker.log_stale(
            logger,
            collection_path=None,  # type: ignore[arg-type]  # intentional None for validation test
            collection_name="voyage-3",
            alias=None,
        )


def test_none_or_empty_collection_name_raises_value_error(tmp_path):
    """log_stale raises ValueError when collection_name is None, empty, or non-str."""
    tracker = HnswStaleTracker()
    logger = logging.getLogger("test_name_validation")
    with pytest.raises(ValueError, match="collection_name"):
        tracker.log_stale(
            logger,
            collection_path=tmp_path / "repo",
            collection_name=None,  # type: ignore[arg-type]  # intentional None for validation test
            alias=None,
        )
    with pytest.raises(ValueError, match="collection_name"):
        tracker.log_stale(
            logger,
            collection_path=tmp_path / "repo",
            collection_name="",
            alias=None,
        )
    # Non-string type must also be rejected
    with pytest.raises(ValueError, match="collection_name"):
        tracker.log_stale(
            logger,
            collection_path=tmp_path / "repo",
            collection_name=123,  # type: ignore[arg-type]  # intentional wrong type for validation test
            alias=None,
        )


def test_invalid_collection_path_type_raises_value_error(tmp_path):
    """log_stale raises ValueError when collection_path is not str or PathLike."""
    tracker = HnswStaleTracker()
    logger = logging.getLogger("test_path_type_validation")
    # Integer is not str or PathLike
    with pytest.raises(ValueError, match="collection_path"):
        tracker.log_stale(
            logger,
            collection_path=42,  # type: ignore[arg-type]  # intentional wrong type for validation test
            collection_name="voyage-3",
            alias=None,
        )


def test_logger_methods_must_be_callable(tmp_path):
    """log_stale raises ValueError when logger has the method names but they are not callable."""
    tracker = HnswStaleTracker()

    class _NonCallableMethod:
        """Logger stub where 'error' is a non-callable attribute."""
        error = "not_a_function"   # attribute exists but is not callable
        warning = lambda self, msg: None  # noqa: E731
        debug = lambda self, msg: None    # noqa: E731

    with pytest.raises(ValueError, match="logger"):
        tracker.log_stale(
            _NonCallableMethod(),
            collection_path=tmp_path / "repo",
            collection_name="voyage-3",
            alias=None,
        )


def test_clock_returning_non_numeric_raises_value_error(tmp_path):
    """log_stale raises ValueError when the injected clock returns a non-numeric value."""
    tracker = HnswStaleTracker(clock=lambda: "not-a-number")  # type: ignore[return-value]  # intentional bad clock for validation test
    logger = logging.getLogger("test_clock_return_validation")
    with pytest.raises(ValueError, match="clock"):
        tracker.log_stale(
            logger,
            collection_path=tmp_path / "repo",
            collection_name="voyage-3",
            alias=None,
        )
