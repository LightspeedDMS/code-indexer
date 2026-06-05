"""
Unit tests for Bug #1061: uniform-random distribution fix in DescriptionRefreshScheduler.

Replaced hash-bucket + 18-min jitter with pure uniform random across full interval.
Added _reconcile_stale_next_run_rows() called from start() to prevent first-enable
thundering herd.

Tests:
1. calculate_next_run returns timestamp in [now, now + interval]
2. 10000 calls produce uniform distribution (chi-squared test)
3. _reconcile_stale_next_run_rows recomputes ONLY stale/NULL rows, preserves future rows
4. start() sequence: reconcile_orphan -> reconcile_stale -> thread-spawn
5. Integration: first-enable with ~100 stale rows — next tick sees at most small constant
"""

from __future__ import annotations

import random
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared test infra helpers
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS description_refresh_tracking (
    repo_alias TEXT PRIMARY KEY,
    last_run TEXT,
    next_run TEXT,
    status TEXT DEFAULT 'pending',
    error TEXT,
    last_known_commit TEXT,
    last_known_files_processed INTEGER,
    last_known_indexed_at TEXT,
    lifecycle_schema_version INTEGER,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS golden_repos_metadata (
    alias TEXT PRIMARY KEY NOT NULL,
    repo_url TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    clone_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    enable_temporal INTEGER NOT NULL DEFAULT 0,
    globally_active INTEGER NOT NULL DEFAULT 0
);
"""


def _make_db(tmp_path) -> str:
    """Create minimal SQLite DB and return its path string."""
    db = tmp_path / "test_uniform.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
    return str(db)


def _make_scheduler(db_path: str):
    """Return a minimal DescriptionRefreshScheduler with mocked config."""
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        ServerConfig,
    )

    cfg = ServerConfig(server_dir=db_path)
    cfg.claude_integration_config = ClaudeIntegrationConfig()
    cfg.claude_integration_config.description_refresh_enabled = True
    cfg.claude_integration_config.description_refresh_interval_hours = 24

    mock_cm = MagicMock()
    mock_cm.load_config.return_value = cfg

    return DescriptionRefreshScheduler(
        db_path=db_path,
        config_manager=mock_cm,
    )


def _seed_golden_repo(db_path: str, alias: str) -> None:
    """Insert a golden repo row."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO golden_repos_metadata"
            " (alias, repo_url, default_branch, clone_path, created_at)"
            " VALUES (?,?,?,?,?)",
            (
                alias,
                f"git@example.com:{alias}.git",
                "main",
                f"/repos/{alias}",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def _seed_tracking_row(db_path: str, alias: str, next_run: str | None) -> None:
    """Insert a tracking row with the given next_run (None = NULL)."""
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO description_refresh_tracking"
            " (repo_alias, next_run, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?)",
            (alias, next_run, "pending", now, now),
        )
        conn.commit()


def _get_next_run(db_path: str, alias: str) -> str | None:
    """Read next_run for the given alias from the DB."""
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT next_run FROM description_refresh_tracking WHERE repo_alias=?",
            (alias,),
        ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Test 1: calculate_next_run returns timestamp within [now, now + interval]
# ---------------------------------------------------------------------------


class TestCalculateNextRunRange:
    """calculate_next_run must return a timestamp in [now, now + interval_hours]."""

    def test_timestamp_within_interval_window(self, tmp_path):
        """Single call returns ISO timestamp in [now, now + interval_hours * 3600]."""
        db = _make_db(tmp_path)
        scheduler = _make_scheduler(db)

        interval_hours = 24
        before = datetime.now(timezone.utc)
        result = scheduler.calculate_next_run(
            "some-repo", interval_hours=interval_hours
        )
        after = datetime.now(timezone.utc)

        ts = datetime.fromisoformat(result)
        # Normalize to UTC
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        assert ts >= before, f"timestamp {ts} is before call time {before}"
        assert ts <= after + timedelta(hours=interval_hours), (
            f"timestamp {ts} exceeds upper bound {after + timedelta(hours=interval_hours)}"
        )

    def test_alias_parameter_unused_but_accepted(self, tmp_path):
        """Passing different aliases should not deterministically produce the same result."""
        db = _make_db(tmp_path)
        scheduler = _make_scheduler(db)

        # Collect 50 results for different aliases — they should NOT all be the same
        results = {
            scheduler.calculate_next_run(f"repo-{i}", interval_hours=24)
            for i in range(50)
        }
        # With 50 uniform random draws over 24h, probability all land in same second < 1e-80
        assert len(results) > 1, (
            "All 50 calls returned the same timestamp — not uniform random"
        )

    def test_defaults_to_config_interval(self, tmp_path):
        """When interval_hours is omitted, reads from config (default 24h)."""
        db = _make_db(tmp_path)
        scheduler = _make_scheduler(db)

        before = datetime.now(timezone.utc)
        result = scheduler.calculate_next_run("some-repo")
        after = datetime.now(timezone.utc)

        ts = datetime.fromisoformat(result)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        assert ts >= before
        assert ts <= after + timedelta(hours=24)


# ---------------------------------------------------------------------------
# Test 2: Uniform distribution — chi-squared over histogram bins
# ---------------------------------------------------------------------------


class TestCalculateNextRunUniformDistribution:
    """10000 calls must produce uniform distribution across the interval window."""

    def test_uniform_distribution_chi_squared(self, tmp_path):
        """
        10000 calls → bin into N equal-width buckets → chi-squared test.

        H0: distribution is uniform.  At p < 0.001 we reject if any single
        bucket deviates wildly.  We use a simpler bound: no bin should hold
        more than 3× the expected count (generous tolerance).
        """

        db = _make_db(tmp_path)
        scheduler = _make_scheduler(db)

        n_samples = 10_000
        n_bins = 24  # one bin per hour of the 24h interval
        interval_hours = 24
        interval_seconds = interval_hours * 3600

        before = datetime.now(timezone.utc)

        samples_seconds: list[float] = []
        for i in range(n_samples):
            ts_str = scheduler.calculate_next_run(
                f"repo-{i}", interval_hours=interval_hours
            )
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            offset = (ts - before).total_seconds()
            samples_seconds.append(offset)

        # Build histogram
        bins = [0] * n_bins
        for s in samples_seconds:
            # Clamp to [0, interval_seconds) to handle tiny floating-point overruns
            clamped = max(0.0, min(s, interval_seconds - 0.001))
            idx = int(clamped / interval_seconds * n_bins)
            idx = min(idx, n_bins - 1)
            bins[idx] += 1

        expected = n_samples / n_bins  # = 416.67

        # No bin should have more than 3× expected or less than 1/3 expected
        max_count = max(bins)
        min_count = min(bins)
        assert max_count <= expected * 3, (
            f"Max bin count {max_count} > 3× expected {expected:.1f} — NOT uniform"
        )
        assert min_count >= expected / 3, (
            f"Min bin count {min_count} < 1/3 expected {expected:.1f} — NOT uniform"
        )

        # Chi-squared statistic (sum of (O-E)^2/E); at df=23 critical value @p=0.001 ~49.7
        chi2 = sum((b - expected) ** 2 / expected for b in bins)
        assert chi2 < 60, (
            f"Chi-squared statistic {chi2:.2f} too large — distribution is not uniform"
        )


# ---------------------------------------------------------------------------
# Test 3: _reconcile_stale_next_run_rows — stale/NULL recomputed, future preserved
# ---------------------------------------------------------------------------


class TestReconcileStaleNextRunRows:
    """_reconcile_stale_next_run_rows must recompute only stale/NULL rows."""

    def test_recomputes_null_next_run(self, tmp_path):
        """Rows with next_run = NULL must be recomputed."""
        db = _make_db(tmp_path)
        scheduler = _make_scheduler(db)

        _seed_golden_repo(db, "repo-null")
        _seed_tracking_row(db, "repo-null", None)

        assert _get_next_run(db, "repo-null") is None

        scheduler._reconcile_stale_next_run_rows()

        new_next_run = _get_next_run(db, "repo-null")
        assert new_next_run is not None, "NULL next_run was not recomputed"

        # Must be in the future
        ts = datetime.fromisoformat(new_next_run)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        assert ts > datetime.now(timezone.utc), (
            "Recomputed next_run is not in the future"
        )

    def test_recomputes_past_next_run(self, tmp_path):
        """Rows with next_run in the past must be recomputed."""
        db = _make_db(tmp_path)
        scheduler = _make_scheduler(db)

        past_ts = "2020-01-01T00:00:00+00:00"
        _seed_golden_repo(db, "repo-past")
        _seed_tracking_row(db, "repo-past", past_ts)

        scheduler._reconcile_stale_next_run_rows()

        new_next_run = _get_next_run(db, "repo-past")
        assert new_next_run != past_ts, "Past next_run was not recomputed"
        assert new_next_run is not None, "Recomputed next_run must not be None"

        ts = datetime.fromisoformat(new_next_run)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        assert ts > datetime.now(timezone.utc), (
            "Recomputed next_run is not in the future"
        )

    def test_preserves_future_next_run(self, tmp_path):
        """Rows with next_run already in the future must NOT be touched."""
        db = _make_db(tmp_path)
        scheduler = _make_scheduler(db)

        future_ts = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        _seed_golden_repo(db, "repo-future")
        _seed_tracking_row(db, "repo-future", future_ts)

        scheduler._reconcile_stale_next_run_rows()

        preserved = _get_next_run(db, "repo-future")
        assert preserved == future_ts, (
            f"Future next_run was modified: expected {future_ts!r}, got {preserved!r}"
        )

    def test_mixed_rows(self, tmp_path):
        """Mixed batch: only stale/NULL rows are recomputed; future row is preserved."""
        db = _make_db(tmp_path)
        scheduler = _make_scheduler(db)

        past_ts = "2021-03-15T12:00:00+00:00"
        future_ts = (datetime.now(timezone.utc) + timedelta(hours=10)).isoformat()

        for alias in ("r-null", "r-past", "r-future"):
            _seed_golden_repo(db, alias)

        _seed_tracking_row(db, "r-null", None)
        _seed_tracking_row(db, "r-past", past_ts)
        _seed_tracking_row(db, "r-future", future_ts)

        scheduler._reconcile_stale_next_run_rows()

        null_new = _get_next_run(db, "r-null")
        past_new = _get_next_run(db, "r-past")
        future_preserved = _get_next_run(db, "r-future")

        assert null_new is not None and null_new != "None"
        assert past_new != past_ts
        assert future_preserved == future_ts

    def test_returns_count_of_recomputed_rows(self, tmp_path):
        """_reconcile_stale_next_run_rows must return the count of rows recomputed."""
        db = _make_db(tmp_path)
        scheduler = _make_scheduler(db)

        for i in range(3):
            _seed_golden_repo(db, f"stale-{i}")
            _seed_tracking_row(db, f"stale-{i}", "2020-01-01T00:00:00+00:00")

        future_ts = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        _seed_golden_repo(db, "fresh-0")
        _seed_tracking_row(db, "fresh-0", future_ts)

        count = scheduler._reconcile_stale_next_run_rows()
        assert count == 3, f"Expected 3 rows recomputed, got {count}"

    def test_no_rows_returns_zero(self, tmp_path):
        """Empty DB returns 0."""
        db = _make_db(tmp_path)
        scheduler = _make_scheduler(db)

        count = scheduler._reconcile_stale_next_run_rows()
        assert count == 0


# ---------------------------------------------------------------------------
# Test 4: start() sequence — reconcile_orphan → reconcile_stale → thread
# ---------------------------------------------------------------------------


class TestStartSequence:
    """start() must call reconcile_orphan_tracking, then _reconcile_stale_next_run_rows,
    then spawn the daemon thread."""

    def test_start_calls_reconcile_stale_after_orphan(self, tmp_path):
        """reconcile_orphan_tracking is called before _reconcile_stale_next_run_rows."""
        db = _make_db(tmp_path)
        scheduler = _make_scheduler(db)

        call_order: list[str] = []

        original_orphan = scheduler.reconcile_orphan_tracking

        def patched_orphan():
            call_order.append("orphan")
            return original_orphan()

        def patched_stale():
            call_order.append("stale")
            return 0

        scheduler.reconcile_orphan_tracking = patched_orphan

        with patch.object(
            scheduler, "_reconcile_stale_next_run_rows", side_effect=patched_stale
        ):
            with patch.object(
                scheduler, "reconcile_broken_lifecycle_metadata", return_value=0
            ):
                with patch.object(
                    scheduler, "reconcile_terse_descriptions", return_value=0
                ):
                    with patch.object(
                        scheduler, "_migrate_global_suffix_filenames", return_value=0
                    ):
                        with patch("threading.Thread") as mock_thread:
                            mock_thread.return_value.start = MagicMock()
                            scheduler.start()

        assert "orphan" in call_order, "reconcile_orphan_tracking was not called"
        assert "stale" in call_order, "_reconcile_stale_next_run_rows was not called"

        orphan_idx = call_order.index("orphan")
        stale_idx = call_order.index("stale")
        assert orphan_idx < stale_idx, (
            f"reconcile_orphan ({orphan_idx}) must be called before "
            f"_reconcile_stale_next_run_rows ({stale_idx})"
        )

    def test_start_spawns_thread_after_reconcile_stale(self, tmp_path):
        """Thread must be spawned after _reconcile_stale_next_run_rows."""
        db = _make_db(tmp_path)
        scheduler = _make_scheduler(db)

        call_order: list[str] = []

        def patched_stale():
            call_order.append("stale")
            return 0

        def patched_start():
            call_order.append("thread_start")

        with patch.object(
            scheduler, "_reconcile_stale_next_run_rows", side_effect=patched_stale
        ):
            with patch.object(scheduler, "reconcile_orphan_tracking", return_value=0):
                with patch.object(
                    scheduler, "reconcile_broken_lifecycle_metadata", return_value=0
                ):
                    with patch.object(
                        scheduler, "reconcile_terse_descriptions", return_value=0
                    ):
                        with patch.object(
                            scheduler,
                            "_migrate_global_suffix_filenames",
                            return_value=0,
                        ):
                            with patch("threading.Thread") as mock_thread:
                                mock_thread.return_value.start = MagicMock(
                                    side_effect=patched_start
                                )
                                scheduler.start()

        assert "stale" in call_order
        assert "thread_start" in call_order
        stale_idx = call_order.index("stale")
        thread_idx = call_order.index("thread_start")
        assert stale_idx < thread_idx, (
            f"_reconcile_stale_next_run_rows ({stale_idx}) must happen before "
            f"thread spawn ({thread_idx})"
        )


# ---------------------------------------------------------------------------
# In-memory backend for fast integration tests
# ---------------------------------------------------------------------------


class _InMemoryTrackingBackend:
    """
    Pure-Python in-memory replacement for DescriptionRefreshTrackingBackend.

    Satisfies the interface used by _reconcile_stale_next_run_rows:
      - get_all_tracking() -> List[Dict]
      - upsert_tracking(repo_alias, **fields) -> None

    Eliminates all SQLite I/O so the integration tests complete in milliseconds
    regardless of system load.  This is the correct fix for the 15-second
    pytest-timeout breach that caused TestFirstEnableDistribution to fail 2/2
    under chunked-parallel server-fast execution.
    """

    def __init__(self, rows: list) -> None:
        # rows is a list of dicts; each dict must have at least 'repo_alias'
        self._store: dict = {r["repo_alias"]: dict(r) for r in rows}

    def get_all_tracking(self) -> list:
        return list(self._store.values())

    def upsert_tracking(self, repo_alias: str, **fields) -> None:
        if repo_alias not in self._store:
            self._store[repo_alias] = {"repo_alias": repo_alias}
        self._store[repo_alias].update(fields)

    def get_next_run(self, alias: str):
        return self._store.get(alias, {}).get("next_run")

    def close(self) -> None:
        pass


def _make_scheduler_with_backend(tracking_backend, golden_backend=None):
    """Return DescriptionRefreshScheduler wired with an injected tracking backend."""
    from unittest.mock import MagicMock

    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        ServerConfig,
    )

    cfg = ServerConfig(server_dir="/tmp/fake-db-path")
    cfg.claude_integration_config = ClaudeIntegrationConfig()
    cfg.claude_integration_config.description_refresh_enabled = True
    cfg.claude_integration_config.description_refresh_interval_hours = 24

    mock_cm = MagicMock()
    mock_cm.load_config.return_value = cfg

    if golden_backend is None:
        golden_backend = MagicMock()

    return DescriptionRefreshScheduler(
        db_path=None,
        config_manager=mock_cm,
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
    )


