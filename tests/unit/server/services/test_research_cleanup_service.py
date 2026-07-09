"""
Bug #1085 — Part B: server-side automatic GC for Research Assistant workspaces.

Mirrors the SCIP ``WorkspaceCleanupService`` pattern: a sweep that reconciles
``~/.cidx-server/research/<uuid>`` directories against the live
``research_sessions`` registry and deletes orphaned, aged directories — while
NEVER deleting a directory that maps to a live session row.

Following TDD: these tests fail until ResearchCleanupService is implemented.
"""

import os
import time
from pathlib import Path

from code_indexer.server.services.research_cleanup_service import (
    ResearchCleanupService,
)


def _make_session_dir(base: Path, name: str, age_days: float = 0.0) -> Path:
    """Create research/<name>/ with an uploads file and an aged mtime."""
    d = base / name
    (d / "uploads").mkdir(parents=True, exist_ok=True)
    (d / "uploads" / "file0.txt").write_text("x")
    if age_days > 0:
        old = time.time() - age_days * 24 * 3600
        # Age both the file and the dir so recent-modification guard passes.
        os.utime(d / "uploads" / "file0.txt", (old, old))
        os.utime(d / "uploads", (old, old))
        os.utime(d, (old, old))
    return d


class TestResearchCleanupService:
    """Bug #1085 Part B: orphan reconciliation + TTL sweep with live-row safety."""

    def test_orphan_dir_removed(self, tmp_path):
        """An aged orphan (no live DB row) is deleted."""
        base = tmp_path / "research"
        orphan = _make_session_dir(
            base, "aaaaaaaa-0000-4000-8000-000000000001", age_days=10
        )

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),  # no live sessions
        )
        result = svc.cleanup()

        assert not orphan.exists(), "Aged orphan dir must be deleted"
        assert result.dirs_deleted == 1
        assert result.dirs_scanned == 1

    def test_live_session_dir_preserved(self, tmp_path):
        """A dir mapping to a live DB row is NEVER deleted, even if aged."""
        base = tmp_path / "research"
        live = _make_session_dir(base, "bbbbbbbb-live", age_days=100)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: {str(live)},
        )
        result = svc.cleanup()

        assert live.exists(), "Live-session dir must be preserved"
        assert result.dirs_deleted == 0
        assert result.dirs_preserved == 1

    def test_recent_orphan_preserved(self, tmp_path):
        """A freshly-modified orphan is skipped (avoid racing an in-flight create)."""
        base = tmp_path / "research"
        recent = _make_session_dir(base, "cccccccc-recent", age_days=0)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=0.0001,  # tiny retention so age alone wouldn't save it
            live_folder_provider=lambda: set(),
        )
        result = svc.cleanup()

        assert recent.exists(), "Recently-modified orphan must be preserved"
        assert result.dirs_deleted == 0

    def test_fresh_orphan_within_retention_preserved(self, tmp_path):
        """An orphan younger than retention is kept (TTL not yet reached)."""
        base = tmp_path / "research"
        young = _make_session_dir(base, "dddddddd-young", age_days=1)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=7,
            live_folder_provider=lambda: set(),
        )
        result = svc.cleanup()

        assert young.exists(), "Orphan within retention must be preserved"
        assert result.dirs_deleted == 0

    def test_deletion_failure_logged_not_swallowed(self, tmp_path, caplog):
        """A deletion failure is recorded + logged, never raised (Messi #13)."""
        import logging

        base = tmp_path / "research"
        orphan = _make_session_dir(
            base, "eeeeeeee-0000-4000-8000-00000000000f", age_days=10
        )

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),
        )

        from unittest.mock import patch

        with caplog.at_level(logging.ERROR):
            with patch(
                "code_indexer.server.services.research_cleanup_service.shutil.rmtree",
                side_effect=OSError("boom"),
            ):
                result = svc.cleanup()  # must NOT raise

        assert orphan.exists(), "Dir remains because deletion failed"
        assert len(result.errors) == 1, "Failure must be recorded in result.errors"
        assert str(orphan) in result.errors[0]
        assert any(
            "boom" in rec.getMessage() or str(orphan) in rec.getMessage()
            for rec in caplog.records
        ), "Deletion failure must be logged with the path"

    def test_retention_disabled_noop(self, tmp_path):
        """retention_days <= 0 disables the sweep entirely (kill switch)."""
        base = tmp_path / "research"
        orphan = _make_session_dir(base, "ffffffff-disabled", age_days=100)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=0,
            live_folder_provider=lambda: set(),
        )
        result = svc.cleanup()

        assert orphan.exists(), "Disabled sweep must not delete anything"
        assert result.dirs_deleted == 0
        assert result.dirs_scanned == 0

    def test_missing_base_dir_is_safe(self, tmp_path):
        """A non-existent research base dir is a no-op, not a crash."""
        svc = ResearchCleanupService(
            research_base_dir=tmp_path / "does-not-exist",
            retention_days=3,
            live_folder_provider=lambda: set(),
        )
        result = svc.cleanup()  # must not raise
        assert result.dirs_scanned == 0
        assert result.dirs_deleted == 0

    def test_bounded_scan_cap(self, tmp_path):
        """The sweep is bounded by max_dirs_per_run (Messi #14)."""
        base = tmp_path / "research"
        for i in range(5):
            _make_session_dir(base, f"cap-{i:08d}", age_days=10)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),
            max_dirs_per_run=2,
        )
        result = svc.cleanup()

        assert result.dirs_scanned <= 2, "Scan must respect max_dirs_per_run bound"

    def test_provider_failure_aborts_with_no_deletions(self, tmp_path):
        """If the live-row provider raises, the sweep aborts and deletes nothing."""
        base = tmp_path / "research"
        orphan = _make_session_dir(base, "99999999-provfail", age_days=100)

        def _boom():
            raise RuntimeError("registry unreadable")

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=_boom,
        )
        result = svc.cleanup()  # must NOT raise

        assert orphan.exists(), "Unreadable registry must never trigger deletion"
        assert result.dirs_deleted == 0
        assert result.dirs_scanned == 0

    def test_aged_dir_with_recent_inner_file_preserved(self, tmp_path):
        """An aged dir with a freshly-touched inner file is preserved (recent activity)."""
        base = tmp_path / "research"
        d = _make_session_dir(base, "55555555-mixed", age_days=100)
        # Directory + existing files are old, but a NEW upload just landed.
        fresh = d / "uploads" / "just_uploaded.txt"
        fresh.write_text("fresh")  # mtime == now

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),
        )
        result = svc.cleanup()

        assert d.exists(), "Dir with a recent inner file must be preserved"
        assert result.dirs_deleted == 0
        assert result.dirs_preserved == 1

    def test_broken_symlink_inside_dir_is_skipped_gracefully(self, tmp_path):
        """A dangling symlink inside an aged orphan is skipped; the orphan deletes."""
        base = tmp_path / "research"
        d = _make_session_dir(
            base, "44444444-0000-4000-8000-000000000044", age_days=100
        )
        # A real broken symlink: stat() on it raises OSError during the walks.
        dangling = d / "uploads" / "dead_link"
        dangling.symlink_to(d / "uploads" / "no_such_target")
        # Re-age the dir AND the new symlink so nothing looks recent. Recency is
        # now symlink-safe (lstat), so the dangling link's OWN mtime counts and
        # must be aged like the rest of this aged orphan (Bug #1085 N-1).
        old = time.time() - 100 * 24 * 3600
        os.utime(d, (old, old))
        os.utime(d / "uploads", (old, old))
        os.utime(dangling, (old, old), follow_symlinks=False)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),
        )
        result = svc.cleanup()  # must NOT raise on the dangling link

        assert not d.exists(), "Aged orphan must delete despite a broken inner symlink"
        assert result.dirs_deleted == 1

    def test_dir_removed_before_scan_is_graceful(self, tmp_path):
        """If a concurrent delete_session removes a dir before the scan, sweep is safe.

        ``cleanup()`` reads the live registry (provider) BEFORE scanning the
        filesystem, so a dir removed during that window is simply absent from the
        scan -- the sweep must complete with zero deletions and zero errors,
        never raising.
        """
        import shutil as _shutil

        base = tmp_path / "research"
        orphan = _make_session_dir(base, "77777777-vanish", age_days=100)

        def _provider_that_removes():
            # Models a concurrent DELETE /admin/research/sessions/{id} firing
            # between the registry read and the filesystem scan.
            if orphan.exists():
                _shutil.rmtree(orphan)
            return set()

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=_provider_that_removes,
        )
        result = svc.cleanup()  # must NOT raise

        assert not orphan.exists()
        assert result.dirs_deleted == 0, "Vanished dir is not counted as deleted"
        assert result.dirs_scanned == 0, "Dir removed before scan is absent"
        assert result.errors == [], "No spurious deletion error must be recorded"


