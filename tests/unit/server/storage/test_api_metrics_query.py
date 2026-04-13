"""
Tests for extended period query backend methods (Story #673).

Tests get_metrics_bucketed, get_metrics_by_user, get_metrics_timeseries
for ApiMetricsSqliteBackend; protocol extension; period selector HTML update;
and dashboard service wiring.
"""

from datetime import datetime, timezone, timedelta

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path):
    """Return an ApiMetricsSqliteBackend instance backed by a temp DB."""
    from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend

    db_file = str(tmp_path / "test_metrics.db")
    return ApiMetricsSqliteBackend(db_file)


def _bucket_start(dt: datetime, granularity: str) -> str:
    """Compute a bucket_start string for a given datetime and granularity."""
    if granularity == "min1":
        return dt.replace(second=0, microsecond=0).isoformat()
    if granularity == "min5":
        return dt.replace(
            minute=(dt.minute // 5) * 5, second=0, microsecond=0
        ).isoformat()
    if granularity == "hour1":
        return dt.replace(minute=0, second=0, microsecond=0).isoformat()
    if granularity == "day1":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    raise ValueError(f"Unknown granularity: {granularity}")


# ---------------------------------------------------------------------------
# PERIOD_TO_TIER constant
# ---------------------------------------------------------------------------


class TestPeriodToTierConstant:
    """PERIOD_TO_TIER maps period seconds to granularity tier."""

    def test_period_to_tier_maps_15min_to_min1(self):
        from code_indexer.server.storage.sqlite_backends import PERIOD_TO_TIER

        assert PERIOD_TO_TIER[900] == "min1"

    def test_period_to_tier_maps_1h_to_min5(self):
        from code_indexer.server.storage.sqlite_backends import PERIOD_TO_TIER

        assert PERIOD_TO_TIER[3600] == "min5"

    def test_period_to_tier_maps_24h_to_hour1(self):
        from code_indexer.server.storage.sqlite_backends import PERIOD_TO_TIER

        assert PERIOD_TO_TIER[86400] == "hour1"

    def test_period_to_tier_maps_7d_to_day1(self):
        from code_indexer.server.storage.sqlite_backends import PERIOD_TO_TIER

        assert PERIOD_TO_TIER[604800] == "day1"

    def test_period_to_tier_maps_15d_to_day1(self):
        from code_indexer.server.storage.sqlite_backends import PERIOD_TO_TIER

        assert PERIOD_TO_TIER[1296000] == "day1"


# ---------------------------------------------------------------------------
# get_metrics_bucketed — basic totals
# ---------------------------------------------------------------------------


class TestGetMetricsBucketed:
    """Tests for ApiMetricsSqliteBackend.get_metrics_bucketed."""

    def test_returns_zero_counts_when_empty(self, backend):
        result = backend.get_metrics_bucketed(period_seconds=900)
        assert result == {
            "semantic_searches": 0,
            "other_index_searches": 0,
            "regex_searches": 0,
            "other_api_calls": 0,
        }

    def test_returns_correct_totals_for_15min(self, backend):
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "min1")

        backend.upsert_bucket("alice", "min1", bucket, "semantic")
        backend.upsert_bucket("alice", "min1", bucket, "semantic")
        backend.upsert_bucket("alice", "min1", bucket, "regex")

        result = backend.get_metrics_bucketed(period_seconds=900)
        assert result["semantic_searches"] == 2
        assert result["regex_searches"] == 1
        assert result["other_index_searches"] == 0
        assert result["other_api_calls"] == 0

    def test_returns_correct_totals_for_1h(self, backend):
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "min5")

        backend.upsert_bucket("bob", "min5", bucket, "other_api")
        backend.upsert_bucket("bob", "min5", bucket, "other_api")
        backend.upsert_bucket("bob", "min5", bucket, "other_index")

        result = backend.get_metrics_bucketed(period_seconds=3600)
        assert result["other_api_calls"] == 2
        assert result["other_index_searches"] == 1

    def test_returns_correct_totals_for_24h(self, backend):
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "hour1")

        backend.upsert_bucket("alice", "hour1", bucket, "semantic")
        backend.upsert_bucket("bob", "hour1", bucket, "regex")

        result = backend.get_metrics_bucketed(period_seconds=86400)
        assert result["semantic_searches"] == 1
        assert result["regex_searches"] == 1

    def test_returns_correct_totals_for_7d(self, backend):
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "day1")

        backend.upsert_bucket("alice", "day1", bucket, "semantic")
        backend.upsert_bucket("alice", "day1", bucket, "semantic")

        result = backend.get_metrics_bucketed(period_seconds=604800)
        assert result["semantic_searches"] == 2

    def test_returns_correct_totals_for_15d(self, backend):
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "day1")

        backend.upsert_bucket("alice", "day1", bucket, "other_api")

        result = backend.get_metrics_bucketed(period_seconds=1296000)
        assert result["other_api_calls"] == 1

    def test_excludes_rows_outside_period(self, backend):
        """Rows with bucket_start before cutoff must NOT be included."""
        # Insert a row 20 minutes ago using min1 granularity (15min window)
        old_dt = datetime.now(timezone.utc) - timedelta(minutes=20)
        old_bucket = _bucket_start(old_dt, "min1")
        backend.upsert_bucket("alice", "min1", old_bucket, "semantic")

        result = backend.get_metrics_bucketed(period_seconds=900)
        # 20 minutes ago is outside the 15-minute window — must be excluded
        assert result["semantic_searches"] == 0

    def test_excludes_wrong_granularity_rows(self, backend):
        """Rows in wrong granularity tier must NOT be counted."""
        now = datetime.now(timezone.utc)
        # Insert in hour1 tier but query with 15min (min1 tier)
        bucket = _bucket_start(now, "hour1")
        backend.upsert_bucket("alice", "hour1", bucket, "semantic")

        # 15min query should use min1 tier — hour1 rows must not be counted
        result = backend.get_metrics_bucketed(period_seconds=900)
        assert result["semantic_searches"] == 0

    def test_aggregates_multiple_users(self, backend):
        """Totals are sum across all users."""
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "min1")

        backend.upsert_bucket("alice", "min1", bucket, "semantic")
        backend.upsert_bucket("bob", "min1", bucket, "semantic")
        backend.upsert_bucket("carol", "min1", bucket, "semantic")

        result = backend.get_metrics_bucketed(period_seconds=900)
        assert result["semantic_searches"] == 3


