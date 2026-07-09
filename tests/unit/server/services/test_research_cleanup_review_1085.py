"""Bug #1085 code-review regressions — TWO BLOCKING data-loss defects.

These tests reproduce (RED) then lock in the fixes for the two blocking
defects found in code review of the Research Assistant workspace GC:

BLOCKING-1 (cluster/postgres mass-deletion of LIVE sessions): the live-session
provider must source the live set from the SAME ``research_sessions`` backend
the writers use (PostgreSQL in cluster, SQLite in solo) -- NOT a hardcoded
SQLite path. In postgres mode the stray SQLite ``research_sessions`` table is
EMPTY, so the old SQLite-only provider returned ``set()`` with no exception and
EVERY aged research dir was treated as an orphan and DELETED.

BLOCKING-2 (no session-id shape guard): a directory must parse as a
``uuid.UUID`` before it is ever a deletion candidate. ``default`` is preserved.
A non-UUID dir (e.g. ``important-do-not-delete``) is skipped + logged, never
``rmtree``-d.

Plus N-1 (symlink-safe recency): ``_is_recently_modified`` / ``_dir_size`` must
NOT follow symlinks into real-repo targets when computing recency/size.
"""

import logging
import os
import time
from pathlib import Path

import pytest

from code_indexer.server.services.research_cleanup_service import (
    ResearchCleanupService,
    make_backend_live_folder_provider,
)


def _make_session_dir(base: Path, name: str, age_days: float = 0.0) -> Path:
    """Create research/<name>/ with an uploads file and an aged mtime."""
    d = base / name
    (d / "uploads").mkdir(parents=True, exist_ok=True)
    (d / "uploads" / "file0.txt").write_text("x")
    if age_days > 0:
        old = time.time() - age_days * 24 * 3600
        os.utime(d / "uploads" / "file0.txt", (old, old))
        os.utime(d / "uploads", (old, old))
        os.utime(d, (old, old))
    return d


class _FakeBackend:
    """Minimal stand-in for a ResearchSessionsBackend (SQLite OR Postgres).

    ``list_sessions()`` is the SAME method both real backends expose; the GC
    must consume it via the registry instead of opening a hardcoded SQLite DB.
    """

    def __init__(self, folder_paths):
        self._folder_paths = list(folder_paths)
        self.calls = 0

    def list_sessions(self):
        self.calls += 1
        return [{"id": "x", "folder_path": p} for p in self._folder_paths]


class _RaisingBackend:
    def list_sessions(self):
        raise RuntimeError("postgres pool unavailable")


class TestBackendAwareLiveSet:
    """BLOCKING-1: live set comes from the active backend, not a SQLite path."""

    def test_postgres_mode_does_not_delete_live_dirs(self, tmp_path):
        """Cluster mode: PG-backed registry HAS live sessions; a stray EMPTY
        SQLite research_sessions also exists. The GC must delete ZERO live dirs.

        This is the critical regression: the old SQLite-only provider read the
        empty table -> set() -> every aged dir looked like an orphan -> DELETED.
        """
        base = tmp_path / "research"
        live = _make_session_dir(
            base, "a1b2c3d4-0000-4000-8000-000000000001", age_days=100
        )

        # The active backend (postgres in cluster) knows the live session.
        backend = _FakeBackend({str(live)})
        provider = make_backend_live_folder_provider(lambda: backend)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=provider,
        )
        result = svc.cleanup()

        assert live.exists(), (
            "LIVE session dir known to the active backend must NEVER be deleted"
        )
        assert result.dirs_deleted == 0
        assert result.dirs_preserved == 1
        assert backend.calls == 1, "Provider must query the active backend"

    def test_backend_missing_is_failsafe_no_deletion(self, tmp_path):
        """Fail-safe: if the active backend is missing (None), delete NOTHING."""
        base = tmp_path / "research"
        orphan = _make_session_dir(
            base, "a1b2c3d4-0000-4000-8000-000000000002", age_days=100
        )

        provider = make_backend_live_folder_provider(lambda: None)
        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=provider,
        )
        result = svc.cleanup()  # must NOT raise

        assert orphan.exists(), (
            "Missing backend => untrustworthy live set => no deletions"
        )
        assert result.dirs_deleted == 0
        assert result.dirs_scanned == 0

    def test_backend_raises_is_failsafe_no_deletion(self, tmp_path):
        """Fail-safe: if the backend raises, the sweep aborts with zero deletions."""
        base = tmp_path / "research"
        orphan = _make_session_dir(
            base, "a1b2c3d4-0000-4000-8000-000000000003", age_days=100
        )

        provider = make_backend_live_folder_provider(lambda: _RaisingBackend())
        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=provider,
        )
        result = svc.cleanup()  # must NOT raise

        assert orphan.exists(), "Backend read error must never trigger deletion"
        assert result.dirs_deleted == 0
        assert result.dirs_scanned == 0

    def test_backend_supplier_raises_is_failsafe(self, tmp_path):
        """Fail-safe: if even resolving the backend raises, delete NOTHING."""
        base = tmp_path / "research"
        orphan = _make_session_dir(
            base, "a1b2c3d4-0000-4000-8000-000000000004", age_days=100
        )

        def _supplier():
            raise RuntimeError("registry not wired")

        provider = make_backend_live_folder_provider(_supplier)
        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=provider,
        )
        result = svc.cleanup()  # must NOT raise

        assert orphan.exists()
        assert result.dirs_deleted == 0
        assert result.dirs_scanned == 0


