"""
Unit tests for RefreshScheduler jitter calculation methods (Story #284 AC3).

Tests:
- _calculate_jitter(): bounds, sign distribution, scale with interval
- _calculate_poll_interval(): MIN/MAX clamping, midrange formula, boundary values
"""

from pathlib import Path

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.config import ConfigManager


def _make_scheduler(tmp_path: Path) -> RefreshScheduler:
    """Create a minimal RefreshScheduler for method-level unit tests."""
    golden_repos_dir = tmp_path / "golden_repos"
    golden_repos_dir.mkdir(parents=True)

    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    tracker = QueryTracker()
    cleanup_mgr = CleanupManager(tracker)
    registry = GlobalRegistry(str(golden_repos_dir))

    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=tracker,
        cleanup_manager=cleanup_mgr,
        registry=registry,
    )


class TestCalculateJitter:
    """Tests for _calculate_jitter() method (Story #284 AC3)."""

    def test_jitter_percentage_constant_is_ten_percent(self, tmp_path):
        """JITTER_PERCENTAGE class constant is exactly 0.10 (10%)."""
        scheduler = _make_scheduler(tmp_path)
        assert scheduler.JITTER_PERCENTAGE == 0.10

    def test_jitter_within_positive_bounds(self, tmp_path):
        """_calculate_jitter() never exceeds +JITTER_PERCENTAGE * interval."""
        scheduler = _make_scheduler(tmp_path)
        refresh_interval = 3600

        for _ in range(200):
            jitter = scheduler._calculate_jitter(refresh_interval)
            max_jitter = refresh_interval * scheduler.JITTER_PERCENTAGE
            assert jitter <= max_jitter, (
                f"Jitter {jitter} exceeded max {max_jitter}"
            )

    def test_jitter_within_negative_bounds(self, tmp_path):
        """_calculate_jitter() never goes below -JITTER_PERCENTAGE * interval."""
        scheduler = _make_scheduler(tmp_path)
        refresh_interval = 3600

        for _ in range(200):
            jitter = scheduler._calculate_jitter(refresh_interval)
            min_jitter = -(refresh_interval * scheduler.JITTER_PERCENTAGE)
            assert jitter >= min_jitter, (
                f"Jitter {jitter} below minimum {min_jitter}"
            )

    def test_jitter_produces_both_positive_and_negative_values(self, tmp_path):
        """
        Over many samples, _calculate_jitter() produces both positive and
        negative values (bidirectional distribution).
        """
        scheduler = _make_scheduler(tmp_path)
        refresh_interval = 3600

        values = [scheduler._calculate_jitter(refresh_interval) for _ in range(100)]
        assert any(v > 0 for v in values), "Expected some positive jitter values"
        assert any(v < 0 for v in values), "Expected some negative jitter values"

    def test_jitter_scales_with_interval(self, tmp_path):
        """Max jitter magnitude scales proportionally with the refresh interval."""
        scheduler = _make_scheduler(tmp_path)

        short_interval = 300    # 5 min -> max jitter 30s
        long_interval = 7200    # 2 hours -> max jitter 720s

        short_max = short_interval * scheduler.JITTER_PERCENTAGE
        long_max = long_interval * scheduler.JITTER_PERCENTAGE

        for _ in range(50):
            assert abs(scheduler._calculate_jitter(short_interval)) <= short_max
            assert abs(scheduler._calculate_jitter(long_interval)) <= long_max


class TestCalculatePollInterval:
    """Tests for _calculate_poll_interval() method (Story #284)."""

    def test_min_poll_constant_is_ten(self, tmp_path):
        """MIN_POLL_SECONDS constant equals 10."""
        scheduler = _make_scheduler(tmp_path)
        assert scheduler.MIN_POLL_SECONDS == 10

    def test_max_poll_constant_is_thirty(self, tmp_path):
        """MAX_POLL_SECONDS constant equals 30."""
        scheduler = _make_scheduler(tmp_path)
        assert scheduler.MAX_POLL_SECONDS == 30

    def test_very_short_interval_clamped_to_min(self, tmp_path):
        """refresh_interval/20 below MIN_POLL_SECONDS is clamped to MIN_POLL_SECONDS."""
        scheduler = _make_scheduler(tmp_path)
        # interval=60s -> 60/20=3s, below MIN_POLL_SECONDS=10
        result = scheduler._calculate_poll_interval(60)
        assert result == scheduler.MIN_POLL_SECONDS

    def test_very_long_interval_clamped_to_max(self, tmp_path):
        """refresh_interval/20 above MAX_POLL_SECONDS is clamped to MAX_POLL_SECONDS."""
        scheduler = _make_scheduler(tmp_path)
        # interval=3600s -> 3600/20=180s, above MAX_POLL_SECONDS=30
        result = scheduler._calculate_poll_interval(3600)
        assert result == scheduler.MAX_POLL_SECONDS

    def test_midrange_interval_uses_formula(self, tmp_path):
        """A midrange interval returns interval/20 without clamping."""
        scheduler = _make_scheduler(tmp_path)
        # interval=400s -> 400/20=20s, within [10, 30]
        result = scheduler._calculate_poll_interval(400)
        assert result == 20.0

    def test_exactly_at_min_boundary(self, tmp_path):
        """interval/20 == MIN_POLL_SECONDS returns exactly MIN_POLL_SECONDS."""
        scheduler = _make_scheduler(tmp_path)
        # interval=200s -> 200/20=10s = MIN_POLL_SECONDS
        result = scheduler._calculate_poll_interval(200)
        assert result == scheduler.MIN_POLL_SECONDS

    def test_exactly_at_max_boundary(self, tmp_path):
        """interval/20 == MAX_POLL_SECONDS returns exactly MAX_POLL_SECONDS."""
        scheduler = _make_scheduler(tmp_path)
        # interval=600s -> 600/20=30s = MAX_POLL_SECONDS
        result = scheduler._calculate_poll_interval(600)
        assert result == scheduler.MAX_POLL_SECONDS