class TestResearchCleanupSchedulerErrorPaths:
    """Bug #1085 Part B: scheduler error paths never crash and never over-delete."""

    def test_retention_provider_error_skips_cycle(self, tmp_path):
        """A retention-config read error yields a safe empty result, no deletion."""
        from code_indexer.server.services.research_cleanup_service import (
            ResearchCleanupScheduler,
        )

        base = tmp_path / "research"
        orphan = _make_session_dir(base, "88888888-cfgerr", age_days=100)

        def _bad_retention():
            raise RuntimeError("config service down")

        sched = ResearchCleanupScheduler(
            research_base_dir=base,
            retention_days_provider=_bad_retention,
            live_folder_provider=lambda: set(),
            interval_seconds=3600,
        )
        result = sched._run_one_sweep()  # direct call exercises the cycle safely

        assert orphan.exists(), "Config error must not delete anything"
        assert result.dirs_deleted == 0
        assert result.dirs_scanned == 0


class TestResearchRetentionConfigKnob:
    """Bug #1085 Part B: named retention knob on ServerConfig (no magic literal)."""

    def test_server_config_has_research_retention_default(self, tmp_path):
        """ServerConfig exposes research_session_retention_days with a sane default."""
        from code_indexer.server.utils.config_manager import ServerConfig

        cfg = ServerConfig(server_dir=str(tmp_path))
        assert hasattr(cfg, "research_session_retention_days")
        assert cfg.research_session_retention_days == 7

    def test_server_config_research_retention_is_overridable(self, tmp_path):
        """The retention knob can be set (Web-UI tunable), including disable (0)."""
        from code_indexer.server.utils.config_manager import ServerConfig

        cfg = ServerConfig(server_dir=str(tmp_path), research_session_retention_days=0)
        assert cfg.research_session_retention_days == 0


