"""
Bug #1485 follow-up (code-review BLOCKING finding) -- Research Assistant
workspace GC (``ResearchCleanupService``) matched live sessions by the STORED
``folder_path`` string instead of by session id.

Root cause: Bug #1485's core fix (``research_assistant_service.py``) made
every filesystem operation recompute the node-local session folder from
``research_base_dir / session_id`` instead of trusting the stored
``folder_path`` column -- but it deliberately did NOT self-heal that stored
column (self-healing an absolute local path into shared cluster state would
re-introduce the exact anti-pattern this bug is about). The cleanup GC
(``ResearchCleanupService.cleanup()``) was NOT part of that fix and still
built its "live set" from the stored ``folder_path`` values
(``make_backend_live_folder_provider`` / ``make_db_live_folder_provider``)
and preserved an on-disk dir only if ``str(on_disk_path) in live_folders``.

On a foreign-seeded / topology-changed cluster, the stored ``folder_path``
(e.g. ``/home/jsbattig/...``) never equals the on-disk node-local dir (e.g.
``/opt/code-indexer/.cidx-server/research/<id>``) -- so a LIVE, non-expired
UUID session's workspace would be treated as an orphan and ``rmtree``'d once
it aged past ``research_session_retention_days`` (default 7). ``default`` is
name-protected (``_is_session_dir_name``), which is why the original Bug
#1485 repro never exposed this.

Fix: match live sessions by SESSION ID (the on-disk directory's ``.name``,
which IS the session id by construction -- ``create_session()`` always
creates the folder at ``research_base_dir / session_id``) against the set of
live session ids from the DB -- NEVER by the stored, possibly-foreign,
absolute path string. ``make_db_live_session_id_provider`` /
``make_backend_live_session_id_provider`` and
``ResearchCleanupService(live_session_id_provider=...)`` replace the old
path-based provider/param.

These tests use real SQLite + real filesystem (no mocking of the SUT's own
logic), mirroring ``test_research_assistant_stale_folder_path_1485.py``.

Following TDD: these tests fail (the live session's workspace gets deleted)
until the session-id-based matching fix lands.
"""

import os
import sqlite3
import time
from pathlib import Path

from src.code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.services.research_cleanup_service import (
    ResearchCleanupService,
    make_db_live_session_id_provider,
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


class TestLiveSessionPreservedBySessionIdNotStoredPath:
    def test_live_uuid_session_with_foreign_folder_path_is_preserved(self, tmp_path):
        """
        A LIVE, aged (past retention + past the recent-modification window)
        UUID session whose stored ``folder_path`` is a FOREIGN absolute path
        (never matching the on-disk node-local directory) must have its
        on-disk workspace PRESERVED -- because the GC matches by session id,
        not by the stale stored path string.
        """
        base = tmp_path / "research"
        session_id = "a1b2c3d4-1485-4000-8000-000000001485"
        # Aged past retention (3 days) AND past the default 24h
        # recent-modification guard, so ONLY the live-session-id match can
        # save it.
        live = _make_session_dir(base, session_id, age_days=10)

        db_path = str(tmp_path / "cidx_server.db")
        DatabaseSchema(db_path=db_path).initialize_database()

        # Simulates a row written by a PRIOR deployment/node with a
        # different service-account home (Bug #1485's root cause) -- the
        # stored folder_path NEVER equals str(live).
        foreign_folder_path = str(
            tmp_path / "foreign_home" / ".cidx-server" / "research" / session_id
        )

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO research_sessions "
                "(id, name, folder_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, "Live Session", foreign_folder_path, "now", "now"),
            )
            conn.commit()
        finally:
            conn.close()

        provider = make_db_live_session_id_provider(db_path)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_session_id_provider=provider,
        )
        result = svc.cleanup()

        assert live.exists(), (
            "A LIVE session's on-disk workspace must be preserved by "
            "SESSION ID even though its stored folder_path is a foreign, "
            "stale absolute path that never matches the on-disk node-local "
            "directory"
        )
        assert result.dirs_deleted == 0
        assert result.dirs_preserved == 1

    def test_genuinely_orphaned_session_dir_still_reaped(self, tmp_path):
        """
        A session dir with NO corresponding DB row at all (a genuine orphan)
        is still reaped under session-id-based matching -- the fix narrows
        HOW liveness is proven, it does not weaken the orphan-reaping
        behavior itself.
        """
        base = tmp_path / "research"
        orphan = _make_session_dir(
            base, "a1b2c3d4-1485-4000-8000-000000009999", age_days=10
        )

        db_path = str(tmp_path / "cidx_server.db")
        DatabaseSchema(db_path=db_path).initialize_database()
        # No rows inserted -- the sessions table is empty.

        provider = make_db_live_session_id_provider(db_path)

        svc = ResearchCleanupService(
            research_base_dir=base,
            retention_days=3,
            live_session_id_provider=provider,
        )
        result = svc.cleanup()

        assert not orphan.exists(), (
            "A genuinely orphaned (no DB row) aged session dir must still "
            "be reaped under session-id-based matching"
        )
        assert result.dirs_deleted == 1