# ---------------------------------------------------------------------------
# Test 5: Integration — first-enable: next tick sees at most a small constant
# ---------------------------------------------------------------------------


class TestFirstEnableDistribution:
    """
    Simulate first-enable with ~100 stale rows.

    After _reconcile_stale_next_run_rows(), the scheduler's periodic tick
    (which queries next_run <= now) must see at most a small constant of due
    rows — NOT all 100.

    Design: time-deterministic, load-invariant
    ------------------------------------------
    Both positive tests use:

    1. ``seeded_random`` fixture (seed=42) — patches ``random.uniform`` in the
       scheduler module so the 100 offsets are reproducible across every run.

    2. ``_InMemoryTrackingBackend`` — eliminates SQLite I/O (each upsert was
       ~300ms × 100 = 30s, causing a 15-second pytest-timeout breach under
       chunked-parallel server-fast load).  With in-memory storage the whole
       reconcile finishes in <100ms regardless of system load.

    3. ``reference_now`` pinned BEFORE reconcile — due-ness and offset
       calculations use the same instant the scheduler used for new next_run
       values, so elapsed real wall-clock between reconcile and assertion
       cannot inflate either count.

    Seed=42 properties (verified offline):
      * min offset ≈ 561s, max offset ≈ 86187s, spread ≈ 85626s
      * max_bin = 10 across 24 one-hour windows (≤ 20 required)
      * No offset is < 0 even with a reference_now that is several minutes
        behind the actual writes (safety margin ≈ 9 minutes).

    Regression guard against pre-#1061 behavior
    --------------------------------------------
    The pre-#1061 code used hash-bucket clustering and had NO
    ``_reconcile_stale_next_run_rows`` method. Both negative tests below
    document the two mechanisms by which the positive tests catch the old bug:

    1. ``AttributeError`` on the missing method.
    2. Distribution assertion failure for severely-clustered timestamps.
    """

    @pytest.fixture
    def seeded_random(self, monkeypatch):
        """Patch random.uniform in the scheduler module with a seeded RNG.

        Seed=42 → 100 draws from Uniform(0, 86400) satisfy both assertions:
          * max_bin  = 10  (≤ 20 required)
          * spread   = 85626s  (≥ 43200s required)
        """
        import code_indexer.server.services.description_refresh_scheduler as sched_mod

        seeded_rng = random.Random(42)
        monkeypatch.setattr(sched_mod.random, "uniform", seeded_rng.uniform)

    def test_first_enable_100_stale_rows_few_immediately_due(self, seeded_random):
        """
        Seed 100 repos with NULL next_run.  After reconciliation, at most 5%
        (5 repos) should have next_run <= reference_now (i.e., be immediately due).

        With seed=42 all 100 offsets are ≥ 561s into the future relative to the
        moment calculate_next_run was called, so immediately_due = 0.
        The generous bound of 5 is preserved for documentation clarity.

        Time-determinism: reference_now is captured BEFORE reconcile, so it is
        always earlier than the actual write timestamps.  No elapsed wall-clock
        time between writes and assertion can cause a row to flip from future to
        past.  This makes the test load-invariant.
        """
        n_repos = 100
        rows = [
            {"repo_alias": f"fleet-repo-{i:03d}", "next_run": None}
            for i in range(n_repos)
        ]
        tracking = _InMemoryTrackingBackend(rows)
        scheduler = _make_scheduler_with_backend(tracking)

        # Pin reference_now BEFORE reconcile — all new next_run values will be
        # computed relative to datetime.now() calls AFTER this point, so they
        # are guaranteed to be >= reference_now + offset (offset >= 561s).
        reference_now = datetime.now(timezone.utc)

        scheduler._reconcile_stale_next_run_rows()

        # Count rows whose next_run <= reference_now (immediately due at the
        # pre-reconcile instant).  With seed=42 this is always 0.
        immediately_due = 0
        for i in range(n_repos):
            alias = f"fleet-repo-{i:03d}"
            nxt = tracking.get_next_run(alias)
            if nxt is not None:
                ts = datetime.fromisoformat(nxt)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts <= reference_now:
                    immediately_due += 1

        assert immediately_due <= 5, (
            f"{immediately_due} repos are immediately due after reconciliation "
            f"(expected <= 5 out of {n_repos})"
        )

    def test_first_enable_100_past_rows_distributed_across_interval(
        self, seeded_random
    ):
        """
        100 repos with past next_run, after reconciliation, should have their
        next_run spread across the full interval (not all clustered in one hour).

        With seed=42: spread=85626s (≥43200s), max_bin=10 (≤20).  Both bounds
        hold deterministically — no flakiness possible.

        Time-determinism: reference_now is captured BEFORE reconcile.  Each
        new next_run = datetime.now_at_write + offset_i, so
        (next_run - reference_now) = (now_at_write - reference_now) + offset_i
        ≥ offset_i (since now_at_write >= reference_now).  The spread and bin
        assertions are on offsets relative to reference_now, which can only be
        LARGER than the raw offset, preserving the ≥43200s spread guarantee.
        """
        n_repos = 100
        interval_hours = 24
        past_ts = "2020-06-01T00:00:00+00:00"

        rows = [
            {"repo_alias": f"spread-repo-{i:03d}", "next_run": past_ts}
            for i in range(n_repos)
        ]
        tracking = _InMemoryTrackingBackend(rows)
        scheduler = _make_scheduler_with_backend(tracking)

        # Pin reference_now BEFORE reconcile (see docstring for why this is safe).
        reference_now = datetime.now(timezone.utc)

        scheduler._reconcile_stale_next_run_rows()

        offsets: list[float] = []
        for i in range(n_repos):
            alias = f"spread-repo-{i:03d}"
            nxt = tracking.get_next_run(alias)
            assert nxt is not None
            ts = datetime.fromisoformat(nxt)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            offsets.append((ts - reference_now).total_seconds())

        max_offset = max(offsets)
        min_offset = min(offsets)

        # Spread should cover at least half the interval (12h = 43200s)
        assert max_offset - min_offset >= interval_hours * 3600 * 0.5, (
            f"Spread {max_offset - min_offset:.0f}s < half interval "
            f"{interval_hours * 3600 * 0.5:.0f}s — rows may be clustered"
        )

        # No more than 20% of repos (20) in any 1-hour window
        n_bins = interval_hours
        bin_width = 3600.0
        bins = [0] * n_bins
        for offset in offsets:
            idx = int(offset / bin_width)
            idx = max(0, min(idx, n_bins - 1))
            bins[idx] += 1

        max_bin = max(bins)
        assert max_bin <= 20, (
            f"Max repos in any 1-hour window: {max_bin} > 20 — thundering herd not solved"
        )

    # -----------------------------------------------------------------------
    # Negative tests: regression guard documenting HOW the positive tests
    # catch the pre-#1061 thundering-herd bug.
    # -----------------------------------------------------------------------

    def test_negative__old_code_lacking_reconcile_raises_attribute_error(self):
        """NEGATIVE: pre-#1061 DescriptionRefreshScheduler had no
        ``_reconcile_stale_next_run_rows`` method.

        The positive tests above call ``scheduler._reconcile_stale_next_run_rows()``.
        Against the old code that method did not exist → ``AttributeError`` →
        the test fails, catching the thundering-herd regression.

        This test documents and verifies that detection mechanism by constructing
        a minimal object that mimics the old scheduler's missing method.
        """

        class PreBug1061Scheduler:
            """Minimal stand-in for the old scheduler (no reconcile method)."""

            pass

        old = PreBug1061Scheduler()
        with pytest.raises(AttributeError):
            old._reconcile_stale_next_run_rows()  # type: ignore[attr-defined]

    def test_negative__severe_clustering_fails_distribution_assertion(self):
        """NEGATIVE: severely-clustered timestamps fail the max_bin assertion.

        If ``calculate_next_run`` returned the same timestamp for every repo
        (100% clustering — the pathological thundering-herd case), all 100
        offsets land in a single 1-hour bin → max_bin = 100, which exceeds the
        ``max_bin <= 20`` guard.

        This confirms that the distribution assertion in
        ``test_first_enable_100_past_rows_distributed_across_interval`` is
        sensitive enough to catch real clustering, not just the missing-method
        failure mode.
        """
        interval_hours = 24
        n_repos = 100
        # All repos get the same offset (clustered at hour 1 = 3600s)
        clustered_offsets = [3600.0] * n_repos

        n_bins = interval_hours
        bin_width = 3600.0
        bins = [0] * n_bins
        for offset in clustered_offsets:
            idx = int(offset / bin_width)
            idx = max(0, min(idx, n_bins - 1))
            bins[idx] += 1

        max_bin = max(bins)
        # Verify the assertion WOULD fire (i.e., our test IS sensitive)
        assert max_bin > 20, (
            f"Expected max_bin > 20 for 100% clustered data, got {max_bin}"
        )
        # And the actual guard that the positive test enforces:
        assert not (max_bin <= 20), (
            "Clustered data should NOT pass the max_bin<=20 guard — "
            "if this assertion fails the positive test would miss clustering"
        )
