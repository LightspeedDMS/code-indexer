"""
Unit tests for CacheInvalidationService.

Tests dual-strategy cache invalidation:
  - Strategy 1: mtime/content change detection on alias JSON files
  - Strategy 2: TTL-based expiry

Uses real filesystem operations via tmp_path; no mocking required.
"""

import json
import os
import time
from pathlib import Path

import pytest

from code_indexer.server.services.cache_invalidation_service import (
    CacheInvalidationService,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_alias_json(aliases_dir: Path, alias: str, target_path: str) -> Path:
    """Write a minimal alias JSON file and return its path."""
    alias_file = aliases_dir / f"{alias}.json"
    data = {
        "target_path": target_path,
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_refresh": "2026-01-01T00:00:00+00:00",
        "repo_name": alias.replace("-global", ""),
    }
    alias_file.write_text(json.dumps(data))
    return alias_file


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------


class TestInit:
    def test_creates_service_with_defaults(self, tmp_path):
        svc = CacheInvalidationService(str(tmp_path / "aliases"))
        assert svc._ttl_seconds == 300

    def test_creates_service_with_custom_ttl(self, tmp_path):
        svc = CacheInvalidationService(str(tmp_path / "aliases"), ttl_seconds=60)
        assert svc._ttl_seconds == 60

    def test_internal_caches_start_empty(self, tmp_path):
        svc = CacheInvalidationService(str(tmp_path / "aliases"))
        assert svc._mtime_cache == {}
        assert svc._content_cache == {}
        assert svc._last_loaded == {}


# ---------------------------------------------------------------------------
# check_invalidation — first-time / no-change
# ---------------------------------------------------------------------------


class TestCheckInvalidationFirstSight:
    def test_first_check_returns_false(self, tmp_path):
        """First call establishes baseline; no invalidation triggered."""
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        result = svc.check_invalidation("repo-global")
        assert result is False

    def test_repeated_check_no_change_returns_false(self, tmp_path):
        """Multiple checks with no file change stay False."""
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        svc.check_invalidation("repo-global")  # prime baseline
        assert svc.check_invalidation("repo-global") is False
        assert svc.check_invalidation("repo-global") is False

    def test_missing_alias_file_returns_true(self, tmp_path):
        """If the alias file does not exist, cache is treated as stale."""
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()

        svc = CacheInvalidationService(str(aliases_dir))
        result = svc.check_invalidation("nonexistent-global")
        assert result is True


# ---------------------------------------------------------------------------
# check_invalidation — mtime change, same target_path
# ---------------------------------------------------------------------------


class TestMtimeChangeNoTargetChange:
    def test_mtime_bump_same_target_returns_false(self, tmp_path):
        """
        If mtime changes but target_path is the same (e.g. last_refresh update),
        the cache should NOT be invalidated.
        """
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        alias_file = write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        svc.check_invalidation("repo-global")  # prime

        # Touch the file (update mtime) but keep target_path the same
        original = json.loads(alias_file.read_text())
        original["last_refresh"] = "2026-06-01T12:00:00+00:00"
        alias_file.write_text(json.dumps(original))

        result = svc.check_invalidation("repo-global")
        assert result is False


# ---------------------------------------------------------------------------
# check_invalidation — content change (target_path changed)
# ---------------------------------------------------------------------------


class TestContentChange:
    def test_target_path_change_returns_true(self, tmp_path):
        """Changing target_path should trigger invalidation."""
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        alias_file = write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        svc.check_invalidation("repo-global")  # prime

        # Simulate swap_alias: update target_path
        data = json.loads(alias_file.read_text())
        data["target_path"] = "/versioned/v_002"
        alias_file.write_text(json.dumps(data))
        # Bump mtime forward so the service detects the change
        # (filesystem mtime resolution is 1 second on most systems)
        import os

        st = os.stat(alias_file)
        os.utime(alias_file, (st.st_atime, st.st_mtime + 2))

        result = svc.check_invalidation("repo-global")
        assert result is True

    def test_after_invalidation_record_load_resets_baseline(self, tmp_path):
        """
        After detecting an invalidation, record_cache_load should reset the
        baseline so subsequent checks return False again.
        """
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        alias_file = write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        svc.check_invalidation("repo-global")  # prime

        # Swap to v_002
        data = json.loads(alias_file.read_text())
        data["target_path"] = "/versioned/v_002"
        alias_file.write_text(json.dumps(data))
        # Bump mtime forward (filesystem mtime resolution is 1 second)
        import os

        st = os.stat(alias_file)
        os.utime(alias_file, (st.st_atime, st.st_mtime + 2))

        assert svc.check_invalidation("repo-global") is True

        # Caller reloads cache and records it
        svc.record_cache_load("repo-global", "/versioned/v_002")

        # Now the cache is fresh again
        assert svc.check_invalidation("repo-global") is False


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


class TestTTLExpiry:
    def test_ttl_expiry_triggers_invalidation(self, tmp_path):
        """Cache loaded more than ttl_seconds ago is treated as stale."""
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir), ttl_seconds=1)
        svc.check_invalidation("repo-global")  # prime baseline
        svc.record_cache_load("repo-global", "/versioned/v_001")

        # Expire the TTL by back-dating the load timestamp
        svc._last_loaded["repo-global"] = time.time() - 2  # 2s > 1s TTL

        result = svc.check_invalidation("repo-global")
        assert result is True

    def test_within_ttl_not_invalidated(self, tmp_path):
        """Cache loaded within ttl_seconds is NOT stale (TTL path)."""
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir), ttl_seconds=300)
        svc.check_invalidation("repo-global")  # prime
        svc.record_cache_load("repo-global", "/versioned/v_001")

        # 1 second elapsed — well within TTL
        svc._last_loaded["repo-global"] = time.time() - 1

        result = svc.check_invalidation("repo-global")
        assert result is False

    def test_ttl_checked_before_file_stat(self, tmp_path):
        """
        TTL expiry should trigger even if the file has not changed on disk,
        ensuring TTL acts as an independent ceiling.
        """
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir), ttl_seconds=1)
        svc.check_invalidation("repo-global")
        svc.record_cache_load("repo-global", "/versioned/v_001")

        # File unchanged, only TTL expired
        svc._last_loaded["repo-global"] = time.time() - 2

        assert svc.check_invalidation("repo-global") is True