class TestResearchCleanupScheduler:
    """Bug #1085 Part B: daemon scheduler runs the sweep on start + on interval."""

    def test_scheduler_runs_cleanup_on_start(self, tmp_path):
        """Starting the scheduler performs startup reconciliation immediately."""
        import threading

        from code_indexer.server.services.research_cleanup_service import (
            ResearchCleanupScheduler,
        )

        base = tmp_path / "research"
        orphan = _make_session_dir(
            base, "11111111-0000-4000-8000-000000000011", age_days=10
        )

        ran = threading.Event()

        def _provider():
            ran.set()
            return set()

        sched = ResearchCleanupScheduler(
            research_base_dir=base,
            retention_days_provider=lambda: 3,
            live_folder_provider=_provider,
            interval_seconds=3600,
        )
        sched.start()
        try:
            assert ran.wait(timeout=5), "Scheduler must run cleanup on start"
            # Give the sweep a moment to complete the deletion.
            deadline = time.time() + 5
            while orphan.exists() and time.time() < deadline:
                time.sleep(0.05)
            assert not orphan.exists(), "Startup reconciliation must delete orphan"
        finally:
            sched.stop()

    def test_double_start_is_idempotent(self, tmp_path):
        """Calling start() twice does not spawn a second thread."""
        from code_indexer.server.services.research_cleanup_service import (
            ResearchCleanupScheduler,
        )

        sched = ResearchCleanupScheduler(
            research_base_dir=tmp_path / "research",
            retention_days_provider=lambda: 3,
            live_folder_provider=lambda: set(),
            interval_seconds=3600,
        )
        sched.start()
        first_thread = sched._thread
        sched.start()  # idempotent: must NOT replace the thread
        try:
            assert sched._thread is first_thread, "Second start() must be a no-op"
            assert sched.is_running()
        finally:
            sched.stop()

    def test_scheduler_stop_is_clean(self, tmp_path):
        """stop() joins the daemon thread without raising."""
        from code_indexer.server.services.research_cleanup_service import (
            ResearchCleanupScheduler,
        )

        sched = ResearchCleanupScheduler(
            research_base_dir=tmp_path / "research",
            retention_days_provider=lambda: 3,
            live_folder_provider=lambda: set(),
            interval_seconds=3600,
        )
        sched.start()
        sched.stop()  # must not raise / hang
        assert not sched.is_running()

    def test_live_folder_provider_reads_research_sessions(self, tmp_path):
        """The DB-backed provider returns folder_path strings for live rows."""
        import sqlite3

        from src.code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.services.research_cleanup_service import (
            make_db_live_folder_provider,
        )

        db_path = str(tmp_path / "cidx_server.db")
        DatabaseSchema(db_path=db_path).initialize_database()

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO research_sessions (id, name, folder_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s1", "S1", "/x/research/s1", "now", "now"),
        )
        conn.commit()
        conn.close()

        provider = make_db_live_folder_provider(db_path)
        assert provider() == {"/x/research/s1"}


