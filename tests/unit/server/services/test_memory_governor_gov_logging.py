"""Story 4 — Part 4: GOV-001..005 structured log codes land in logs.db.

Each test verifies that a specific GOV-* log record is emitted by the
appropriate governor method/transition AND is written to the SQLite log
store that admin_logs_query reads (i.e., the SQLiteLogHandler sink).

Tests:
- GOV-001 emitted on a real band transition (GREEN->YELLOW).
- GOV-002 emitted by log_gov002_evict() and lands in logs.db.
- GOV-002 is rate-limited (20 rapid calls produce ≤ max allowed entries).
- GOV-003 emitted by log_gov003_lru_evict() and lands in logs.db.
- GOV-004 emitted by log_gov004_trim() and lands in logs.db.
- GOV-005 emitted when pswpin_rate > 0 forces RED.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.memory_governor import (
    MemoryBand,
    MemoryGovernor,
)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

BYTES_PER_GIB = 1024 * 1024 * 1024
HOST_100_GIB = 100 * BYTES_PER_GIB

# Memory usage percentages
GREEN_USAGE_PCT = 30.0
# YELLOW_USAGE_PCT must be >= yellow_pct(70) AND < red_exit(=red_pct-hysteresis=75).
# 75.0 is NOT strictly less than red_exit so the governor stays RED.  Use 72.0.
YELLOW_USAGE_PCT = 72.0
RED_USAGE_PCT = 90.0

# Watermarks
YELLOW_PCT_DEFAULT = 70.0
RED_PCT_DEFAULT = 85.0
HYSTERESIS_PCT_DEFAULT = 10.0
NO_RED_DWELL_SECONDS = 0.0

# Swap pages
NO_SWAP_PAGES_IN = 0
SWAP_PAGES_DELTA = 100  # non-zero swap-in rate that forces RED
PREV_PSWPIN_BASELINE = 0  # baseline before the swap event

# GOV-002 rate-limit: 20 rapid calls should emit no more than this many entries
GOV002_RAPID_CALL_COUNT = 20
GOV002_MAX_ALLOWED_LOG_ENTRIES = 5

# Test values for log_gov003_lru_evict
LRU_EVICT_COUNT = 3
LRU_FREED_MB = 120.0

# Test values for log_gov002_evict
EVICT_FREED_MB = 42.5
EVICT_SHARD_NAME = "tests/shard_2024_Q1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_readers(used_pct: float, pswpin: int = NO_SWAP_PAGES_IN) -> MagicMock:
    readers = MagicMock()
    vm = MagicMock()
    vm.total = HOST_100_GIB
    vm.used = int(HOST_100_GIB * used_pct / 100)
    readers.read_host_memory.return_value = vm
    readers.read_cgroup_v2_max.side_effect = FileNotFoundError
    readers.read_cgroup_v1_limit.side_effect = FileNotFoundError
    readers.read_pswpin.return_value = pswpin
    return readers


def _red_gov() -> MemoryGovernor:
    gov = MemoryGovernor(
        readers=_make_readers(RED_USAGE_PCT),
        enabled=True,
        start_sampler=False,
        yellow_pct=YELLOW_PCT_DEFAULT,
        red_pct=RED_PCT_DEFAULT,
        hysteresis_pct=HYSTERESIS_PCT_DEFAULT,
        red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
    )
    gov._tick()
    assert gov.band == MemoryBand.RED
    return gov


def _query_gov_rows(db_path: Path) -> list:
    """Return all rows with message LIKE 'GOV-%' from the log store."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT level, source, message FROM logs "
            "WHERE message LIKE 'GOV-%' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def log_db(tmp_path: Path):
    """Install a SQLiteLogHandler to a temp logs.db, yield the db path, then clean up.

    Also temporarily lowers the root logger level to DEBUG so that INFO-level
    GOV-* messages (which the governor emits) are not filtered before reaching
    the handler.  The original level is restored in teardown.
    """
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    db_path = tmp_path / "logs.db"
    handler = SQLiteLogHandler(db_path)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    original_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    yield db_path, handler
    root.removeHandler(handler)
    root.setLevel(original_level)
    handler.flush()
    handler.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGovStructuredLogs:
    """GOV-001..005 log codes land in the SQLiteLogHandler log store."""

    def test_gov001_band_transition_logged(self, log_db):
        """GOV-001 is emitted to logs.db on a real GREEN->YELLOW band transition."""
        db_path, handler = log_db
        gov = MemoryGovernor(
            readers=_make_readers(GREEN_USAGE_PCT),
            enabled=True,
            start_sampler=False,
            yellow_pct=YELLOW_PCT_DEFAULT,
            red_pct=RED_PCT_DEFAULT,
            hysteresis_pct=HYSTERESIS_PCT_DEFAULT,
            red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
        )
        gov._tick()  # first tick: fail-safe RED -> GREEN (first_tick, no counter)
        assert gov.band == MemoryBand.GREEN

        gov._readers = _make_readers(YELLOW_USAGE_PCT)  # type: ignore[attr-defined]
        gov._tick()  # real operational GREEN->YELLOW transition
        assert gov.band == MemoryBand.YELLOW
        assert gov.counters.green_to_yellow >= 1

        handler.flush()
        rows = _query_gov_rows(db_path)
        gov001 = [r for r in rows if r[2].startswith("GOV-001")]
        assert len(gov001) >= 1, f"GOV-001 not in logs.db. All GOV rows: {rows}"

    def test_gov002_red_evict_logged(self, log_db):
        """GOV-002 lands in logs.db when log_gov002_evict() is called."""
        db_path, handler = log_db
        gov = _red_gov()
        gov.log_gov002_evict(shard=EVICT_SHARD_NAME, freed_mb=EVICT_FREED_MB)
        handler.flush()
        rows = _query_gov_rows(db_path)
        gov002 = [r for r in rows if r[2].startswith("GOV-002")]
        assert len(gov002) >= 1, "GOV-002 not in logs.db"

    def test_gov002_rate_limited(self, log_db):
        """GOV-002 is rate-limited: rapid calls emit no more than GOV002_MAX_ALLOWED_LOG_ENTRIES."""
        db_path, handler = log_db
        gov = _red_gov()
        for i in range(GOV002_RAPID_CALL_COUNT):
            gov.log_gov002_evict(shard=f"shard_{i}", freed_mb=1.0)
        handler.flush()
        rows = _query_gov_rows(db_path)
        gov002 = [r for r in rows if r[2].startswith("GOV-002")]
        assert len(gov002) <= GOV002_MAX_ALLOWED_LOG_ENTRIES, (
            f"GOV-002 not rate-limited: {len(gov002)} entries emitted for "
            f"{GOV002_RAPID_CALL_COUNT} calls (max allowed: {GOV002_MAX_ALLOWED_LOG_ENTRIES})"
        )

    def test_gov003_yellow_lru_evict_logged(self, log_db):
        """GOV-003 lands in logs.db when log_gov003_lru_evict() is called."""
        db_path, handler = log_db
        gov = MemoryGovernor(
            readers=_make_readers(YELLOW_USAGE_PCT),
            enabled=True,
            start_sampler=False,
            yellow_pct=YELLOW_PCT_DEFAULT,
            red_pct=RED_PCT_DEFAULT,
            hysteresis_pct=HYSTERESIS_PCT_DEFAULT,
            red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
        )
        gov._tick()
        assert gov.band == MemoryBand.YELLOW
        gov.log_gov003_lru_evict(count=LRU_EVICT_COUNT, freed_mb=LRU_FREED_MB)
        handler.flush()
        rows = _query_gov_rows(db_path)
        gov003 = [r for r in rows if r[2].startswith("GOV-003")]
        assert len(gov003) >= 1, "GOV-003 not in logs.db"

    def test_gov004_malloc_trim_logged(self, log_db):
        """GOV-004 lands in logs.db when log_gov004_trim() is called."""
        db_path, handler = log_db
        gov = _red_gov()
        gov.log_gov004_trim(released=True)
        handler.flush()
        rows = _query_gov_rows(db_path)
        gov004 = [r for r in rows if r[2].startswith("GOV-004")]
        assert len(gov004) >= 1, "GOV-004 not in logs.db"

    def test_gov005_swap_in_forced_red_logged(self, log_db):
        """GOV-005 lands in logs.db when pswpin_rate > 0 forces the band to RED."""
        db_path, handler = log_db
        gov = MemoryGovernor(
            readers=_make_readers(GREEN_USAGE_PCT, pswpin=NO_SWAP_PAGES_IN),
            enabled=True,
            start_sampler=False,
            yellow_pct=YELLOW_PCT_DEFAULT,
            red_pct=RED_PCT_DEFAULT,
            hysteresis_pct=HYSTERESIS_PCT_DEFAULT,
            red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
            swap_forces_red=True,
        )
        gov._tick()
        assert gov.band == MemoryBand.GREEN

        # Inject swap-in activity (delta = SWAP_PAGES_DELTA > 0)
        gov._readers = _make_readers(GREEN_USAGE_PCT, pswpin=SWAP_PAGES_DELTA)  # type: ignore[attr-defined]
        gov._prev_pswpin = PREV_PSWPIN_BASELINE
        gov._tick()
        assert gov.band == MemoryBand.RED

        handler.flush()
        rows = _query_gov_rows(db_path)
        gov005 = [r for r in rows if r[2].startswith("GOV-005")]
        assert len(gov005) >= 1, f"GOV-005 not in logs.db. All GOV rows: {rows}"
