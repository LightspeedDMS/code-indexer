"""
Tests for Bug #299: INSERT OR REPLACE in GlobalReposSqliteBackend.register_repo()
silently wipes next_refresh.

Acceptance Criteria:
- AC1: register_repo() uses ON CONFLICT(alias_name) DO UPDATE SET instead of
       INSERT OR REPLACE, preserving next_refresh and created_at on conflict
- AC2: Re-registering an existing repo does NOT wipe next_refresh
- AC3: Re-registering an existing repo does NOT reset created_at
- AC4: First-time registration still works correctly (INSERT path)

Uses real SQLite databases — no mocking.
"""

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import GlobalReposSqliteBackend


def _init_db(tmp_path: Path) -> tuple[str, GlobalReposSqliteBackend]:
    """Initialize a test database and return (db_path_str, backend)."""
    db_path = tmp_path / "test_upsert.db"
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    backend = GlobalReposSqliteBackend(str(db_path))
    return str(db_path), backend


class TestRegisterRepoUpsert:
    """Tests for proper UPSERT behaviour in register_repo() (Bug #299)."""

    def test_first_registration_inserts_correctly(self, tmp_path):
        """AC4: First-time registration inserts a row with all expected fields."""
        _, backend = _init_db(tmp_path)

        backend.register_repo(
            alias_name="new-repo-global",
            repo_name="new-repo",
            repo_url="https://github.com/org/new-repo.git",
            index_path="/golden-repos/new-repo",
            enable_temporal=True,
            temporal_options={"branches": ["main"]},
            enable_scip=True,
        )

        result = backend.get_repo("new-repo-global")

        assert result is not None, "Repo should be found after first registration"
        assert result["alias_name"] == "new-repo-global"
        assert result["repo_name"] == "new-repo"
        assert result["repo_url"] == "https://github.com/org/new-repo.git"
        assert result["index_path"] == "/golden-repos/new-repo"
        assert result["enable_temporal"] is True
        assert result["temporal_options"] == {"branches": ["main"]}
        assert result["enable_scip"] is True
        assert result["created_at"] is not None
        assert result["last_refresh"] is not None
        assert result["next_refresh"] is None  # Not set on first registration

    def test_re_registration_preserves_next_refresh(self, tmp_path):
        """AC2: Re-registering an existing repo does NOT wipe next_refresh."""
        _, backend = _init_db(tmp_path)

        # First registration
        backend.register_repo(
            alias_name="existing-repo-global",
            repo_name="existing-repo",
            repo_url="https://github.com/org/existing.git",
            index_path="/golden-repos/existing-repo",
        )

        # Set next_refresh via update method
        future_time = time.time() + 3600
        success = backend.update_next_refresh("existing-repo-global", str(future_time))
        assert success is True

        # Verify it was set
        before = backend.get_repo("existing-repo-global")
        assert before is not None
        assert before["next_refresh"] is not None
        stored_before = float(before["next_refresh"])
        assert abs(stored_before - future_time) < 0.001

        # Re-register the same alias (simulates GlobalActivator re-activating)
        backend.register_repo(
            alias_name="existing-repo-global",
            repo_name="existing-repo",
            repo_url="https://github.com/org/existing.git",
            index_path="/golden-repos/existing-repo",
        )

        # next_refresh MUST be preserved — Bug #299 fix
        after = backend.get_repo("existing-repo-global")
        assert after is not None, "Repo must still exist after re-registration"
        assert after["next_refresh"] is not None, (
            "next_refresh was wiped by re-registration (Bug #299)"
        )
        stored_after = float(after["next_refresh"])
        assert abs(stored_after - future_time) < 0.001, (
            f"next_refresh changed: expected {future_time}, got {stored_after}"
        )

    def test_re_registration_preserves_created_at(self, tmp_path):
        """AC3: Re-registering an existing repo does NOT reset created_at."""
        _, backend = _init_db(tmp_path)

        # First registration
        backend.register_repo(
            alias_name="stable-repo-global",
            repo_name="stable-repo",
            repo_url="https://github.com/org/stable.git",
            index_path="/golden-repos/stable-repo",
        )

        first = backend.get_repo("stable-repo-global")
        assert first is not None
        original_created_at = first["created_at"]
        assert original_created_at is not None

        # Small pause to ensure a different timestamp would be generated
        # if the code re-sets created_at (which it must NOT do)
        time.sleep(0.01)

        # Re-register
        backend.register_repo(
            alias_name="stable-repo-global",
            repo_name="stable-repo",
            repo_url="https://github.com/org/stable.git",
            index_path="/golden-repos/stable-repo",
        )

        second = backend.get_repo("stable-repo-global")
        assert second is not None
        assert second["created_at"] == original_created_at, (
            f"created_at was reset by re-registration: "
            f"original={original_created_at!r}, after={second['created_at']!r}"
        )

    def test_re_registration_updates_mutable_fields(self, tmp_path):
        """AC1: Re-registering updates repo_name, repo_url, index_path,
        last_refresh, enable_temporal, temporal_options, and enable_scip."""
        _, backend = _init_db(tmp_path)

        # First registration with initial values
        backend.register_repo(
            alias_name="mutable-repo-global",
            repo_name="mutable-repo",
            repo_url="https://github.com/org/mutable.git",
            index_path="/golden-repos/mutable-repo-v1",
            enable_temporal=False,
            temporal_options=None,
            enable_scip=False,
        )

        first = backend.get_repo("mutable-repo-global")
        assert first is not None
        first_last_refresh = first["last_refresh"]

        # Small pause to ensure last_refresh timestamp would differ
        time.sleep(0.01)

        # Re-register with updated values
        backend.register_repo(
            alias_name="mutable-repo-global",
            repo_name="mutable-repo-renamed",
            repo_url="https://github.com/org/mutable-new.git",
            index_path="/golden-repos/mutable-repo-v2",
            enable_temporal=True,
            temporal_options={"branches": ["main", "dev"]},
            enable_scip=True,
        )

        second = backend.get_repo("mutable-repo-global")
        assert second is not None

        assert second["repo_name"] == "mutable-repo-renamed", (
            "repo_name must be updated on re-registration"
        )
        assert second["repo_url"] == "https://github.com/org/mutable-new.git", (
            "repo_url must be updated on re-registration"
        )
        assert second["index_path"] == "/golden-repos/mutable-repo-v2", (
            "index_path must be updated on re-registration"
        )
        assert second["enable_temporal"] is True, (
            "enable_temporal must be updated on re-registration"
        )
        assert second["temporal_options"] == {"branches": ["main", "dev"]}, (
            "temporal_options must be updated on re-registration"
        )
        assert second["enable_scip"] is True, (
            "enable_scip must be updated on re-registration"
        )
        # last_refresh should also be updated (it's a mutable field)
        assert second["last_refresh"] != first_last_refresh, (
            "last_refresh must be updated on re-registration"
        )
