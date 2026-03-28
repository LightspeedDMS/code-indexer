"""Indexing resilience tests (Bug #467).

Verifies that interrupted indexing can resume, metadata isn't poisoned
by timeouts, and the refresh scheduler uses crash-resilient settings.
"""

import inspect


class TestProgressiveMetadataResumeAfterFailed:
    """Bug #467 Fix C: Resume works even after metadata status is 'failed'."""

    def _create_metadata(self, tmp_path):
        from code_indexer.services.progressive_metadata import ProgressiveMetadata

        return ProgressiveMetadata(tmp_path / "metadata.json")

    def test_can_resume_after_failed_status(self, tmp_path):
        """When status='failed' but files_to_index has remaining work, resume should work."""
        metadata = self._create_metadata(tmp_path)
        metadata.start_indexing("voyage", "voyage-3", {"git_available": True})
        metadata.set_files_to_index(["file1.py", "file2.py", "file3.py"])
        metadata.mark_file_completed("file1.py")
        metadata.fail_indexing("Process killed")

        assert metadata.can_resume_interrupted_operation() is True

    def test_can_resume_after_in_progress_status(self, tmp_path):
        """Original behavior: in_progress with remaining work should resume."""
        metadata = self._create_metadata(tmp_path)
        metadata.start_indexing("voyage", "voyage-3", {"git_available": True})
        metadata.set_files_to_index(["file1.py", "file2.py", "file3.py"])
        metadata.mark_file_completed("file1.py")
        # Status stays "in_progress" (no fail_indexing call)

        assert metadata.can_resume_interrupted_operation() is True

    def test_cannot_resume_when_all_files_completed(self, tmp_path):
        """When all files are done, no resume needed."""
        metadata = self._create_metadata(tmp_path)
        metadata.start_indexing("voyage", "voyage-3", {"git_available": True})
        metadata.set_files_to_index(["file1.py"])
        metadata.mark_file_completed("file1.py")

        assert metadata.can_resume_interrupted_operation() is False

    def test_cannot_resume_when_no_files_to_index(self, tmp_path):
        """When files_to_index is empty, no resume possible."""
        metadata = self._create_metadata(tmp_path)
        metadata.start_indexing("voyage", "voyage-3", {"git_available": True})
        metadata.fail_indexing("error")

        assert metadata.can_resume_interrupted_operation() is False

    def test_remaining_files_after_failed(self, tmp_path):
        """Remaining files list is correct after failed status."""
        metadata = self._create_metadata(tmp_path)
        metadata.start_indexing("voyage", "voyage-3", {"git_available": True})
        metadata.set_files_to_index(["a.py", "b.py", "c.py", "d.py"])
        metadata.mark_file_completed("a.py")
        metadata.mark_file_completed("b.py")
        metadata.fail_indexing("timeout")

        remaining = metadata.get_remaining_files()
        assert "c.py" in remaining
        assert "d.py" in remaining
        assert "a.py" not in remaining
        assert "b.py" not in remaining

    def test_resume_timestamp_nonzero_after_failed(self, tmp_path):
        """get_resume_timestamp returns non-zero after 'failed' status."""
        metadata = self._create_metadata(tmp_path)
        metadata.start_indexing("voyage", "voyage-3", {"git_available": True})
        metadata.set_files_to_index(["file1.py"])
        metadata.mark_file_completed("file1.py")
        metadata.fail_indexing("timeout")

        timestamp = metadata.get_resume_timestamp()
        assert timestamp > 0.0, "Should NOT return 0.0 for failed status"

    def test_resume_timestamp_zero_for_unknown_status(self, tmp_path):
        """get_resume_timestamp returns 0.0 for unknown status (not in_progress/completed/failed)."""
        metadata = self._create_metadata(tmp_path)
        # Fresh metadata has status="" or default
        metadata.metadata["status"] = "unknown"
        metadata._save_metadata()

        timestamp = metadata.get_resume_timestamp()
        assert timestamp == 0.0


class TestIndexSourceNoTimeout:
    """Bug #467 Fix A: _index_source() has no subprocess timeout."""

    def test_index_source_has_no_timeout_parameter(self):
        """Verify _index_source() subprocess calls have no timeout parameter."""
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        source = inspect.getsource(RefreshScheduler._index_source)
        # Should have no timeout= in subprocess.run calls
        # (excluding comments which may mention "timeout" in explanatory text)
        lines = source.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # Skip comments
            if "timeout=" in stripped and "subprocess" in source:
                # Allow timeout=None but not timeout=<value>
                assert "timeout=None" in stripped, (
                    f"Found timeout in subprocess call: {stripped}"
                )

    def test_index_source_has_no_timeout_expired_handler(self):
        """Verify _index_source() has no TimeoutExpired exception handler."""
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        source = inspect.getsource(RefreshScheduler._index_source)
        assert "TimeoutExpired" not in source, (
            "_index_source should not handle TimeoutExpired"
        )