# ---------------------------------------------------------------------------
# record_cache_load
# ---------------------------------------------------------------------------


class TestRecordCacheLoad:
    def test_record_updates_last_loaded(self, tmp_path):
        """record_cache_load stores a fresh load timestamp."""
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        before = time.time()
        svc.record_cache_load("repo-global", "/versioned/v_001")
        after = time.time()

        assert "repo-global" in svc._last_loaded
        assert before <= svc._last_loaded["repo-global"] <= after

    def test_record_updates_content_cache(self, tmp_path):
        """record_cache_load stores the target_path in content cache."""
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        svc.record_cache_load("repo-global", "/versioned/v_001")

        assert svc._content_cache["repo-global"] == "/versioned/v_001"

    def test_record_updates_mtime_baseline(self, tmp_path):
        """record_cache_load refreshes the mtime baseline from disk."""
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        alias_file = write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        svc.record_cache_load("repo-global", "/versioned/v_001")

        expected_mtime = os.stat(alias_file).st_mtime
        assert svc._mtime_cache["repo-global"] == pytest.approx(expected_mtime)

    def test_record_missing_file_does_not_raise(self, tmp_path):
        """record_cache_load on a missing file must not raise."""
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()

        svc = CacheInvalidationService(str(aliases_dir))
        # No alias file created — should not raise
        svc.record_cache_load("ghost-global", "/some/path")
        assert svc._content_cache["ghost-global"] == "/some/path"


# ---------------------------------------------------------------------------
# get_current_target_path
# ---------------------------------------------------------------------------


class TestGetCurrentTargetPath:
    def test_returns_target_path_from_disk(self, tmp_path):
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-global", "/versioned/v_007")

        svc = CacheInvalidationService(str(aliases_dir))
        result = svc.get_current_target_path("repo-global")
        assert result == "/versioned/v_007"

    def test_returns_none_for_missing_file(self, tmp_path):
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()

        svc = CacheInvalidationService(str(aliases_dir))
        result = svc.get_current_target_path("nonexistent-global")
        assert result is None

    def test_always_reads_from_disk_not_cache(self, tmp_path):
        """get_current_target_path bypasses internal cache."""
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        alias_file = write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        svc.record_cache_load("repo-global", "/versioned/v_001")

        # Update file on disk
        data = json.loads(alias_file.read_text())
        data["target_path"] = "/versioned/v_999"
        alias_file.write_text(json.dumps(data))

        # Should read the updated value, ignoring content_cache
        result = svc.get_current_target_path("repo-global")
        assert result == "/versioned/v_999"


# ---------------------------------------------------------------------------
# invalidate_all
# ---------------------------------------------------------------------------


class TestInvalidateAll:
    def test_invalidate_all_returns_list_of_aliases(self, tmp_path):
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-a-global", "/versioned/a_001")
        write_alias_json(aliases_dir, "repo-b-global", "/versioned/b_001")

        svc = CacheInvalidationService(str(aliases_dir))
        svc.check_invalidation("repo-a-global")
        svc.check_invalidation("repo-b-global")

        invalidated = svc.invalidate_all()
        assert set(invalidated) == {"repo-a-global", "repo-b-global"}

    def test_invalidate_all_clears_mtime_cache(self, tmp_path):
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        svc.check_invalidation("repo-global")
        assert "repo-global" in svc._mtime_cache

        svc.invalidate_all()
        assert svc._mtime_cache == {}

    def test_invalidate_all_clears_content_cache(self, tmp_path):
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        svc.record_cache_load("repo-global", "/versioned/v_001")
        assert "repo-global" in svc._content_cache

        svc.invalidate_all()
        assert svc._content_cache == {}

    def test_invalidate_all_clears_last_loaded(self, tmp_path):
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        svc.record_cache_load("repo-global", "/versioned/v_001")
        assert "repo-global" in svc._last_loaded

        svc.invalidate_all()
        assert svc._last_loaded == {}

    def test_invalidate_all_empty_returns_empty_list(self, tmp_path):
        svc = CacheInvalidationService(str(tmp_path / "aliases"))
        result = svc.invalidate_all()
        assert result == []

    def test_after_invalidate_all_first_check_returns_false(self, tmp_path):
        """
        After invalidate_all, first check_invalidation call re-primes baseline
        and returns False (unknown state is treated as fresh on first sight).
        """
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        write_alias_json(aliases_dir, "repo-global", "/versioned/v_001")

        svc = CacheInvalidationService(str(aliases_dir))
        svc.check_invalidation("repo-global")
        svc.record_cache_load("repo-global", "/versioned/v_001")

        svc.invalidate_all()

        # Next check re-primes; should be fresh
        result = svc.check_invalidation("repo-global")
        assert result is False
