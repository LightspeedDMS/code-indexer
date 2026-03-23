"""
Unit tests for miscellaneous storage Protocol interfaces (Story #410).

Verifies DependencyMapTrackingBackend, GitCredentialsBackend, and
RepoCategoryBackend Protocols.

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
# DependencyMapTrackingBackend
# ---------------------------------------------------------------------------


class TestDependencyMapTrackingBackend:
    def test_sqlite_backend_satisfies_protocol(self, tmp_path: Path) -> None:
        """DependencyMapTrackingBackend (SQLite) must satisfy the Protocol."""
        from code_indexer.server.storage.protocols import (
            DependencyMapTrackingBackend as Protocol,
        )
        from code_indexer.server.storage.sqlite_backends import (
            DependencyMapTrackingBackend as Impl,
        )

        db_path = _make_db(tmp_path)
        backend = Impl(db_path)

        assert isinstance(backend, Protocol)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy the protocol."""
        from code_indexer.server.storage.protocols import (
            DependencyMapTrackingBackend as Protocol,
        )

        class Dummy:
            pass

        assert not isinstance(Dummy(), Protocol)

    def test_protocol_has_required_methods(self) -> None:
        """DependencyMapTrackingBackend protocol must declare all required methods."""
        from code_indexer.server.storage.protocols import (
            DependencyMapTrackingBackend as Protocol,
        )

        required = {
            "get_tracking",
            "update_tracking",
            "cleanup_stale_status_on_startup",
            "record_run_metrics",
            "get_run_history",
            "close",
        }
        protocol_attrs = set(m for m in dir(Protocol) if not m.startswith("_"))
        assert required.issubset(protocol_attrs)


# ---------------------------------------------------------------------------
# GitCredentialsBackend
# ---------------------------------------------------------------------------


class TestGitCredentialsBackend:
    def test_sqlite_backend_satisfies_protocol(self, tmp_path: Path) -> None:
        """GitCredentialsSqliteBackend must satisfy GitCredentialsBackend protocol."""
        from code_indexer.server.storage.protocols import GitCredentialsBackend
        from code_indexer.server.storage.sqlite_backends import (
            GitCredentialsSqliteBackend,
        )

        db_path = _make_db(tmp_path)
        backend = GitCredentialsSqliteBackend(db_path)

        assert isinstance(backend, GitCredentialsBackend)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy GitCredentialsBackend."""
        from code_indexer.server.storage.protocols import GitCredentialsBackend

        class Dummy:
            pass

        assert not isinstance(Dummy(), GitCredentialsBackend)

    def test_protocol_has_required_methods(self) -> None:
        """GitCredentialsBackend must declare all required public methods."""
        from code_indexer.server.storage.protocols import GitCredentialsBackend

        required = {
            "upsert_credential",
            "list_credentials",
            "delete_credential",
            "get_credential_for_host",
            "close",
        }
        protocol_attrs = set(
            m for m in dir(GitCredentialsBackend) if not m.startswith("_")
        )
        assert required.issubset(protocol_attrs)


# ---------------------------------------------------------------------------
# RepoCategoryBackend
# ---------------------------------------------------------------------------


class TestRepoCategoryBackend:
    def test_sqlite_backend_satisfies_protocol(self, tmp_path: Path) -> None:
        """RepoCategorySqliteBackend must satisfy RepoCategoryBackend protocol."""
        from code_indexer.server.storage.protocols import RepoCategoryBackend
        from code_indexer.server.storage.repo_category_backend import (
            RepoCategorySqliteBackend,
        )

        db_path = _make_db(tmp_path)
        backend = RepoCategorySqliteBackend(db_path)

        assert isinstance(backend, RepoCategoryBackend)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy RepoCategoryBackend."""
        from code_indexer.server.storage.protocols import RepoCategoryBackend

        class Dummy:
            pass

        assert not isinstance(Dummy(), RepoCategoryBackend)

    def test_protocol_has_required_methods(self) -> None:
        """RepoCategoryBackend must declare all required public methods."""
        from code_indexer.server.storage.protocols import RepoCategoryBackend

        required = {
            "create_category",
            "list_categories",
            "get_category",
            "update_category",
            "delete_category",
            "reorder_categories",
            "shift_all_priorities",
            "get_next_priority",
            "get_repo_category_map",
            "close",
        }
        protocol_attrs = set(
            m for m in dir(RepoCategoryBackend) if not m.startswith("_")
        )
        assert required.issubset(protocol_attrs)
