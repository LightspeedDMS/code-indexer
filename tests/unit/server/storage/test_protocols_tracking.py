"""
Unit tests for tracking and key storage Protocol interfaces (Story #410).

Verifies DescriptionRefreshTrackingBackend, SSHKeysBackend, and
GoldenRepoMetadataBackend Protocols.

Tests use runtime_checkable isinstance() checks.
"""

from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> str:
    """Create and initialise a test SQLite database, return path string."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / "test.db")
    DatabaseSchema(db_path).initialize_database()
    return db_path


# ---------------------------------------------------------------------------
# DescriptionRefreshTrackingBackend
# ---------------------------------------------------------------------------


class TestDescriptionRefreshTrackingBackend:
    def test_sqlite_backend_satisfies_protocol(self, tmp_path: Path) -> None:
        """DescriptionRefreshTrackingBackend (SQLite) must satisfy the Protocol."""
        from code_indexer.server.storage.protocols import (
            DescriptionRefreshTrackingBackend as Protocol,
        )
        from code_indexer.server.storage.sqlite_backends import (
            DescriptionRefreshTrackingBackend as Impl,
        )

        db_path = _make_db(tmp_path)
        backend = Impl(db_path)

        assert isinstance(backend, Protocol)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy the protocol."""
        from code_indexer.server.storage.protocols import (
            DescriptionRefreshTrackingBackend as Protocol,
        )

        class Dummy:
            pass

        assert not isinstance(Dummy(), Protocol)

    def test_protocol_has_required_methods(self) -> None:
        """DescriptionRefreshTrackingBackend protocol must declare all required methods."""
        from code_indexer.server.storage.protocols import (
            DescriptionRefreshTrackingBackend as Protocol,
        )

        required = {
            "get_tracking_record",
            "get_stale_repos",
            "upsert_tracking",
            "delete_tracking",
            "get_all_tracking",
            "close",
        }
        protocol_attrs = set(m for m in dir(Protocol) if not m.startswith("_"))
        assert required.issubset(protocol_attrs)


# ---------------------------------------------------------------------------
# SSHKeysBackend
# ---------------------------------------------------------------------------


class TestSSHKeysBackend:
    def test_sqlite_backend_satisfies_protocol(self, tmp_path: Path) -> None:
        """SSHKeysSqliteBackend must satisfy SSHKeysBackend protocol."""
        from code_indexer.server.storage.protocols import SSHKeysBackend
        from code_indexer.server.storage.sqlite_backends import SSHKeysSqliteBackend

        db_path = _make_db(tmp_path)
        backend = SSHKeysSqliteBackend(db_path)

        assert isinstance(backend, SSHKeysBackend)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy SSHKeysBackend."""
        from code_indexer.server.storage.protocols import SSHKeysBackend

        class Dummy:
            pass

        assert not isinstance(Dummy(), SSHKeysBackend)

    def test_protocol_has_required_methods(self) -> None:
        """SSHKeysBackend must declare all required public methods."""
        from code_indexer.server.storage.protocols import SSHKeysBackend

        required = {
            "create_key",
            "get_key",
            "assign_host",
            "remove_host",
            "delete_key",
            "list_keys",
            "close",
        }
        protocol_attrs = set(m for m in dir(SSHKeysBackend) if not m.startswith("_"))
        assert required.issubset(protocol_attrs)


# ---------------------------------------------------------------------------
# GoldenRepoMetadataBackend
# ---------------------------------------------------------------------------


class TestGoldenRepoMetadataBackend:
    def test_sqlite_backend_satisfies_protocol(self, tmp_path: Path) -> None:
        """GoldenRepoMetadataSqliteBackend must satisfy GoldenRepoMetadataBackend protocol."""
        from code_indexer.server.storage.protocols import GoldenRepoMetadataBackend
        from code_indexer.server.storage.sqlite_backends import (
            GoldenRepoMetadataSqliteBackend,
        )

        db_path = _make_db(tmp_path)
        backend = GoldenRepoMetadataSqliteBackend(db_path)

        assert isinstance(backend, GoldenRepoMetadataBackend)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy GoldenRepoMetadataBackend."""
        from code_indexer.server.storage.protocols import GoldenRepoMetadataBackend

        class Dummy:
            pass

        assert not isinstance(Dummy(), GoldenRepoMetadataBackend)

    def test_protocol_has_required_methods(self) -> None:
        """GoldenRepoMetadataBackend must declare all required public methods."""
        from code_indexer.server.storage.protocols import GoldenRepoMetadataBackend

        required = {
            "ensure_table_exists",
            "add_repo",
            "get_repo",
            "list_repos",
            "remove_repo",
            "repo_exists",
            "update_enable_temporal",
            "update_repo_url",
            "update_category",
            "update_wiki_enabled",
            "update_default_branch",
            "invalidate_description_refresh_tracking",
            "invalidate_dependency_map_tracking",
            "list_repos_with_categories",
            "close",
        }
        protocol_attrs = set(
            m for m in dir(GoldenRepoMetadataBackend) if not m.startswith("_")
        )
        assert required.issubset(protocol_attrs)
