"""
Unit tests for daemon mode FTS rebuild with progress callbacks.

Tests AC4: Daemon mode FTS rebuild with progress reporting.
"""

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import Mock


class TestDaemonRebuildFTS:
    """Test daemon mode FTS rebuild functionality."""

    def test_daemon_has_rebuild_fts_index_endpoint(self):
        """
        AC4: Verify daemon service has exposed_rebuild_fts_index() RPC endpoint.

        This endpoint already exists but returns "not_implemented".
        We verify it exists and is callable.
        """
        from code_indexer.daemon.service import CIDXDaemonService

        service = CIDXDaemonService()

        # Verify the RPC endpoint exists
        assert hasattr(service, "exposed_rebuild_fts_index"), (
            "Daemon service must have exposed_rebuild_fts_index() method\n"
            "Expected in: src/code_indexer/daemon/service.py\n"
            "Signature: def exposed_rebuild_fts_index(self, project_path, callback=None)"
        )

        # Verify it's callable
        assert callable(service.exposed_rebuild_fts_index), (
            "exposed_rebuild_fts_index must be callable"
        )

    def test_daemon_rebuild_implementation_uses_filefinder(self):
        """
        AC4: Verify daemon rebuild implementation uses FileFinder (not vector JSONs).

        This test will FAIL initially because exposed_rebuild_fts_index()
        returns {"status": "not_implemented"}.

        Expected implementation:
        1. Use FileFinder to discover files
        2. Clear existing FTS index
        3. Index files with progress callbacks
        4. Reload FTS cache
        5. Return success status with stats
        """
        from code_indexer.daemon.service import CIDXDaemonService

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_dir = project_dir / ".code-indexer"
            config_dir.mkdir()

            # Create sample source files
            (project_dir / "main.py").write_text("def main(): pass")
            (project_dir / "utils.py").write_text("def helper(): pass")

            # Create config file
            config_file = config_dir / "config.json"
            config_data = {
                "codebase_dir": str(project_dir),
                "embedding_provider": "voyage-ai",
                "embedding_model": "voyage-code-3",
                "file_extensions": [".py"],
                "exclude_dirs": [".git", "node_modules"],
            }
            config_file.write_text(json.dumps(config_data))

            # Create mock progress file (required)
            progress_file = config_dir / "indexing_progress.json"
            progress_data = {
                "current_session": {
                    "session_id": "test",
                    "operation_type": "full",
                    "embedding_provider": "voyage-ai",
                    "embedding_model": "voyage-code-3",
                    "total_files": 2,
                    "files_completed": 2,
                },
                "file_records": {},
            }
            progress_file.write_text(json.dumps(progress_data))

            # Create service
            service = CIDXDaemonService()

            # Mock progress callback
            progress_callback = Mock()

            # Call rebuild
            result = service.exposed_rebuild_fts_index(
                project_path=str(project_dir), callback=progress_callback
            )

            # This will FAIL initially because current implementation returns:
            # {"status": "not_implemented"}
            assert result.get("status") != "not_implemented", (
                "exposed_rebuild_fts_index() must be implemented!\n"
                f"Current result: {result}\n\n"
                "Expected implementation in src/code_indexer/daemon/service.py:\n"
                "1. Load config and create FileFinder\n"
                "2. Call FileFinder.find_files() to discover files\n"
                "3. Clear existing FTS index (if exists)\n"
                "4. Index files with progress callbacks\n"
                "5. Reload FTS cache with _get_or_create_fts_manager(force_reload=True)\n"
                "6. Return {'status': 'success', 'files_indexed': N, 'files_failed': M}"
            )

            # Verify success
            assert result.get("status") == "success", f"Expected success, got: {result}"
            assert "files_indexed" in result, "Result must include files_indexed count"

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _make_project(tmpdir: str, py_files: list) -> tuple[Path, Any]:
        """Create a minimal project with config + progress file and return (project_dir, service)."""
        from code_indexer.daemon.service import CIDXDaemonService

        project_dir = Path(tmpdir)
        config_dir = project_dir / ".code-indexer"
        config_dir.mkdir(exist_ok=True)

        for name, content in py_files:
            (project_dir / name).write_text(content)

        config_data = {
            "codebase_dir": str(project_dir),
            "embedding_provider": "voyage-ai",
            "embedding_model": "voyage-code-3",
            "file_extensions": [".py"],
            "exclude_dirs": [".git", "node_modules"],
        }
        (config_dir / "config.json").write_text(json.dumps(config_data))

        progress_data = {
            "current_session": {
                "session_id": "test",
                "operation_type": "full",
                "embedding_provider": "voyage-ai",
                "embedding_model": "voyage-code-3",
                "total_files": len(py_files),
                "files_completed": len(py_files),
            },
            "file_records": {},
        }
        (config_dir / "indexing_progress.json").write_text(json.dumps(progress_data))

        return project_dir, CIDXDaemonService()

    # ------------------------------------------------------------------
    # Bug #1218 residual: total-failure guard
    # ------------------------------------------------------------------

    def test_all_files_fail_returns_error_status(self):
        """
        Bug #1218 residual: daemon in-process FTS rebuild with ALL files failing
        must return status != 'success' (total-failure guard).

        RED: current code returns {"status": "success", "files_indexed": 0, "files_failed": N}.
        GREEN: must return {"status": "error"/"failed", ...} with a descriptive message.
        """
        from unittest.mock import patch
        from code_indexer.services.tantivy_index_manager import TantivyIndexManager

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, service = self._make_project(
                tmpdir,
                [("main.py", "def main(): pass"), ("utils.py", "def helper(): pass")],
            )

            with patch.object(
                TantivyIndexManager, "add_document", side_effect=Exception("forced")
            ):
                result = service.exposed_rebuild_fts_index(
                    str(project_dir), callback=None
                )

        assert result.get("status") != "success", (
            f"ALL files failed => must NOT return success, got: {result}\n"
            "Bug #1218 residual: daemon FTS rebuild must fail loudly on total failure."
        )
        assert result.get("status") in ("error", "failed"), (
            f"Expected status 'error' or 'failed', got: {result.get('status')!r}"
        )
        assert result.get("error") or result.get("message"), (
            f"Non-success result must include 'error' or 'message', got: {result}"
        )

    def test_partial_failure_succeeds_with_warning_and_leaves_index_unmarked(
        self,
    ):
        """
        Bug #2056: per-file failures follow the semantic rule -- a rebuild
        that indexed some files succeeds, reports the files missing from FTS
        as a warning, and is not marked content-current, so the next
        `cidx index --fts` rebuilds it (never a partial index marked current).
        """
        from unittest.mock import patch
        from code_indexer.services.fts_file_documents import (
            fts_content_version_is_current,
        )
        from code_indexer.services.tantivy_index_manager import TantivyIndexManager

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, service = self._make_project(
                tmpdir,
                [("main.py", "def main(): pass"), ("utils.py", "def helper(): pass")],
            )

            call_count = {"n": 0}
            original_add = TantivyIndexManager.add_document

            def fail_first_only(self_mgr, doc):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise Exception("forced partial failure")
                return original_add(self_mgr, doc)

            with patch.object(TantivyIndexManager, "add_document", fail_first_only):
                result = service.exposed_rebuild_fts_index(
                    str(project_dir), callback=None
                )
            marked = fts_content_version_is_current(
                project_dir / ".code-indexer" / "tantivy_index"
            )

        assert result.get("status") == "success", result
        assert result.get("files_indexed", 0) >= 1
        assert result.get("files_failed", 0) >= 1
        assert "missing from the FTS index" in result.get("warning", ""), result
        assert not marked, "an incomplete rebuild is never marked content-current"

    def test_rebuilt_documents_store_repo_relative_paths(self):
        """Bug #2056: the daemon rebuild must store repo-relative FTS paths,
        exactly like normal indexing -- path filters and per-file
        supersession (delete-by-path) only ever see relative paths."""
        from code_indexer.services.tantivy_index_manager import TantivyIndexManager

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, service = self._make_project(
                tmpdir, [("main.py", "def main(): return 'MAINTOKEN'")]
            )
            (project_dir / "src").mkdir()
            (project_dir / "src" / "util.py").write_text("UTILTOKEN = 1\n")

            result = service.exposed_rebuild_fts_index(str(project_dir), callback=None)
            assert result.get("status") == "success", result

            fts = TantivyIndexManager(project_dir / ".code-indexer" / "tantivy_index")
            fts.initialize_index(create_new=False)
            try:
                paths = sorted(fts.get_all_indexed_paths())
                filtered = fts.search("UTILTOKEN", path_filters=["src/*"])
            finally:
                fts.close()

        assert paths == ["main.py", "src/util.py"]
        assert [hit["path"] for hit in filtered] == ["src/util.py"]

    def test_rebuild_writes_chunk_level_documents(self):
        """Bug #2056: the daemon rebuild writes exactly normal indexing's
        documents -- one per chunk -- so a file with matches in two chunks
        returns both, never just its first match."""
        from code_indexer.config import ConfigManager
        from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
        from code_indexer.services.tantivy_index_manager import TantivyIndexManager

        filler = "".join(f"value_{i:05d} = {i}\n" for i in range(700))
        content = f"FIRST = 1  # TWICETOKEN\n{filler}LAST = 2  # TWICETOKEN\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, service = self._make_project(tmpdir, [("big.py", content)])
            config = ConfigManager.load_verified_config(project_dir)
            chunks = FixedSizeChunker(config).chunk_file(
                project_dir / "big.py", repo_root=Path(config.codebase_dir)
            )
            assert len(chunks) > 1

            result = service.exposed_rebuild_fts_index(str(project_dir), callback=None)
            assert result.get("status") == "success", result

            fts = TantivyIndexManager(project_dir / ".code-indexer" / "tantivy_index")
            fts.initialize_index(create_new=False)
            try:
                documents = fts.get_document_count()
                hits = fts.search("TWICETOKEN", limit=10)
            finally:
                fts.close()

        assert documents == len(chunks)
        lines = sorted(hit["line"] for hit in hits)
        assert [hit["path"] for hit in hits] == ["big.py", "big.py"]
        assert lines[0] != lines[1]

    def test_rebuild_marks_fts_content_current(self):
        """Bug #2056: a rebuilt index holds every file's current content, so
        it is marked content-current and the next `cidx index --fts` does
        not rebuild it again."""
        from code_indexer.services.fts_file_documents import (
            fts_content_version_is_current,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, service = self._make_project(
                tmpdir, [("main.py", "def main(): pass")]
            )
            result = service.exposed_rebuild_fts_index(str(project_dir), callback=None)
            index_dir = project_dir / ".code-indexer" / "tantivy_index"

            assert result.get("status") == "success", result
            assert fts_content_version_is_current(index_dir)

    def test_normal_success_unchanged(self):
        """Regression guard: all files succeed => status must still be 'success'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir, service = self._make_project(
                tmpdir, [("main.py", "def main(): pass")]
            )
            result = service.exposed_rebuild_fts_index(str(project_dir), callback=None)

        assert result.get("status") == "success", (
            f"Normal success must return 'success', got: {result}"
        )
        assert result.get("files_indexed", 0) >= 1
        assert result.get("files_failed", 0) == 0


class TestRebuildFtsViaDaemonWarning2056:
    """The CLI side of a daemon `--rebuild-fts-index` shows the daemon's
    missing-files warning (Bug #2056 per-file rule). The real daemon service
    runs in-process; only the socket transport is replaced."""

    def test_missing_files_warning_is_printed(self, monkeypatch) -> None:
        import os
        from types import SimpleNamespace

        from rich.console import Console

        from code_indexer import cli_daemon_delegation
        from code_indexer.config import ConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            # _make_project is a @staticmethod of TestDaemonRebuildFTS.
            project_dir, service = TestDaemonRebuildFTS._make_project(
                tmpdir,
                [("main.py", "def main(): pass"), ("locked.py", "def x(): pass")],
            )
            config_manager = ConfigManager(
                project_dir / ".code-indexer" / "config.json"
            )
            # In-process connection to the REAL service: `.root` is the
            # service itself, exactly what RPyC exposes over the socket.
            connection = SimpleNamespace(root=service, close=lambda: None)
            monkeypatch.setattr(cli_daemon_delegation, "_start_daemon", lambda p: None)
            monkeypatch.setattr(
                cli_daemon_delegation, "_connect_to_daemon", lambda s, c: connection
            )
            monkeypatch.chdir(project_dir)
            console = Console(record=True, width=400)
            locked = project_dir / "locked.py"
            os.chmod(locked, 0)
            try:
                code = cli_daemon_delegation.rebuild_fts_via_daemon(
                    config_manager, console
                )
            finally:
                os.chmod(locked, 0o644)

        output = console.export_text()
        assert code == 0, output
        assert "1 file(s) missing from the FTS index" in output, output
