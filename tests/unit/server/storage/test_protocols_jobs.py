"""
Unit tests for job-related storage Protocol interfaces (Story #410).

Verifies BackgroundJobsBackend, SyncJobsBackend, and CITokensBackend Protocols.

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
# BackgroundJobsBackend
# ---------------------------------------------------------------------------


class TestBackgroundJobsBackend:
    def test_sqlite_backend_satisfies_protocol(self, tmp_path: Path) -> None:
        """BackgroundJobsSqliteBackend must satisfy BackgroundJobsBackend protocol."""
        from code_indexer.server.storage.protocols import BackgroundJobsBackend
        from code_indexer.server.storage.sqlite_backends import (
            BackgroundJobsSqliteBackend,
        )

        db_path = _make_db(tmp_path)
        backend = BackgroundJobsSqliteBackend(db_path)

        assert isinstance(backend, BackgroundJobsBackend)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy BackgroundJobsBackend."""
        from code_indexer.server.storage.protocols import BackgroundJobsBackend

        class Dummy:
            pass

        assert not isinstance(Dummy(), BackgroundJobsBackend)

    def test_protocol_has_required_methods(self) -> None:
        """BackgroundJobsBackend must declare all required public methods."""
        from code_indexer.server.storage.protocols import BackgroundJobsBackend

        required = {
            "save_job",
            "get_job",
            "update_job",
            "list_jobs",
            "list_jobs_filtered",
            "delete_job",
            "cleanup_old_jobs",
            "count_jobs_by_status",
            "get_job_stats",
            "cleanup_orphaned_jobs_on_startup",
            "close",
        }
        protocol_attrs = set(
            m for m in dir(BackgroundJobsBackend) if not m.startswith("_")
        )
        assert required.issubset(protocol_attrs)


# ---------------------------------------------------------------------------
# SyncJobsBackend
# ---------------------------------------------------------------------------


class TestSyncJobsBackend:
    def test_sqlite_backend_satisfies_protocol(self, tmp_path: Path) -> None:
        """SyncJobsSqliteBackend must satisfy SyncJobsBackend protocol."""
        from code_indexer.server.storage.protocols import SyncJobsBackend
        from code_indexer.server.storage.sqlite_backends import SyncJobsSqliteBackend

        db_path = _make_db(tmp_path)
        backend = SyncJobsSqliteBackend(db_path)

        assert isinstance(backend, SyncJobsBackend)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy SyncJobsBackend."""
        from code_indexer.server.storage.protocols import SyncJobsBackend

        class Dummy:
            pass

        assert not isinstance(Dummy(), SyncJobsBackend)

    def test_protocol_has_required_methods(self) -> None:
        """SyncJobsBackend must declare all required public methods."""
        from code_indexer.server.storage.protocols import SyncJobsBackend

        required = {
            "create_job",
            "get_job",
            "update_job",
            "list_jobs",
            "delete_job",
            "cleanup_orphaned_jobs_on_startup",
            "close",
        }
        protocol_attrs = set(m for m in dir(SyncJobsBackend) if not m.startswith("_"))
        assert required.issubset(protocol_attrs)


# ---------------------------------------------------------------------------
# CITokensBackend
# ---------------------------------------------------------------------------


class TestCITokensBackend:
    def test_sqlite_backend_satisfies_protocol(self, tmp_path: Path) -> None:
        """CITokensSqliteBackend must satisfy CITokensBackend protocol."""
        from code_indexer.server.storage.protocols import CITokensBackend
        from code_indexer.server.storage.sqlite_backends import CITokensSqliteBackend

        db_path = _make_db(tmp_path)
        backend = CITokensSqliteBackend(db_path)

        assert isinstance(backend, CITokensBackend)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy CITokensBackend."""
        from code_indexer.server.storage.protocols import CITokensBackend

        class Dummy:
            pass

        assert not isinstance(Dummy(), CITokensBackend)

    def test_protocol_has_required_methods(self) -> None:
        """CITokensBackend must declare all required public methods."""
        from code_indexer.server.storage.protocols import CITokensBackend

        required = {
            "save_token",
            "get_token",
            "delete_token",
            "list_tokens",
            "close",
        }
        protocol_attrs = set(m for m in dir(CITokensBackend) if not m.startswith("_"))
        assert required.issubset(protocol_attrs)