class TestUuidShapeGuard:
    """BLOCKING-2: only uuid-shaped dirs are deletion candidates."""

    def test_non_uuid_dir_never_deleted(self, tmp_path, caplog):
        """An aged non-UUID dir (``important-do-not-delete``) is NEVER deleted."""
        base = tmp_path / "research"
        protected = _make_session_dir(base, "important-do-not-delete", age_days=100)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),  # no live rows at all
        )
        with caplog.at_level(logging.WARNING):
            result = svc.cleanup()

        assert protected.exists(), "Non-session-shaped dir must never be rmtree'd"
        assert result.dirs_deleted == 0
        assert any(
            "important-do-not-delete" in rec.getMessage() for rec in caplog.records
        ), "Skipping a non-UUID dir must be logged"

    def test_default_session_dir_preserved(self, tmp_path):
        """The literal ``default`` session dir is preserved even when aged/orphan."""
        base = tmp_path / "research"
        default = _make_session_dir(base, "default", age_days=100)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),
        )
        result = svc.cleanup()

        assert default.exists(), "'default' session dir must be preserved"
        assert result.dirs_deleted == 0

    def test_aged_uuid_orphan_still_deleted(self, tmp_path):
        """A genuinely orphan, aged, uuid-shaped dir IS still deleted (no over-block)."""
        base = tmp_path / "research"
        orphan = _make_session_dir(
            base, "a1b2c3d4-0000-4000-8000-0000000000aa", age_days=100
        )

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),
        )
        result = svc.cleanup()

        assert not orphan.exists(), "Aged uuid-shaped orphan must still be reaped"
        assert result.dirs_deleted == 1


class TestSymlinkSafeRecency:
    """N-1: recency/size must not follow symlinks into real-repo targets."""

    def test_orphan_with_symlinked_recent_target_is_eligible(self, tmp_path):
        """An orphan whose only 'recent' content is a SYMLINK to a freshly
        touched real repo dir is still eligible (recency is symlink-safe)."""
        base = tmp_path / "research"
        orphan = _make_session_dir(
            base, "a1b2c3d4-0000-4000-8000-0000000000bb", age_days=100
        )

        # A real, fresh repo outside the session (e.g. the code-indexer symlink).
        real_repo = tmp_path / "real_repo"
        real_repo.mkdir()
        (real_repo / "fresh.txt").write_text("just now")  # mtime == now

        link = orphan / "code-indexer"
        link.symlink_to(real_repo)
        # Re-age the dir + uploads so the new symlink entry itself is old.
        old = time.time() - 100 * 24 * 3600
        os.utime(orphan / "uploads", (old, old))
        os.utime(orphan, (old, old))
        os.utime(link, (old, old), follow_symlinks=False)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),
        )
        result = svc.cleanup()

        assert not orphan.exists(), (
            "Recency must NOT follow the symlink to the fresh real repo; "
            "the aged orphan stays eligible"
        )
        assert result.dirs_deleted == 1
        # The real repo must be untouched by the sweep.
        assert real_repo.exists() and (real_repo / "fresh.txt").exists()

    def test_genuinely_recent_session_still_protected(self, tmp_path):
        """A session dir with a real (non-symlink) fresh inner file is protected."""
        base = tmp_path / "research"
        d = _make_session_dir(
            base, "a1b2c3d4-0000-4000-8000-0000000000cc", age_days=100
        )
        (d / "uploads" / "just_uploaded.txt").write_text("fresh")  # mtime == now

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),
        )
        result = svc.cleanup()

        assert d.exists(), "A genuinely recently-written session must be protected"
        assert result.dirs_deleted == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
