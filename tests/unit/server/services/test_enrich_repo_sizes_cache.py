"""Tests for _enrich_repo_sizes() TTL cache (Bug #251).

Verifies that repeated calls to _enrich_repo_sizes() within the TTL window
do NOT trigger repeated filesystem rglob walks, and that the cache expires
correctly after the TTL.
"""

import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.services.dependency_map_service import (
    DependencyMapService,
    REPO_SIZES_CACHE_TTL,
)
from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_repo_root(tmp_path: Path) -> Path:
    """Create a temporary directory with two repos, each containing one file."""
    root = tmp_path / "golden-repos"
    root.mkdir()

    for alias in ["repo-a", "repo-b"]:
        repo_dir = root / alias
        repo_dir.mkdir()
        (repo_dir / "main.py").write_text(f"# {alias}\n")

    return root


@pytest.fixture
def repo_list(tmp_repo_root: Path):
    """Two-element repo list pointing at the temp directories."""
    return [
        {"alias": "repo-a", "clone_path": str(tmp_repo_root / "repo-a")},
        {"alias": "repo-b", "clone_path": str(tmp_repo_root / "repo-b")},
    ]


@pytest.fixture
def service():
    """Minimal DependencyMapService with all dependencies mocked."""
    config_manager = Mock()
    config = ClaudeIntegrationConfig(dependency_map_enabled=True)
    config_manager.get_claude_integration_config.return_value = config

    golden_repos_manager = Mock()
    golden_repos_manager.golden_repos_dir = Path("/fake/golden-repos")

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=Mock(),
        analyzer=Mock(),
    )


# ---------------------------------------------------------------------------
# Cache constant
# ---------------------------------------------------------------------------


class TestCacheConstant:
    """REPO_SIZES_CACHE_TTL must be exported and be a positive number."""

    def test_cache_ttl_constant_exists_and_is_positive(self):
        assert REPO_SIZES_CACHE_TTL > 0

    def test_cache_ttl_constant_is_60_seconds(self):
        """Default TTL is 60 seconds as specified in the bug report."""
        assert REPO_SIZES_CACHE_TTL == 60


# ---------------------------------------------------------------------------
# Cache initialisation
# ---------------------------------------------------------------------------


class TestCacheInitialState:
    """Instance cache variables must be initialised in __init__."""

    def test_repo_sizes_cache_starts_as_none(self, service):
        assert service._repo_sizes_cache is None

    def test_repo_sizes_cache_time_starts_as_zero(self, service):
        assert service._repo_sizes_cache_time == 0.0


# ---------------------------------------------------------------------------
# First call behaviour
# ---------------------------------------------------------------------------


class TestFirstCallPerformsWalk:
    """The first call must actually walk the filesystem."""

    def test_first_call_returns_enriched_list(self, service, repo_list):
        result = service._enrich_repo_sizes(repo_list)

        assert len(result) == 2
        for repo in result:
            assert "file_count" in repo
            assert "total_bytes" in repo
            assert repo["file_count"] > 0

    def test_first_call_populates_cache(self, service, repo_list):
        assert service._repo_sizes_cache is None

        service._enrich_repo_sizes(repo_list)

        assert service._repo_sizes_cache is not None

    def test_first_call_sets_cache_timestamp(self, service, repo_list):
        assert service._repo_sizes_cache_time == 0.0

        before = time.monotonic()
        service._enrich_repo_sizes(repo_list)
        after = time.monotonic()

        assert before <= service._repo_sizes_cache_time <= after


# ---------------------------------------------------------------------------
# Cache hit within TTL
# ---------------------------------------------------------------------------


class TestSecondCallWithinTTLUsesCachedResult:
    """A second call within the TTL must NOT re-walk the filesystem."""

    def test_second_call_returns_same_object(self, service, repo_list):
        first_result = service._enrich_repo_sizes(repo_list)
        second_result = service._enrich_repo_sizes(repo_list)

        # Must be the exact same list object (from cache)
        assert first_result is second_result

    def test_rglob_called_once_for_two_calls_within_ttl(self, service, repo_list, tmp_repo_root):
        """Patch Path.rglob to count invocations."""
        rglob_call_count = {"count": 0}
        original_rglob = Path.rglob

        def counting_rglob(self, pattern):
            rglob_call_count["count"] += 1
            return original_rglob(self, pattern)

        with patch.object(Path, "rglob", counting_rglob):
            service._enrich_repo_sizes(repo_list)
            calls_after_first = rglob_call_count["count"]

            service._enrich_repo_sizes(repo_list)
            calls_after_second = rglob_call_count["count"]

        # First call must have done at least one rglob
        assert calls_after_first > 0
        # Second call must NOT have added any new rglob calls
        assert calls_after_second == calls_after_first


# ---------------------------------------------------------------------------
# Cache expiry
# ---------------------------------------------------------------------------


class TestCacheExpiresAfterTTL:
    """A call after TTL expiry must refresh via a new filesystem walk."""

    def test_expired_cache_triggers_new_walk(self, service, repo_list):
        """Force cache time into the past so it appears expired."""
        # Prime the cache
        service._enrich_repo_sizes(repo_list)
        first_cache = service._repo_sizes_cache

        # Wind the clock back past the TTL
        service._repo_sizes_cache_time = time.monotonic() - (REPO_SIZES_CACHE_TTL + 1)

        rglob_call_count = {"count": 0}
        original_rglob = Path.rglob

        def counting_rglob(self, pattern):
            rglob_call_count["count"] += 1
            return original_rglob(self, pattern)

        with patch.object(Path, "rglob", counting_rglob):
            result = service._enrich_repo_sizes(repo_list)

        # A new walk must have happened
        assert rglob_call_count["count"] > 0
        # The cache timestamp must have been refreshed
        assert service._repo_sizes_cache_time > time.monotonic() - 1.0

    def test_expired_cache_updates_stored_cache(self, service, repo_list):
        """After TTL expiry the cache must point to the freshly-built result."""
        # Prime cache
        service._enrich_repo_sizes(repo_list)
        first_cache = service._repo_sizes_cache

        # Expire it
        service._repo_sizes_cache_time = time.monotonic() - (REPO_SIZES_CACHE_TTL + 1)

        # Add a new file to one repo so the result changes
        new_file = Path(repo_list[0]["clone_path"]) / "extra.py"
        new_file.write_text("# extra\n")

        second_result = service._enrich_repo_sizes(repo_list)

        # Cache should be the new result (same object as returned)
        assert service._repo_sizes_cache is second_result


# ---------------------------------------------------------------------------
# Correctness of cached data
# ---------------------------------------------------------------------------


class TestCacheStoresCorrectSizeData:
    """The cached result must contain accurate size information."""

    def test_file_count_reflects_actual_files(self, service, repo_list, tmp_repo_root):
        result = service._enrich_repo_sizes(repo_list)

        repo_a = next(r for r in result if r["alias"] == "repo-a")
        # There is exactly one .py file in repo-a (main.py)
        assert repo_a["file_count"] == 1

    def test_total_bytes_reflects_actual_sizes(self, service, repo_list, tmp_repo_root):
        result = service._enrich_repo_sizes(repo_list)

        for repo in result:
            path = Path(repo["clone_path"]) / "main.py"
            expected_bytes = path.stat().st_size
            assert repo["total_bytes"] == expected_bytes

    def test_cached_result_and_fresh_result_are_identical(self, service, repo_list):
        """Calling twice within TTL must yield identical data."""
        first = service._enrich_repo_sizes(repo_list)
        second = service._enrich_repo_sizes(repo_list)

        assert first == second
