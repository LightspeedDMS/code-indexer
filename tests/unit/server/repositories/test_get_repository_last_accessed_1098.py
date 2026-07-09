"""
Unit tests for Bug #1098 fix: get_repository() touch/no-touch split and
touch_last_accessed() throttled method.

Two compounding defects fixed:
1. Admin/dashboard paths stamped last_accessed on every read (defeated reaper fleet-wide)
2. Search/query path never stamped last_accessed (search-only users got reaped)
"""

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)


class TestGetRepositoryTouchParameter:
    """Tests for the touch= parameter on get_repository()."""

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def manager(self, temp_dir):
        """ActivatedRepoManager with mocked golden_repo_manager to avoid config_service."""
        golden_mock = MagicMock()
        golden_mock.get_golden_repo.return_value = None
        return ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=golden_mock,
        )

    def _create_repo_on_disk(self, manager, username, user_alias, extra_meta=None):
        """Helper: create the repo directory and metadata file so get_repository() finds it."""
        user_dir = os.path.join(manager.activated_repos_dir, username)
        repo_dir = os.path.join(user_dir, user_alias)
        os.makedirs(repo_dir, exist_ok=True)

        metadata = {
            "user_alias": user_alias,
            "username": username,
            "golden_repo_alias": "golden-repo",
            "path": repo_dir,
            "is_composite": False,
        }
        if extra_meta:
            metadata.update(extra_meta)

        metadata_path = os.path.join(user_dir, f"{user_alias}_metadata.json")
        with open(metadata_path, "w") as f:
            json.dump(metadata, f)

        return metadata

    def test_get_repository_touch_true_stamps_last_accessed(self, manager):
        """touch=True (default) must update last_accessed and call _save_metadata."""
        username = "alice"
        user_alias = "my-repo"
        self._create_repo_on_disk(manager, username, user_alias)

        save_calls = []
        original_save = manager._save_metadata

        def capture_save(u, a, meta):
            save_calls.append((u, a, dict(meta)))
            return original_save(u, a, meta)

        manager._save_metadata = capture_save

        result = manager.get_repository(username, user_alias, touch=True)

        assert result is not None
        assert "last_accessed" in result
        assert len(save_calls) == 1, (
            "Expected exactly one _save_metadata call with touch=True"
        )
        saved_meta = save_calls[0][2]
        assert "last_accessed" in saved_meta

    def test_get_repository_default_touch_stamps_last_accessed(self, manager):
        """Calling get_repository() without the touch kwarg must stamp last_accessed (backwards compat)."""
        username = "alice"
        user_alias = "my-repo"
        self._create_repo_on_disk(manager, username, user_alias)

        save_calls = []
        original_save = manager._save_metadata

        def capture_save(u, a, meta):
            save_calls.append((u, a, dict(meta)))
            return original_save(u, a, meta)

        manager._save_metadata = capture_save

        result = manager.get_repository(username, user_alias)  # default touch=True

        assert result is not None
        assert len(save_calls) == 1
        assert "last_accessed" in save_calls[0][2]

    def test_get_repository_touch_false_does_not_stamp(self, manager):
        """touch=False must return metadata WITHOUT updating last_accessed or calling _save_metadata."""
        username = "alice"
        user_alias = "my-repo"
        old_ts = "2023-01-01T00:00:00+00:00"
        self._create_repo_on_disk(
            manager, username, user_alias, extra_meta={"last_accessed": old_ts}
        )

        save_calls = []
        original_save = manager._save_metadata

        def capture_save(u, a, meta):
            save_calls.append((u, a, dict(meta)))
            return original_save(u, a, meta)

        manager._save_metadata = capture_save

        result = manager.get_repository(username, user_alias, touch=False)

        assert result is not None
        assert len(save_calls) == 0, (
            "Expected zero _save_metadata calls with touch=False"
        )
        # last_accessed must not have been updated
        assert result.get("last_accessed") == old_ts


