"""Per-call latency: admin "all repos" enumeration is cached.

get_accessible_repos() for an admin key walks every group x every repo on
EVERY repo-scoped tool call. That set == the full tracked repo set, so it is
safe to cache: invalidated on any repo-access change (register_on_repo_change),
with a TTL backstop. Not per-user, so no group-membership staleness.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.server.services.access_filtering_service import AccessFilteringService
from code_indexer.server.services.group_access_manager import GroupAccessManager


@pytest.fixture
def temp_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_access_manager(temp_db_path):
    manager = GroupAccessManager(temp_db_path)
    admins = manager.get_group_by_name("admins")
    powerusers = manager.get_group_by_name("powerusers")
    manager.grant_repo_access("repo-a", admins.id, "system:test")
    manager.grant_repo_access("repo-b", powerusers.id, "system:test")
    # Assign an admin user so get_accessible_repos hits the admin branch.
    manager.assign_user_to_group("admin-user", admins.id, "system:test")
    return manager


@pytest.fixture
def service(group_access_manager):
    return AccessFilteringService(group_access_manager)


def _count_get_all_groups(manager):
    call_count = [0]
    original = manager.get_all_groups

    def counting():
        call_count[0] += 1
        return original()

    manager.get_all_groups = counting
    return call_count


def test_admin_second_call_hits_cache(service, group_access_manager):
    """Second admin call must not re-enumerate all groups (cache hit)."""
    calls = _count_get_all_groups(group_access_manager)

    r1 = service.get_accessible_repos("admin-user")
    assert calls[0] == 1
    r2 = service.get_accessible_repos("admin-user")
    assert calls[0] == 1, f"expected 1 enumeration (cache hit), got {calls[0]}"
    assert r1 == r2
    assert "repo-a" in r1 and "repo-b" in r1  # repos across all groups


def test_cached_set_is_a_copy(service):
    """Callers mutating the returned set must not corrupt the cache."""
    first = service.get_accessible_repos("admin-user")
    first.add("injected-not-real")
    second = service.get_accessible_repos("admin-user")
    assert "injected-not-real" not in second


def test_repo_change_invalidates_admin_cache(service, group_access_manager):
    """A grant/revoke auto-invalidates via register_on_repo_change."""
    before = service.get_accessible_repos("admin-user")
    assert "repo-new" not in before

    users = group_access_manager.get_group_by_name("users")
    group_access_manager.grant_repo_access("repo-new", users.id, "system:test")

    after = service.get_accessible_repos("admin-user")
    assert "repo-new" in after, "grant must be reflected (cache auto-invalidated)"


@pytest.mark.slow
def test_admin_cache_expires_after_ttl(service, group_access_manager):
    calls = _count_get_all_groups(group_access_manager)

    with patch("time.time", return_value=0.0):
        service.get_accessible_repos("admin-user")
    assert calls[0] == 1

    with patch("time.time", return_value=30.0):
        service.get_accessible_repos("admin-user")
    assert calls[0] == 1, "within TTL: still cached"

    with patch("time.time", return_value=61.0):
        service.get_accessible_repos("admin-user")
    assert calls[0] == 2, "after TTL: re-enumerated"