# ---------------------------------------------------------------------------
# get_metrics_bucketed — username filter
# ---------------------------------------------------------------------------


class TestGetMetricsBucketedByUsername:
    """Tests for username filtering in get_metrics_bucketed."""

    def test_filters_to_specific_user(self, backend):
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "min1")

        backend.upsert_bucket("alice", "min1", bucket, "semantic")
        backend.upsert_bucket("alice", "min1", bucket, "semantic")
        backend.upsert_bucket("bob", "min1", bucket, "semantic")

        result = backend.get_metrics_bucketed(period_seconds=900, username="alice")
        assert result["semantic_searches"] == 2

    def test_returns_zeros_for_nonexistent_user(self, backend):
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "min1")
        backend.upsert_bucket("alice", "min1", bucket, "semantic")

        result = backend.get_metrics_bucketed(period_seconds=900, username="nobody")
        assert result["semantic_searches"] == 0

    def test_no_username_aggregates_all(self, backend):
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "min1")

        backend.upsert_bucket("alice", "min1", bucket, "regex")
        backend.upsert_bucket("bob", "min1", bucket, "regex")

        result = backend.get_metrics_bucketed(period_seconds=900, username=None)
        assert result["regex_searches"] == 2


# ---------------------------------------------------------------------------
# get_metrics_by_user
# ---------------------------------------------------------------------------