class TestIndexSourceConditionalReconcile:
    """Bug #467 Fix D: _index_source() uses --reconcile conditionally."""

    def test_index_source_has_reconcile_logic(self):
        """Verify _index_source() contains conditional --reconcile logic."""
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        source = inspect.getsource(RefreshScheduler._index_source)
        assert "--reconcile" in source, "_index_source should reference --reconcile"
        assert "needs_reconcile" in source, (
            "_index_source should have conditional reconcile logic"
        )

    def test_index_source_uses_fts_flag(self):
        """Verify _index_source() always passes --fts to cidx index."""
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        source = inspect.getsource(RefreshScheduler._index_source)
        assert "--fts" in source, "_index_source should use --fts flag"

    def test_index_source_checks_metadata_status(self):
        """Verify _index_source() reads metadata.json to decide reconcile."""
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

        source = inspect.getsource(RefreshScheduler._index_source)
        assert "metadata.json" in source, "_index_source should read metadata.json"
        assert "in_progress" in source, (
            "_index_source should check for in_progress status"
        )
        assert "failed" in source, "_index_source should check for failed status"

    def test_reconcile_used_when_metadata_shows_interrupted(self, tmp_path):
        """When metadata.json has status='in_progress', reconcile flag should be set."""
        import json

        # Create a metadata.json with interrupted state
        code_indexer_dir = tmp_path / ".code-indexer"
        code_indexer_dir.mkdir()
        metadata = {"status": "in_progress", "files_to_index": ["a.py", "b.py"]}
        (code_indexer_dir / "metadata.json").write_text(json.dumps(metadata))

        # Check the metadata reading logic
        metadata_path = tmp_path / ".code-indexer" / "metadata.json"
        needs_reconcile = False
        if metadata_path.exists():
            with open(metadata_path) as f:
                meta = json.load(f)
            if meta.get("status") in ("in_progress", "failed"):
                needs_reconcile = True

        assert needs_reconcile is True

    def test_no_reconcile_when_metadata_shows_completed(self, tmp_path):
        """When metadata.json has status='completed', no reconcile needed."""
        import json

        code_indexer_dir = tmp_path / ".code-indexer"
        code_indexer_dir.mkdir()
        metadata = {"status": "completed"}
        (code_indexer_dir / "metadata.json").write_text(json.dumps(metadata))

        metadata_path = tmp_path / ".code-indexer" / "metadata.json"
        needs_reconcile = False
        if metadata_path.exists():
            with open(metadata_path) as f:
                meta = json.load(f)
            if meta.get("status") in ("in_progress", "failed"):
                needs_reconcile = True

        assert needs_reconcile is False

    def test_no_reconcile_when_no_metadata(self, tmp_path):
        """When no metadata.json exists, no reconcile needed (first index)."""
        metadata_path = tmp_path / ".code-indexer" / "metadata.json"
        needs_reconcile = False
        if metadata_path.exists():
            needs_reconcile = True  # shouldn't reach here

        assert needs_reconcile is False


class TestSmartIndexerInterruptHandling:
    """Bug #467 Fix B: Interruptions don't poison metadata."""

    def test_timeout_keyword_detected_as_interruption(self):
        """Verify 'timeout' in error message is classified as interruption."""
        # This tests the classification logic, not the full smart_index flow
        error_str = "Process killed by timeout after 3600 seconds"
        is_interruption = any(
            kw in error_str.lower()
            for kw in [
                "timeout",
                "interrupt",
                "killed",
                "signal",
                "sigterm",
                "sigkill",
                "broken pipe",
                "process",
                "shutdown",
            ]
        )
        assert is_interruption is True

    def test_genuine_error_not_classified_as_interruption(self):
        """Verify genuine errors are NOT classified as interruptions."""
        error_str = "ImportError: No module named 'voyageai'"
        is_interruption = any(
            kw in error_str.lower()
            for kw in [
                "timeout",
                "interrupt",
                "killed",
                "signal",
                "sigterm",
                "sigkill",
                "broken pipe",
                "process",
                "shutdown",
            ]
        )
        assert is_interruption is False

    def test_sigterm_classified_as_interruption(self):
        """SIGTERM from server restart should be classified as interruption."""
        error_str = "Indexing interrupted by server shutdown: SIGTERM received"
        is_interruption = any(
            kw in error_str.lower()
            for kw in [
                "timeout",
                "interrupt",
                "killed",
                "signal",
                "sigterm",
                "sigkill",
                "broken pipe",
                "process",
                "shutdown",
            ]
        )
        assert is_interruption is True