class TestTouchLastAccessed:
    """Tests for the new touch_last_accessed() throttled method."""

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def manager(self, temp_dir):
        golden_mock = MagicMock()
        golden_mock.get_golden_repo.return_value = None
        return ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=golden_mock,
        )

    def _create_repo_metadata(self, manager, username, user_alias, extra_meta=None):
        """Create metadata file for the repo."""
        user_dir = os.path.join(manager.activated_repos_dir, username)
        os.makedirs(user_dir, exist_ok=True)
        metadata = {
            "user_alias": user_alias,
            "username": username,
            "golden_repo_alias": "golden-repo",
            "is_composite": False,
        }
        if extra_meta:
            metadata.update(extra_meta)
        metadata_path = os.path.join(user_dir, f"{user_alias}_metadata.json")
        with open(metadata_path, "w") as f:
            json.dump(metadata, f)
        return metadata_path

    def test_touch_last_accessed_stamps_when_no_prior(self, manager):
        """touch_last_accessed() on a repo with no last_accessed must write a fresh timestamp."""
        username = "bob"
        user_alias = "my-repo"
        self._create_repo_metadata(manager, username, user_alias)

        save_calls = []
        original_save = manager._save_metadata

        def capture_save(u, a, meta):
            save_calls.append((u, a, dict(meta)))
            return original_save(u, a, meta)

        manager._save_metadata = capture_save

        manager.touch_last_accessed(username, user_alias)

        assert len(save_calls) == 1, (
            "Expected _save_metadata called once for fresh stamp"
        )
        saved_meta = save_calls[0][2]
        assert "last_accessed" in saved_meta

        # Load persisted value and verify it's a valid recent ISO timestamp
        persisted = manager._load_metadata(username, user_alias)
        assert persisted is not None
        ts = persisted.get("last_accessed")
        assert ts is not None
        parsed = datetime.fromisoformat(ts)
        now = datetime.now(timezone.utc)
        assert abs((now - parsed).total_seconds()) < 10

    def test_touch_last_accessed_skips_within_throttle_window(self, manager):
        """touch_last_accessed() with a last_accessed 30 minutes old must NOT call _save_metadata."""
        username = "bob"
        user_alias = "my-repo"
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        self._create_repo_metadata(
            manager, username, user_alias, extra_meta={"last_accessed": recent_ts}
        )

        save_calls = []
        original_save = manager._save_metadata

        def capture_save(u, a, meta):
            save_calls.append((u, a, dict(meta)))
            return original_save(u, a, meta)

        manager._save_metadata = capture_save

        manager.touch_last_accessed(username, user_alias, throttle_seconds=3600)

        assert len(save_calls) == 0, (
            "Expected _save_metadata NOT called when last_accessed is within throttle window"
        )

    def test_touch_last_accessed_stamps_after_throttle_expired(self, manager):
        """touch_last_accessed() with a last_accessed 2 hours old must call _save_metadata."""
        username = "bob"
        user_alias = "my-repo"
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self._create_repo_metadata(
            manager, username, user_alias, extra_meta={"last_accessed": old_ts}
        )

        save_calls = []
        original_save = manager._save_metadata

        def capture_save(u, a, meta):
            save_calls.append((u, a, dict(meta)))
            return original_save(u, a, meta)

        manager._save_metadata = capture_save

        manager.touch_last_accessed(username, user_alias, throttle_seconds=3600)

        assert len(save_calls) == 1, (
            "Expected _save_metadata called once when last_accessed is past throttle window"
        )
        saved_meta = save_calls[0][2]
        # The new timestamp must be fresher than the old one
        new_ts = saved_meta.get("last_accessed")
        assert new_ts is not None
        assert new_ts > old_ts

    def test_touch_last_accessed_nonfatal_on_error(self, manager):
        """touch_last_accessed() must not propagate exceptions from _load_metadata."""
        username = "bob"
        user_alias = "missing-repo"
        # No metadata file created — _load_metadata returns None, method should handle gracefully

        # Should not raise
        manager.touch_last_accessed(username, user_alias)

    def test_touch_last_accessed_nonfatal_when_load_raises(self, manager):
        """touch_last_accessed() must not propagate exceptions when _load_metadata raises."""
        username = "bob"
        user_alias = "my-repo"

        def broken_load(u, a):
            raise RuntimeError("Simulated IO error")

        manager._load_metadata = broken_load

        # Should not raise
        manager.touch_last_accessed(username, user_alias)


class TestReaperNotDefeatedByAdminRead:
    """Integration-style: verify admin read does not defeat the reaper."""

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def manager(self, temp_dir):
        golden_mock = MagicMock()
        golden_mock.get_golden_repo.return_value = None
        return ActivatedRepoManager(
            data_dir=temp_dir,
            golden_repo_manager=golden_mock,
        )

    def test_reaper_not_defeated_by_admin_dashboard_read(self, manager):
        """
        Simulate admin read: activate a repo with old last_accessed, call
        get_repository(touch=False), verify last_accessed is unchanged.
        The reaper would evict a repo with a 3-day-old last_accessed.
        """
        username = "carol"
        user_alias = "my-repo"
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

        # Create the repo directory
        user_dir = os.path.join(manager.activated_repos_dir, username)
        repo_dir = os.path.join(user_dir, user_alias)
        os.makedirs(repo_dir, exist_ok=True)

        # Create metadata with an old last_accessed (reaper bait)
        metadata = {
            "user_alias": user_alias,
            "username": username,
            "golden_repo_alias": "golden-repo",
            "path": repo_dir,
            "is_composite": False,
            "last_accessed": three_days_ago,
        }
        metadata_path = os.path.join(user_dir, f"{user_alias}_metadata.json")
        with open(metadata_path, "w") as f:
            json.dump(metadata, f)

        # Simulate admin dashboard read (touch=False)
        result = manager.get_repository(username, user_alias, touch=False)

        assert result is not None
        # last_accessed must still be the old value — reaper can evict it
        assert result.get("last_accessed") == three_days_ago

        # Also confirm the persisted metadata was NOT updated
        persisted = manager._load_metadata(username, user_alias)
        assert persisted is not None
        assert persisted.get("last_accessed") == three_days_ago