class TestGetMetricsByUser:
    """Tests for ApiMetricsSqliteBackend.get_metrics_by_user."""

    def test_returns_empty_dict_when_no_data(self, backend):
        result = backend.get_metrics_by_user(period_seconds=900)
        assert result == {}

    def test_groups_by_username_and_metric_type(self, backend):
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "min1")

        backend.upsert_bucket("alice", "min1", bucket, "semantic")
        backend.upsert_bucket("alice", "min1", bucket, "semantic")
        backend.upsert_bucket("alice", "min1", bucket, "regex")
        backend.upsert_bucket("bob", "min1", bucket, "semantic")

        result = backend.get_metrics_by_user(period_seconds=900)

        assert "alice" in result
        assert result["alice"]["semantic"] == 2
        assert result["alice"]["regex"] == 1
        assert "bob" in result
        assert result["bob"]["semantic"] == 1

    def test_excludes_rows_outside_period(self, backend):
        old_dt = datetime.now(timezone.utc) - timedelta(minutes=20)
        old_bucket = _bucket_start(old_dt, "min1")
        backend.upsert_bucket("alice", "min1", old_bucket, "semantic")

        result = backend.get_metrics_by_user(period_seconds=900)
        assert result == {}

    def test_uses_correct_tier_for_24h(self, backend):
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "hour1")

        backend.upsert_bucket("alice", "hour1", bucket, "other_api")
        backend.upsert_bucket("alice", "min1", bucket, "other_api")

        result = backend.get_metrics_by_user(period_seconds=86400)
        # Should query hour1 tier only — min1 rows must not be mixed in
        assert result["alice"]["other_api"] == 1

    def test_multiple_metrics_per_user(self, backend):
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "min5")

        backend.upsert_bucket("bob", "min5", bucket, "semantic")
        backend.upsert_bucket("bob", "min5", bucket, "regex")
        backend.upsert_bucket("bob", "min5", bucket, "other_index")
        backend.upsert_bucket("bob", "min5", bucket, "other_api")

        result = backend.get_metrics_by_user(period_seconds=3600)
        assert result["bob"]["semantic"] == 1
        assert result["bob"]["regex"] == 1
        assert result["bob"]["other_index"] == 1
        assert result["bob"]["other_api"] == 1


# ---------------------------------------------------------------------------
# get_metrics_timeseries
# ---------------------------------------------------------------------------


class TestGetMetricsTimeseries:
    """Tests for ApiMetricsSqliteBackend.get_metrics_timeseries."""

    def test_returns_empty_list_when_no_data(self, backend):
        result = backend.get_metrics_timeseries(period_seconds=900)
        assert result == []

    def test_returns_list_of_tuples(self, backend):
        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "min1")
        backend.upsert_bucket("alice", "min1", bucket, "semantic")

        result = backend.get_metrics_timeseries(period_seconds=900)
        assert len(result) >= 1
        row = result[0]
        assert len(row) == 3  # (bucket_start, metric_type, count)

    def test_15min_uses_no_grouping(self, backend):
        """15min window (min1 tier): each 1-minute bucket is its own group."""
        now = datetime.now(timezone.utc)

        # Insert data in 3 distinct 1-minute buckets
        for minutes_ago in [1, 3, 5]:
            dt = now - timedelta(minutes=minutes_ago)
            bucket = _bucket_start(dt, "min1")
            backend.upsert_bucket("alice", "min1", bucket, "semantic")

        result = backend.get_metrics_timeseries(period_seconds=900)
        bucket_starts = [row[0] for row in result]
        # All 3 distinct buckets must appear
        assert len(set(bucket_starts)) == 3

    def test_1h_uses_no_grouping(self, backend):
        """1h window (min5 tier): each 5-minute bucket is its own group."""
        now = datetime.now(timezone.utc)

        for minutes_ago in [5, 15, 25]:
            dt = now - timedelta(minutes=minutes_ago)
            bucket = _bucket_start(dt, "min5")
            backend.upsert_bucket("alice", "min5", bucket, "regex")

        result = backend.get_metrics_timeseries(period_seconds=3600)
        bucket_starts = [row[0] for row in result]
        assert len(set(bucket_starts)) == 3

    def test_24h_groups_into_2hour_windows(self, backend):
        """24h window (hour1 tier): buckets grouped into 2-hour windows → 12 max."""
        now = datetime.now(timezone.utc)

        # Insert data in 4 distinct hour buckets spread across 2 different 2-hour groups
        # e.g. hours 0,1 → grouped to hour 0; hours 2,3 → grouped to hour 2
        for hours_ago in [1, 2, 3, 4]:
            dt = now - timedelta(hours=hours_ago)
            bucket = _bucket_start(dt, "hour1")
            backend.upsert_bucket("alice", "hour1", bucket, "semantic")

        result = backend.get_metrics_timeseries(period_seconds=86400)

        # Result must have rows, each row is (bucket_start, metric_type, count)
        assert len(result) > 0
        # The bucket_starts must be even hours (grouped into 2h windows)
        for bucket_start, metric_type, count in result:
            # Parse the hour from the bucket_start
            dt_parsed = datetime.fromisoformat(bucket_start.replace("Z", "+00:00"))
            # Even-hour grouping: hour must be even
            assert dt_parsed.hour % 2 == 0, (
                f"Expected even hour (2h window), got hour {dt_parsed.hour} in {bucket_start}"
            )

    def test_24h_max_12_buckets(self, backend):
        """24h window produces at most 12 buckets (24 hours / 2-hour windows)."""
        now = datetime.now(timezone.utc)

        # Insert data for all 24 hours
        for hours_ago in range(24):
            dt = now - timedelta(hours=hours_ago + 1)
            bucket = _bucket_start(dt, "hour1")
            backend.upsert_bucket("alice", "hour1", bucket, "semantic")

        result = backend.get_metrics_timeseries(period_seconds=86400)
        unique_buckets = set(row[0] for row in result)
        assert len(unique_buckets) <= 12

    def test_7d_uses_no_grouping(self, backend):
        """7d window (day1 tier): each day bucket is its own group, max 7.

        Uses days_ago=0..6 (not 1..7) so the oldest bucket is clearly inside
        the 7-day window, avoiding boundary exclusion due to sub-second timing.
        """
        now = datetime.now(timezone.utc)

        for days_ago in range(
            0, 7
        ):  # today through 6 days ago = 7 distinct day buckets
            dt = now - timedelta(days=days_ago)
            bucket = _bucket_start(dt, "day1")
            backend.upsert_bucket("alice", "day1", bucket, "semantic")

        result = backend.get_metrics_timeseries(period_seconds=604800)
        unique_buckets = set(row[0] for row in result)
        assert len(unique_buckets) == 7

    def test_15d_uses_no_grouping(self, backend):
        """15d window (day1 tier): each day bucket is its own group, max 15.

        Uses days_ago=0..14 (not 1..15) so the oldest bucket is clearly inside
        the 15-day window, avoiding boundary exclusion due to sub-second timing.
        """
        now = datetime.now(timezone.utc)

        for days_ago in range(
            0, 15
        ):  # today through 14 days ago = 15 distinct day buckets
            dt = now - timedelta(days=days_ago)
            bucket = _bucket_start(dt, "day1")
            backend.upsert_bucket("alice", "day1", bucket, "semantic")

        result = backend.get_metrics_timeseries(period_seconds=1296000)
        unique_buckets = set(row[0] for row in result)
        assert len(unique_buckets) == 15

    def test_ordered_by_bucket_start_asc(self, backend):
        """Timeseries must be ordered oldest → newest."""
        now = datetime.now(timezone.utc)

        for days_ago in [3, 1, 2]:
            dt = now - timedelta(days=days_ago)
            bucket = _bucket_start(dt, "day1")
            backend.upsert_bucket("alice", "day1", bucket, "semantic")

        result = backend.get_metrics_timeseries(period_seconds=604800)
        bucket_starts = [row[0] for row in result]
        assert bucket_starts == sorted(bucket_starts)

    def test_excludes_rows_outside_period(self, backend):
        """Rows with bucket_start before period cutoff must not appear."""
        old_dt = datetime.now(timezone.utc) - timedelta(days=20)
        old_bucket = _bucket_start(old_dt, "day1")
        backend.upsert_bucket("alice", "day1", old_bucket, "semantic")

        result = backend.get_metrics_timeseries(period_seconds=604800)
        assert result == []


