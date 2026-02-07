"""
Integration tests for CLI health command.

Tests the health command with real HNSW indexes to verify end-to-end functionality.

Story #57: CLI cidx health Command
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

import hnswlib
import numpy as np
import pytest
from click.testing import CliRunner

from code_indexer.cli import cli


@pytest.fixture
def temp_project_dir():
    """Create a temporary project directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def healthy_index(temp_project_dir):
    """Create a healthy HNSW index for testing."""
    # Create .code-indexer/index directory
    index_dir = temp_project_dir / ".code-indexer" / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = index_dir / "hnsw.bin"

    # Create a small HNSW index with valid structure
    dim = 64
    num_elements = 100
    ef_construction = 200
    M = 16

    # Initialize index
    index = hnswlib.Index(space='l2', dim=dim)
    index.init_index(max_elements=num_elements, ef_construction=ef_construction, M=M)

    # Add some vectors
    data = np.random.random((num_elements, dim)).astype('float32')
    ids = np.arange(num_elements)
    index.add_items(data, ids)

    # Save index
    index.save_index(str(index_path))

    return index_path


@pytest.fixture
def corrupted_index(temp_project_dir):
    """Create a corrupted HNSW index for testing."""
    # Create .code-indexer/index directory
    index_dir = temp_project_dir / ".code-indexer" / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = index_dir / "hnsw.bin"

    # Write corrupted data (not a valid HNSW index)
    with open(index_path, 'wb') as f:
        f.write(b"CORRUPTED DATA NOT A VALID HNSW INDEX")

    return index_path


