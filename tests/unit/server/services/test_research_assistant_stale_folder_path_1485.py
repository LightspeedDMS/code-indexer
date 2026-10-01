"""
Bug #1485 -- Research Assistant broken on cluster: session folder_path
persisted as an absolute local path in shared storage (stale prior-home
paths -> PermissionError on mkdir).

Root cause: ``get_default_session()`` (and, historically,
``_ensure_session_folder_setup()``'s callers) trusted the stored
``folder_path`` value read back from the session backend/DB for a real
filesystem operation (``mkdir``). In cluster mode that value is shared
PostgreSQL state that may have been written by a PRIOR deployment whose
service-account home differed from the CURRENT node's -- so the stored path
can point at a directory this node has no business creating or writing.

These tests reproduce the defect for BOTH storage code paths
``ResearchAssistantService`` supports:

* SQLite-direct (``self._backend is None``) -- the solo/CLI-style path.
* Backend-based (``self._backend is not None``) -- the cluster-mode path
  (PostgreSQL in production; a REAL ``ResearchSessionsSqliteBackend``
  instance is used here, satisfying the same
  ``ResearchSessionsBackend`` Protocol, per the project's anti-mock
  testing hierarchy -- no mocking of the SUT's own logic).

In both cases, a pre-existing 'default' session row is seeded with a
FOREIGN ``folder_path`` (a path outside the current node's
``research_base_dir``, simulating a value written by a different
deployment/home). The fix must make ``get_default_session()`` ALWAYS
recompute and use the node-local ``research_base_dir / <session_id>`` path
for the actual filesystem operation -- the stored value is advisory/display
only.

Following TDD: these tests fail (the foreign path gets created and/or the
node-local path is never created) until the recompute fix lands.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.services.research_assistant_service import (
    ResearchAssistantService,
)


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary research database with the full schema."""
    db_path = str(tmp_path / "test.db")
    schema = DatabaseSchema(db_path=db_path)
    schema.initialize_database()
    return db_path


class TestGetDefaultSessionRecomputesNodeLocalPathSqliteDirect:
    """SQLite-direct storage path (``self._backend is None``)."""

    def test_stale_default_row_folder_created_under_node_local_base_dir(
        self, temp_db, tmp_path
    ):
        """
        A pre-existing 'default' row whose stored folder_path points at a
        FOREIGN path (outside the current node's research_base_dir, as
        would happen after a service-account home/topology change) must
        result in the session folder being created under THIS node's
        research_base_dir -- the stale foreign path must never be touched.
        """
        import sqlite3

        node_local_base = tmp_path / "node_local_root"
        foreign_folder_path = str(
            tmp_path / "foreign_home" / ".cidx-server" / "research" / "default"
        )

        # Simulate a row written by a PRIOR deployment with a different home.
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "INSERT INTO research_sessions "
                "(id, name, folder_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("default", "Default Session", foreign_folder_path, now, now),
            )
            conn.commit()
        finally:
            conn.close()

        service = ResearchAssistantService(
            db_path=temp_db, research_base_dir=node_local_base
        )

        # Must not raise, even though the stored path is foreign.
        session = service.get_default_session()

        assert session["id"] == "default"

        node_local_folder = node_local_base / "default"
        assert node_local_folder.exists(), (
            f"Session folder must be created under the node-local base dir "
            f"{node_local_folder}, never trusting the stale stored folder_path"
        )
        assert not Path(foreign_folder_path).exists(), (
            f"The stale foreign folder_path {foreign_folder_path} must NEVER "
            f"be touched by a filesystem operation"
        )


class TestGetDefaultSessionRecomputesNodeLocalPathBackend:
    """
    Backend-based storage path (``self._backend is not None``) -- the
    cluster-mode code path. Production uses PostgreSQL; this test uses a
    real ``ResearchSessionsSqliteBackend`` instance, which satisfies the
    identical ``ResearchSessionsBackend`` Protocol the PostgreSQL backend
    implements, so the exercised ``ResearchAssistantService`` code path
    (``self._backend is not None``) is identical to cluster mode.
    """

    def test_stale_default_row_folder_created_under_node_local_base_dir(self, tmp_path):
        from code_indexer.server.storage.sqlite_backends import (
            ResearchSessionsSqliteBackend,
        )

        backend_db_path = str(tmp_path / "backend.db")
        backend = ResearchSessionsSqliteBackend(backend_db_path)

        node_local_base = tmp_path / "node_local_root"
        foreign_folder_path = str(
            tmp_path / "foreign_home" / ".cidx-server" / "research" / "default"
        )

        try:
            # Simulate a row written by a PRIOR deployment/node with a
            # different home, directly through the SAME backend contract
            # the production code uses.
            backend.create_session(
                session_id="default",
                name="Default Session",
                folder_path=foreign_folder_path,
            )

            service = ResearchAssistantService(
                storage_backend=backend,
                research_base_dir=node_local_base,
            )

            # Must not raise, even though the stored path is foreign.
            session = service.get_default_session()

            assert session["id"] == "default"

            node_local_folder = node_local_base / "default"
            assert node_local_folder.exists(), (
                f"Session folder must be created under the node-local base "
                f"dir {node_local_folder}, never trusting the stale stored "
                f"folder_path"
            )
            assert not Path(foreign_folder_path).exists(), (
                f"The stale foreign folder_path {foreign_folder_path} must "
                f"NEVER be touched by a filesystem operation"
            )
        finally:
            backend.close()