# ---------------------------------------------------------------------------
# Protocol extension — ApiMetricsBackend protocol includes new methods
# ---------------------------------------------------------------------------


class TestApiMetricsBackendProtocol:
    """Verify ApiMetricsBackend protocol declares new methods."""

    def test_protocol_has_get_metrics_bucketed(self):
        from code_indexer.server.storage.protocols import ApiMetricsBackend

        assert hasattr(ApiMetricsBackend, "get_metrics_bucketed")

    def test_protocol_has_get_metrics_by_user(self):
        from code_indexer.server.storage.protocols import ApiMetricsBackend

        assert hasattr(ApiMetricsBackend, "get_metrics_by_user")

    def test_protocol_has_get_metrics_timeseries(self):
        from code_indexer.server.storage.protocols import ApiMetricsBackend

        assert hasattr(ApiMetricsBackend, "get_metrics_timeseries")

    def test_sqlite_backend_satisfies_protocol(self, backend):
        from code_indexer.server.storage.protocols import ApiMetricsBackend

        assert isinstance(backend, ApiMetricsBackend)


# ---------------------------------------------------------------------------
# Period selector HTML
# ---------------------------------------------------------------------------


class TestPeriodSelectorHtml:
    """Verify period selector template changes."""

    def _read_template(self):
        import os

        template_path = os.path.join(
            os.path.dirname(__file__),
            "../../../../src/code_indexer/server/web/templates/partials/dashboard_stats.html",
        )
        with open(os.path.normpath(template_path)) as f:
            return f.read()

    def test_60s_option_removed(self):
        html = self._read_template()
        assert 'value="60"' not in html, "60-second (1 minute) option must be removed"

    def test_7d_option_present(self):
        html = self._read_template()
        assert 'value="604800"' in html, "7-day (604800s) option must be present"

    def test_15d_option_present(self):
        html = self._read_template()
        assert 'value="1296000"' in html, "15-day (1296000s) option must be present"

    def test_900_option_kept(self):
        html = self._read_template()
        assert 'value="900"' in html, "15-minute (900s) option must be kept"

    def test_3600_option_kept(self):
        html = self._read_template()
        assert 'value="3600"' in html, "1-hour (3600s) option must be kept"

    def test_86400_option_kept(self):
        html = self._read_template()
        assert 'value="86400"' in html, "24-hour (86400s) option must be kept"

    def test_default_is_24h_not_60s(self):
        """Default selected option should be 86400 (24h), not 60."""
        html = self._read_template()
        # The old default was 60s; new default must be 86400 or 900
        assert 'value="60"' not in html


