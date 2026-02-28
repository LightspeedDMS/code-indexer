"""
Unit tests for Bug #338: _get_all_repo_aliases() TTL caching in AccessFilteringService.

AC1: _get_all_repo_aliases() results are cached with a configurable TTL (default 60s).
AC2: Cache is invalidated when group repo assignments change (add/remove repo from group).
AC3: Unit tests verify caching behavior (cached hit, TTL expiry, invalidation).

TDD: Tests written FIRST before implementation (red phase).
"""

import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from code_indexer.server.services.access_filtering_service import AccessFilteringService
from code_indexer.server.services.group_access_manager import GroupAccessManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_path():
    """Temporary SQLite DB file for GroupAccessManager."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_access_manager(temp_db_path):
    """
    GroupAccessManager pre-populated with:
    - admins: repo-a, repo-b, repo-c
    - powerusers: repo-a, repo-b
    - users: (no extra repos beyond cidx-meta)
    """
    manager = GroupAccessManager(temp_db_path)
    admins = manager.get_group_by_name("admins")
    powerusers = manager.get_group_by_name("powerusers")

    manager.grant_repo_access("repo-a", admins.id, "system:test")
    manager.grant_repo_access("repo-b", admins.id, "system:test")
    manager.grant_repo_access("repo-c", admins.id, "system:test")
    manager.grant_repo_access("repo-a", powerusers.id, "system:test")
    manager.grant_repo_access("repo-b", powerusers.id, "system:test")

    return manager


@pytest.fixture
def service(group_access_manager):
    """Real AccessFilteringService with real GroupAccessManager."""
    return AccessFilteringService(group_access_manager)


# ---------------------------------------------------------------------------
# AC1: Cache hit - second call returns cached result without DB query
# ---------------------------------------------------------------------------


class TestRepoAliasesCacheHit:
    """AC1: _get_all_repo_aliases() is cached with a TTL."""

    def test_second_call_returns_same_result_without_db_query(self, service, group_access_manager):
        """
        AC1: Two consecutive calls return the same set.
        The second call must not invoke get_all_groups() again (cache hit).
        """
        call_count = [0]
        original_get_all_groups = group_access_manager.get_all_groups

        def counting_get_all_groups():
            call_count[0] += 1
            return original_get_all_groups()

        group_access_manager.get_all_groups = counting_get_all_groups

        # First call - populates cache
        result1 = service._get_all_repo_aliases()
        assert call_count[0] == 1

        # Second call - must hit cache (no additional DB query)
        result2 = service._get_all_repo_aliases()
        assert call_count[0] == 1, (
            f"Expected 1 DB call total (cache hit on 2nd call), got {call_count[0]}"
        )
        assert result1 == result2

    def test_cached_result_contains_all_repo_aliases(self, service):
        """
        AC1: The cached result is the full set of all repo aliases across all groups.
        """
        aliases = service._get_all_repo_aliases()
        assert "repo-a" in aliases
        assert "repo-b" in aliases
        assert "repo-c" in aliases

    def test_cache_is_fresh_on_first_call(self, service, group_access_manager):
        """
        AC1: On a fresh service instance, _get_all_repo_aliases() queries the DB.
        """
        call_count = [0]
        original_get_all_groups = group_access_manager.get_all_groups

        def counting_get_all_groups():
            call_count[0] += 1
            return original_get_all_groups()

        group_access_manager.get_all_groups = counting_get_all_groups

        service._get_all_repo_aliases()
        assert call_count[0] == 1, "First call must query the DB"


# ---------------------------------------------------------------------------
# AC1: TTL expiry - cache expires and DB is re-queried
# ---------------------------------------------------------------------------


class TestRepoAliasesTTLExpiry:
    """AC1: Cache expires after TTL and forces a fresh DB query."""

    def test_cache_expires_after_ttl(self, service, group_access_manager):
        """
        AC1: After TTL seconds, the next call re-queries the DB.
        Uses time.monotonic() manipulation via patch.
        """
        call_count = [0]
        original_get_all_groups = group_access_manager.get_all_groups

        def counting_get_all_groups():
            call_count[0] += 1
            return original_get_all_groups()

        group_access_manager.get_all_groups = counting_get_all_groups

        # First call at t=0
        with patch("time.monotonic", return_value=0.0):
            service._get_all_repo_aliases()
        assert call_count[0] == 1

        # Second call still within TTL (t=30, TTL=60)
        with patch("time.monotonic", return_value=30.0):
            service._get_all_repo_aliases()
        assert call_count[0] == 1, "Within TTL: should still be cached"

        # Third call after TTL expiry (t=61, TTL=60)
        with patch("time.monotonic", return_value=61.0):
            service._get_all_repo_aliases()
        assert call_count[0] == 2, (
            f"After TTL expiry: expected 2 DB calls, got {call_count[0]}"
        )

    def test_default_ttl_is_60_seconds(self, service):
        """
        AC1: The default TTL is 60 seconds.
        Verifies the service has a REPO_ALIASES_CACHE_TTL attribute of 60.
        """
        assert hasattr(service, "REPO_ALIASES_CACHE_TTL"), (
            "AccessFilteringService must have REPO_ALIASES_CACHE_TTL attribute"
        )
        assert service.REPO_ALIASES_CACHE_TTL == 60


# ---------------------------------------------------------------------------
# AC2: Cache invalidation when group repo assignments change
# ---------------------------------------------------------------------------


class TestRepoAliasesCacheInvalidation:
    """AC2: Cache is invalidated when group repo assignments change."""

    def test_invalidate_repo_aliases_cache_method_exists(self, service):
        """
        AC2: AccessFilteringService must have a public invalidate_repo_aliases_cache() method.
        """
        assert hasattr(service, "invalidate_repo_aliases_cache"), (
            "AccessFilteringService must have invalidate_repo_aliases_cache() method"
        )
        assert callable(service.invalidate_repo_aliases_cache)

    def test_after_invalidation_next_call_queries_db(self, service, group_access_manager):
        """
        AC2: After invalidate_repo_aliases_cache(), the next _get_all_repo_aliases()
        call must re-query the DB.
        """
        call_count = [0]
        original_get_all_groups = group_access_manager.get_all_groups

        def counting_get_all_groups():
            call_count[0] += 1
            return original_get_all_groups()

        group_access_manager.get_all_groups = counting_get_all_groups

        # Populate cache
        service._get_all_repo_aliases()
        assert call_count[0] == 1

        # Invalidate cache
        service.invalidate_repo_aliases_cache()

        # Next call must re-query DB
        service._get_all_repo_aliases()
        assert call_count[0] == 2, (
            f"After invalidation: expected 2 DB calls, got {call_count[0]}"
        )

    def test_invalidation_reflects_new_repo_grants(self, service, group_access_manager):
        """
        AC2: After granting a new repo, _get_all_repo_aliases() automatically
        reflects the change because grant_repo_access() triggers the callback
        which invalidates the cache.
        """
        # Populate cache
        aliases_before = service._get_all_repo_aliases()
        assert "repo-new" not in aliases_before

        # Grant new repo to a group — callback auto-invalidates cache
        users = group_access_manager.get_group_by_name("users")
        group_access_manager.grant_repo_access("repo-new", users.id, "system:test")

        # Next call re-queries DB (cache was auto-invalidated by callback)
        aliases_after = service._get_all_repo_aliases()
        assert "repo-new" in aliases_after, (
            "After grant, new repo must appear in aliases (auto-invalidated by callback)"
        )

    def test_invalidation_reflects_revoked_repos(self, service, group_access_manager):
        """
        AC2: After revoking a repo, _get_all_repo_aliases() automatically
        reflects the removal because revoke_repo_access() triggers the callback
        which invalidates the cache.
        """
        # Populate cache - verify repo-c is there (only in admins group)
        aliases_before = service._get_all_repo_aliases()
        assert "repo-c" in aliases_before

        # Revoke repo-c from admins — callback auto-invalidates cache
        admins = group_access_manager.get_group_by_name("admins")
        group_access_manager.revoke_repo_access("repo-c", admins.id)

        # Next call re-queries DB (cache was auto-invalidated by callback)
        aliases_after = service._get_all_repo_aliases()
        assert "repo-c" not in aliases_after, (
            "After revoke, removed repo must not appear in aliases (auto-invalidated by callback)"
        )


# ---------------------------------------------------------------------------
# Bug #338: Callback/observer pattern in GroupAccessManager
# ---------------------------------------------------------------------------


class TestGroupAccessManagerCallbackPattern:
    """Bug #338: GroupAccessManager fires registered callbacks on repo access changes."""

    def test_callback_registered_in_access_filtering_service(self, group_access_manager):
        """
        AccessFilteringService must register invalidate_repo_aliases_cache as a
        callback on GroupAccessManager during __init__.
        After construction, grant_repo_access() must auto-invalidate the cache.
        """
        svc = AccessFilteringService(group_access_manager)

        # Populate the cache
        svc._get_all_repo_aliases()
        assert svc._repo_aliases_cache is not None, "Cache must be populated after first call"

        # Trigger grant_repo_access - callback should invalidate the cache automatically
        users = group_access_manager.get_group_by_name("users")
        group_access_manager.grant_repo_access("repo-callback-test", users.id, "system:test")

        assert svc._repo_aliases_cache is None, (
            "Cache must be invalidated automatically after grant_repo_access() "
            "when AccessFilteringService registers its callback on construction"
        )

    def test_grant_repo_access_triggers_callback(self, group_access_manager):
        """
        grant_repo_access() must invoke all registered callbacks when access is
        newly granted (rowcount > 0). It must NOT invoke callbacks when the
        access already exists (INSERT OR IGNORE - idempotent, rowcount == 0).
        """
        call_count = [0]

        def on_change():
            call_count[0] += 1

        group_access_manager.register_on_repo_change(on_change)

        users = group_access_manager.get_group_by_name("users")

        # First grant - new insert, callback must fire
        result = group_access_manager.grant_repo_access("repo-new", users.id, "system:test")
        assert result is True
        assert call_count[0] == 1, f"Expected callback called once after new grant, got {call_count[0]}"

        # Second grant of same repo - INSERT OR IGNORE, no change, callback must NOT fire again
        result2 = group_access_manager.grant_repo_access("repo-new", users.id, "system:test")
        assert result2 is False
        assert call_count[0] == 1, (
            f"Expected callback count to stay at 1 (no duplicate fire), got {call_count[0]}"
        )

    def test_revoke_repo_access_triggers_callback(self, group_access_manager):
        """
        revoke_repo_access() must invoke all registered callbacks when access is
        successfully revoked (rowcount > 0). It must NOT invoke callbacks when
        the access didn't exist (rowcount == 0).
        """
        call_count = [0]

        def on_change():
            call_count[0] += 1

        group_access_manager.register_on_repo_change(on_change)

        admins = group_access_manager.get_group_by_name("admins")

        # Revoke repo-c (exists in admins from fixture) - callback must fire
        result = group_access_manager.revoke_repo_access("repo-c", admins.id)
        assert result is True
        assert call_count[0] == 1, f"Expected callback called once after revoke, got {call_count[0]}"

        # Revoke same repo again - already gone, callback must NOT fire again
        result2 = group_access_manager.revoke_repo_access("repo-c", admins.id)
        assert result2 is False
        assert call_count[0] == 1, (
            f"Expected callback count to stay at 1 (no duplicate fire), got {call_count[0]}"
        )