class TestRunClaudeBackgroundUsesNodeLocalWorkingDir:
    """
    Bug #1485: the Claude CLI ``cwd`` used by ``_run_claude_background`` must
    also be recomputed from the node-local ``research_base_dir``, never
    trusted from the stored (possibly stale/foreign) ``folder_path``.
    Otherwise the folder-creation fix alone is a half-fix: the HTTP request
    would stop 500'ing, but the background job would still try to run
    Claude CLI with a working directory that was never created (or that this
    node cannot use), silently failing the chat instead.
    """

    def test_working_dir_passed_to_subprocess_is_node_local_not_stale(
        self, temp_db, tmp_path
    ):
        from unittest.mock import MagicMock, patch

        node_local_base = tmp_path / "node_local_root"
        foreign_folder_path = str(
            tmp_path / "foreign_home" / ".cidx-server" / "research" / "default"
        )

        now = datetime.now(timezone.utc).isoformat()
        import sqlite3

        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "INSERT INTO research_sessions "
                "(id, name, folder_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("default", "Default Session", foreign_folder_path, now, now),
            )
            conn.commit()
        finally:
            conn.close()

        service = ResearchAssistantService(
            db_path=temp_db, research_base_dir=node_local_base
        )
        # Establishes the node-local folder on disk (Bug #1485 fix already
        # covered by the tests above) -- here we care about what cwd
        # _run_claude_background actually uses when invoking Claude CLI.
        service.get_default_session()

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "ok"
        fake_result.stderr = ""

        with patch(
            "code_indexer.server.services.research_assistant_service.subprocess.run",
            return_value=fake_result,
        ) as mock_run:
            service._run_claude_background(
                job_id="job-1485",
                session_id="default",
                claude_prompt="hello",
                is_first_prompt=True,
            )

        assert mock_run.called, "subprocess.run must have been invoked"
        used_cwd = mock_run.call_args.kwargs["cwd"]

        expected_node_local = str(node_local_base / "default")
        assert used_cwd == expected_node_local, (
            f"Claude CLI cwd must be the node-local folder {expected_node_local}, "
            f"not the stale stored folder_path; got {used_cwd}"
        )
        assert used_cwd != foreign_folder_path, (
            "Claude CLI cwd must never be the stale foreign folder_path"
        )


class TestDeleteSessionCleansUpNodeLocalFolder:
    """
    Bug #1485: ``delete_session`` must remove the ACTUAL node-local session
    folder, not silently no-op because the stored ``folder_path`` is a stale
    foreign path that never existed on this node in the first place.
    """

    def test_delete_session_removes_node_local_folder_not_stale_path(
        self, temp_db, tmp_path
    ):
        import sqlite3

        node_local_base = tmp_path / "node_local_root"
        foreign_folder_path = str(
            tmp_path / "foreign_home" / ".cidx-server" / "research" / "default"
        )

        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "INSERT INTO research_sessions "
                "(id, name, folder_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("default", "Default Session", foreign_folder_path, now, now),
            )
            conn.commit()
        finally:
            conn.close()

        service = ResearchAssistantService(
            db_path=temp_db, research_base_dir=node_local_base
        )

        # Materializes the real node-local folder on disk.
        service.get_default_session()
        node_local_folder = node_local_base / "default"
        assert node_local_folder.exists(), "Precondition: node-local folder must exist"

        deleted = service.delete_session("default")

        assert deleted is True, "delete_session must report success"
        assert not node_local_folder.exists(), (
            f"delete_session must remove the ACTUAL node-local folder "
            f"{node_local_folder}, not silently no-op against the stale "
            f"stored folder_path"
        )


class _FakeUploadFile:
    """Minimal stand-in for FastAPI's UploadFile -- provides only the
    ``filename``/``file.read()`` surface ``upload_file()`` actually uses."""

    def __init__(self, filename: str, content: bytes) -> None:
        import io

        self.filename = filename
        self.file = io.BytesIO(content)


class TestUploadFileUsesNodeLocalUploadsDir:
    """
    Bug #1485: the uploads folder (``upload_file``, ``list_files``,
    ``get_session_upload_size``, ``delete_file``, ``get_file_path``) must
    also be recomputed from the node-local ``research_base_dir``, never
    trusted from the stored (possibly stale/foreign) ``folder_path``.
    """

    def test_upload_file_and_list_files_use_node_local_folder_not_stale(
        self, temp_db, tmp_path
    ):
        import sqlite3

        node_local_base = tmp_path / "node_local_root"
        foreign_folder_path = str(
            tmp_path / "foreign_home" / ".cidx-server" / "research" / "default"
        )

        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(temp_db)
        try:
            conn.execute(
                "INSERT INTO research_sessions "
                "(id, name, folder_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("default", "Default Session", foreign_folder_path, now, now),
            )
            conn.commit()
        finally:
            conn.close()

        service = ResearchAssistantService(
            db_path=temp_db, research_base_dir=node_local_base
        )
        # Materializes the real node-local folder on disk.
        service.get_default_session()

        fake_file = _FakeUploadFile("notes.txt", b"hello bug 1485")
        result = service.upload_file("default", fake_file)

        assert result["success"] is True, f"Upload must succeed: {result}"

        node_local_uploads = node_local_base / "default" / "uploads"
        assert (node_local_uploads / "notes.txt").exists(), (
            f"Uploaded file must land under the node-local uploads dir "
            f"{node_local_uploads}, never the stale stored folder_path"
        )

        foreign_uploads = Path(foreign_folder_path) / "uploads"
        assert not foreign_uploads.exists(), (
            f"The stale foreign uploads dir {foreign_uploads} must NEVER "
            f"be touched by a filesystem operation"
        )

        files = service.list_files("default")
        assert any(f["filename"] == "notes.txt" for f in files), (
            "list_files must find the file uploaded under the node-local dir"
        )

        size = service.get_session_upload_size("default")
        assert size == len(b"hello bug 1485")