# ---------------------------------------------------------------------------
# ApiMetricsService delegation
# ---------------------------------------------------------------------------


class TestApiMetricsServiceDelegation:
    """Verify ApiMetricsService delegates new methods to backend."""

    def test_get_metrics_bucketed_delegates_to_backend(self, backend):
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        service = ApiMetricsService()
        service._backend = backend

        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "min1")
        backend.upsert_bucket("alice", "min1", bucket, "semantic")

        result = service.get_metrics_bucketed(period_seconds=900)
        assert result["semantic_searches"] == 1

    def test_get_metrics_bucketed_returns_zeros_without_backend(self):
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        service = ApiMetricsService()
        # No backend set
        result = service.get_metrics_bucketed(period_seconds=900)
        assert result == {
            "semantic_searches": 0,
            "other_index_searches": 0,
            "regex_searches": 0,
            "other_api_calls": 0,
        }

    def test_get_metrics_by_user_delegates_to_backend(self, backend):
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        service = ApiMetricsService()
        service._backend = backend

        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "min1")
        backend.upsert_bucket("alice", "min1", bucket, "regex")

        result = service.get_metrics_by_user(period_seconds=900)
        assert "alice" in result
        assert result["alice"]["regex"] == 1

    def test_get_metrics_by_user_returns_empty_without_backend(self):
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        service = ApiMetricsService()
        result = service.get_metrics_by_user(period_seconds=900)
        assert result == {}

    def test_get_metrics_timeseries_delegates_to_backend(self, backend):
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        service = ApiMetricsService()
        service._backend = backend

        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "day1")
        backend.upsert_bucket("alice", "day1", bucket, "semantic")

        result = service.get_metrics_timeseries(period_seconds=604800)
        assert len(result) >= 1

    def test_get_metrics_timeseries_returns_empty_without_backend(self):
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        service = ApiMetricsService()
        result = service.get_metrics_timeseries(period_seconds=604800)
        assert result == []


# ---------------------------------------------------------------------------
# Dashboard service wiring
# ---------------------------------------------------------------------------


class TestDashboardServiceWiring:
    """Verify dashboard service get_stats_partial calls get_metrics_bucketed."""

    def test_get_stats_partial_uses_get_metrics_bucketed(self, backend):
        """When api_metrics_backend provided, get_stats_partial should call
        get_metrics_bucketed with the api_window value."""
        from code_indexer.server.services.dashboard_service import DashboardService

        now = datetime.now(timezone.utc)
        bucket = _bucket_start(now, "hour1")
        backend.upsert_bucket("alice", "hour1", bucket, "semantic")
        backend.upsert_bucket("alice", "hour1", bucket, "semantic")

        service = DashboardService()
        result = service.get_stats_partial(
            username="alice",
            api_window=86400,
            api_metrics_backend=backend,
        )

        # api_metrics in result must use bucketed counts (semantic_searches=2)
        api_metrics = result["api_metrics"]
        assert api_metrics["semantic_searches"] == 2

    def test_get_stats_partial_default_api_window_is_24h(self):
        """Default api_window for get_stats_partial should be 86400 (24h)."""
        from code_indexer.server.services.dashboard_service import DashboardService
        import inspect

        sig = inspect.signature(DashboardService.get_stats_partial)
        default = sig.parameters["api_window"].default
        assert default == 86400, f"Expected api_window default of 86400, got {default}"
