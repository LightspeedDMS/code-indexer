"""
Unit tests for Bug #679 AC7: read_provider_results helper function.

Covers:
- test_read_returns_none_when_file_absent
- test_read_returns_none_when_file_stale
- test_read_returns_dict_when_file_fresh
- test_read_handles_malformed_json
- test_read_handles_io_error
"""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from src.code_indexer.services.provider_health_bridge import read_provider_results

_VALID_PROVIDER_RESULTS = {
    "provider_results": {
        "voyage-ai": {
            "status": "success",
            "error": None,
            "latency_seconds": 142.3,
            "files_indexed": 4821,
            "chunks_indexed": 12451,
        },
        "cohere": {
            "status": "failed",
            "error": "TimeoutError after 30s",
            "latency_seconds": 30.1,
            "files_indexed": 0,
            "chunks_indexed": 0,
        },
    }
}


class TestReadProviderResults:
    """Tests for the read_provider_results helper (AC7)."""

    def setup_method(self):
        """Create a temporary repo directory with .code-indexer subdir."""
        self.temp_dir = tempfile.mkdtemp()
        self.ci_dir = Path(self.temp_dir) / ".code-indexer"
        self.ci_dir.mkdir()
        self.results_file = self.ci_dir / "provider_results.json"

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_results(self, data: dict) -> float:
        """Write provider_results.json and return its mtime."""
        self.results_file.write_text(json.dumps(data))
        return self.results_file.stat().st_mtime

    def test_read_returns_none_when_file_absent(self):
        """When provider_results.json does not exist, returns None."""
        assert not self.results_file.exists()
        # start_mtime in the past so any file would be "fresh"
        result = read_provider_results(self.temp_dir, start_mtime=time.time() - 10)
        assert result is None, f"Expected None when file absent, got {result!r}"

    def test_read_returns_none_when_file_stale(self):
        """When provider_results.json mtime < start_mtime, returns None (stale guard AC5)."""
        mtime = self._write_results(_VALID_PROVIDER_RESULTS)
        # Use a start_mtime AFTER the file was written → file is stale
        future_start = mtime + 10.0
        result = read_provider_results(self.temp_dir, start_mtime=future_start)
        assert result is None, (
            f"Expected None for stale file (file mtime {mtime} < start {future_start}), "
            f"got {result!r}"
        )

    def test_read_returns_dict_when_file_fresh(self):
        """When provider_results.json mtime >= start_mtime, returns parsed dict."""
        # Write file, then use a start_mtime BEFORE the file was written
        past_start = time.time() - 60.0
        self._write_results(_VALID_PROVIDER_RESULTS)
        result = read_provider_results(self.temp_dir, start_mtime=past_start)
        assert result is not None, "Expected dict for fresh file, got None"
        assert result == _VALID_PROVIDER_RESULTS, (
            f"Expected {_VALID_PROVIDER_RESULTS!r}, got {result!r}"
        )

    def test_read_handles_malformed_json(self):
        """When provider_results.json contains invalid JSON, returns None (no exception)."""
        past_start = time.time() - 60.0
        self.results_file.write_text("{ not valid json !!!")
        result = read_provider_results(self.temp_dir, start_mtime=past_start)
        assert result is None, f"Expected None for malformed JSON, got {result!r}"

    def test_read_handles_io_error(self):
        """When reading provider_results.json raises IOError, returns None (no exception)."""
        past_start = time.time() - 60.0
        # Create file so mtime check passes, then mock open to raise IOError
        self._write_results(_VALID_PROVIDER_RESULTS)
        with patch("builtins.open", side_effect=IOError("disk error")):
            result = read_provider_results(self.temp_dir, start_mtime=past_start)
        assert result is None, f"Expected None when IOError raised, got {result!r}"

    def test_read_returns_none_when_ci_dir_missing(self):
        """When .code-indexer directory doesn't exist, returns None gracefully."""
        import shutil

        shutil.rmtree(self.ci_dir)
        result = read_provider_results(self.temp_dir, start_mtime=time.time() - 10)
        assert result is None

    def test_read_exact_mtime_boundary(self):
        """File with mtime exactly equal to start_mtime is considered fresh (>=)."""
        mtime = self._write_results(_VALID_PROVIDER_RESULTS)
        # start_mtime exactly equal to file mtime → should be fresh
        result = read_provider_results(self.temp_dir, start_mtime=mtime)
        assert result is not None, (
            "Expected dict when file mtime == start_mtime (>= boundary), got None"
        )