class TestDefaultDirLogLevel:
    """Bug #1099 — 'default' dir must log at DEBUG, not WARNING, every hourly sweep.

    The well-known ``DEFAULT_SESSION_DIR_NAME`` ('default') is expected and
    preserved on every sweep.  Logging it at WARNING pollutes logs and masks
    genuine unexpected-directory warnings.  Only truly unexpected non-session
    directory names should emit WARNING.
    """

    def test_default_dir_emits_debug_not_warning(self, tmp_path, caplog):
        """Sweeping a research root with a 'default' dir must produce NO WARNING.

        The 'default' dir is preserved (dirs_preserved incremented) and a DEBUG
        message is emitted instead of WARNING.
        """
        import logging

        base = tmp_path / "research"
        default_dir = base / "default"
        default_dir.mkdir(parents=True)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),
        )

        with caplog.at_level(
            logging.DEBUG,
            logger="code_indexer.server.services.research_cleanup_service",
        ):
            result = svc.cleanup()

        # The 'default' dir must be preserved
        assert default_dir.exists(), "'default' dir must never be deleted"
        assert result.dirs_preserved >= 1, (
            "dirs_preserved must be incremented for 'default'"
        )

        # No WARNING must be emitted for the known 'default' directory
        warning_records = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "default" in r.getMessage()
        ]
        assert warning_records == [], (
            f"Expected no WARNING for 'default' dir, got: {[r.getMessage() for r in warning_records]}"
        )

        # A DEBUG record must be emitted for 'default'
        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "default" in r.getMessage()
        ]
        assert debug_records, "Expected a DEBUG log record mentioning 'default'"

    def test_unexpected_dir_emits_warning(self, tmp_path, caplog):
        """A genuinely unexpected non-session dir name still emits WARNING.

        Only the 'default' special case is downgraded to DEBUG; any other
        non-UUID directory name remains at WARNING level so operators notice it.
        """
        import logging

        base = tmp_path / "research"
        unexpected_dir = base / "garbage-not-a-uuid"
        unexpected_dir.mkdir(parents=True)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),
        )

        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.services.research_cleanup_service",
        ):
            result = svc.cleanup()

        # The unexpected dir must be preserved
        assert unexpected_dir.exists(), "Unexpected dir must never be deleted"
        assert result.dirs_preserved >= 1

        # A WARNING must be emitted for the unexpected dir
        warning_records = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "garbage-not-a-uuid" in r.getMessage()
        ]
        assert warning_records, (
            "Expected a WARNING log record for unexpected dir 'garbage-not-a-uuid'"
        )

    def test_uuid_session_dir_reapable_unchanged(self, tmp_path):
        """A valid UUID session dir that is an aged orphan is still deleted (unchanged behavior)."""
        import os
        import time

        base = tmp_path / "research"
        session_id = "aaaaaaaa-1099-4000-8000-000000001099"
        session_dir = base / session_id
        (session_dir / "uploads").mkdir(parents=True)
        (session_dir / "uploads" / "file.txt").write_text("x")

        # Age the entire tree so it passes the TTL + recent-modification guards
        old = time.time() - 10 * 24 * 3600
        os.utime(session_dir / "uploads" / "file.txt", (old, old))
        os.utime(session_dir / "uploads", (old, old))
        os.utime(session_dir, (old, old))

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_folder_provider=lambda: set(),  # orphan — no live row
        )
        result = svc.cleanup()

        assert not session_dir.exists(), "Aged orphan UUID session dir must be deleted"
        assert result.dirs_deleted == 1
        assert result.dirs_scanned == 1