class TestHealthCommandIntegrationHealthyIndex:
    """Integration tests with healthy HNSW index."""

    def test_healthy_index_default_output(self, healthy_index, temp_project_dir):
        """Test health check with healthy index - default output."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["health", "--index-path", str(healthy_index)],
            catch_exceptions=False
        )

        assert result.exit_code == 0
        assert "HEALTHY" in result.output
        assert "Element Count:" in result.output
        assert "Connections Checked:" in result.output
        assert "Check Duration:" in result.output
        assert "ms" in result.output

    def test_healthy_index_json_output(self, healthy_index, temp_project_dir):
        """Test health check with healthy index - JSON output."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["health", "--index-path", str(healthy_index), "--json"],
            catch_exceptions=False
        )

        assert result.exit_code == 0

        # Parse JSON output
        data = json.loads(result.output)

        # Verify structure
        assert data["valid"] is True
        assert data["file_exists"] is True
        assert data["readable"] is True
        assert data["loadable"] is True
        assert data["element_count"] == 100
        assert data["connections_checked"] > 0
        assert "index_path" in data
        assert "check_duration_ms" in data

    def test_healthy_index_quiet_output(self, healthy_index, temp_project_dir):
        """Test health check with healthy index - quiet output."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["health", "--index-path", str(healthy_index), "--quiet"],
            catch_exceptions=False
        )

        assert result.exit_code == 0
        assert result.output.strip() == "HEALTHY"


class TestHealthCommandIntegrationNoIndex:
    """Integration tests when no index exists."""

    def test_no_index_default_output(self, temp_project_dir):
        """Test health check when no index exists - default output."""
        # Point to non-existent index
        index_path = temp_project_dir / ".code-indexer" / "index" / "hnsw.bin"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["health", "--index-path", str(index_path)],
            catch_exceptions=False
        )

        assert result.exit_code == 2
        assert "NOT FOUND" in result.output or "Index file not found" in result.output
        assert "cidx index" in result.output or "code-indexer index" in result.output

    def test_no_index_json_output(self, temp_project_dir):
        """Test health check when no index exists - JSON output."""
        # Point to non-existent index
        index_path = temp_project_dir / ".code-indexer" / "index" / "hnsw.bin"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["health", "--index-path", str(index_path), "--json"],
            catch_exceptions=False
        )

        assert result.exit_code == 2

        # Parse JSON output
        data = json.loads(result.output)

        assert data["valid"] is False
        assert data["file_exists"] is False
        assert data["readable"] is False
        assert data["loadable"] is False
        assert "Index file not found" in data["errors"]

    def test_no_index_quiet_output(self, temp_project_dir):
        """Test health check when no index exists - quiet output."""
        # Point to non-existent index
        index_path = temp_project_dir / ".code-indexer" / "index" / "hnsw.bin"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["health", "--index-path", str(index_path), "--quiet"],
            catch_exceptions=False
        )

        assert result.exit_code == 2
        assert result.output.strip() == "NOT_FOUND"


class TestHealthCommandIntegrationCorruptedIndex:
    """Integration tests with corrupted HNSW index."""

    def test_corrupted_index_default_output(self, corrupted_index, temp_project_dir):
        """Test health check with corrupted index - default output."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["health", "--index-path", str(corrupted_index)],
            catch_exceptions=False
        )

        # Corrupted index should be detected (exit code 1)
        assert result.exit_code == 1
        assert "UNHEALTHY" in result.output or "NOT FOUND" in result.output
        # May show errors about loading
        assert "Errors Found:" in result.output or "error" in result.output.lower()

    def test_corrupted_index_json_output(self, corrupted_index, temp_project_dir):
        """Test health check with corrupted index - JSON output."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["health", "--index-path", str(corrupted_index), "--json"],
            catch_exceptions=False
        )

        # Corrupted index should be detected
        assert result.exit_code == 1

        # Parse JSON output
        data = json.loads(result.output)

        assert data["valid"] is False
        # Should have errors indicating what went wrong
        assert len(data["errors"]) > 0


class TestHealthCommandIntegrationNotReadable:
    """Integration tests when index is not readable."""

    @pytest.mark.skipif(os.geteuid() == 0, reason="Test requires non-root user")
    def test_not_readable_index_default_output(self, healthy_index, temp_project_dir):
        """Test health check when index is not readable - default output."""
        # Remove read permissions
        os.chmod(healthy_index, 0o000)

        try:
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["health", "--index-path", str(healthy_index)],
                catch_exceptions=False
            )

            # Exit code should be 2 because Click validates path before our command runs
            assert result.exit_code == 2
            # Click's error message for non-readable path
            assert "not readable" in result.output.lower()

        finally:
            # Restore permissions for cleanup
            os.chmod(healthy_index, 0o644)


class TestHealthCommandIntegrationDefaultPath:
    """Integration tests using default index path."""

    def test_default_path_with_existing_index(self, healthy_index, temp_project_dir):
        """Test health check using default path when index exists."""
        runner = CliRunner()

        # Save current directory
        original_dir = os.getcwd()
        try:
            # Change to temp directory so default path is used
            os.chdir(temp_project_dir)

            result = runner.invoke(
                cli,
                ["health"],
                catch_exceptions=False
            )

            # Should find the index at .code-indexer/index/hnsw.bin
            assert result.exit_code == 0
            assert "HEALTHY" in result.output
        finally:
            # Restore original directory
            os.chdir(original_dir)

    def test_default_path_no_index(self, temp_project_dir):
        """Test health check using default path when no index exists."""
        runner = CliRunner()

        # Save current directory
        original_dir = os.getcwd()
        try:
            # Change to temp directory where no index exists
            os.chdir(temp_project_dir)

            result = runner.invoke(
                cli,
                ["health"],
                catch_exceptions=False
            )

            # Should report no index found
            assert result.exit_code == 2
            assert "NOT FOUND" in result.output or "Index file not found" in result.output
        finally:
            # Restore original directory
            os.chdir(original_dir)


class TestHealthCommandIntegrationTimingMetrics:
    """Integration tests for timing metrics."""

    def test_timing_info_in_output(self, healthy_index, temp_project_dir):
        """Test that timing information is displayed."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["health", "--index-path", str(healthy_index)],
            catch_exceptions=False
        )

        assert result.exit_code == 0
        assert "Check Duration:" in result.output
        assert "ms" in result.output.lower()

    def test_timing_info_in_json(self, healthy_index, temp_project_dir):
        """Test that timing information is in JSON output."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["health", "--index-path", str(healthy_index), "--json"],
            catch_exceptions=False
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "check_duration_ms" in data
        assert isinstance(data["check_duration_ms"], (int, float))
        assert data["check_duration_ms"] > 0
