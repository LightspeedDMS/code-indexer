"""Unit tests for temporal path filter type bug.

Bug: --exclude-path and --path-filter with --time-range-all return ZERO results.

Root Cause:
1. CLI receives tuple: ("*.md",)
2. Daemon delegation incorrectly converts: list(exclude_path)[0] → string "*.md"
3. Daemon service has wrong type: Optional[str] instead of Optional[List[str]]
4. TemporalSearchService does list("*.md") → ['*', '.', 'm', 'd'] (character array!)
5. Creates filters for single characters → ZERO results

This test proves the bug exists by testing daemon service parameter signatures.
"""

import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

# Mock rpyc before import if not available
try:
    import rpyc
except ImportError:
    sys.modules["rpyc"] = MagicMock()
    sys.modules["rpyc.utils.server"] = MagicMock()
    rpyc = sys.modules["rpyc"]

from src.code_indexer.daemon.service import CIDXDaemonService


class TestTemporalPathFilterBug(TestCase):
    """Test temporal path filter type bug."""

    def test_daemon_service_path_filter_signature_should_be_list(self):
        """Daemon service path_filter parameter should be Optional[List[str]], not Optional[str]."""

        service = CIDXDaemonService()

        # Get method signature
        import inspect

        sig = inspect.signature(service.exposed_query_temporal)

        # Check path_filter parameter type annotation
        path_filter_param = sig.parameters.get("path_filter")
        assert path_filter_param is not None, "path_filter parameter should exist"

        # Extract type annotation string representation
        annotation_str = str(path_filter_param.annotation)

        # Should be Optional[List[str]], NOT Optional[str]
        assert "Optional[str]" != annotation_str or "[" in annotation_str, (
            "BUG: path_filter signature is Optional[str], should be Optional[List[str]]"
        )
        assert "List[str]" in annotation_str or "list[str]" in annotation_str, (
            f"path_filter signature should contain List[str], got {annotation_str}"
        )

    def test_daemon_service_exclude_path_signature_should_be_list(self):
        """Daemon service exclude_path parameter should be Optional[List[str]], not Optional[str]."""

        service = CIDXDaemonService()

        # Get method signature
        import inspect

        sig = inspect.signature(service.exposed_query_temporal)

        # Check exclude_path parameter type annotation
        exclude_path_param = sig.parameters.get("exclude_path")
        assert exclude_path_param is not None, "exclude_path parameter should exist"

        # Extract type annotation string representation
        annotation_str = str(exclude_path_param.annotation)

        # Should be Optional[List[str]], NOT Optional[str]
        assert "Optional[str]" != annotation_str or "[" in annotation_str, (
            "BUG: exclude_path signature is Optional[str], should be Optional[List[str]]"
        )
        assert "List[str]" in annotation_str or "list[str]" in annotation_str, (
            f"exclude_path signature should contain List[str], got {annotation_str}"
        )

    def test_daemon_handles_multiple_path_filters_correctly(self):
        """Daemon should forward multiple path filter patterns to
        execute_temporal_query_with_fusion() as comma-joined strings (Bug
        #1302: daemon now routes through the same shard-aware fusion-dispatch
        machinery the standalone CLI temporal path uses, which accepts a
        single comma-joined file_path_filter/exclude_path string -- see
        cli.py's own ",".join(...) convention at the standalone call site)."""
        import tempfile

        service = CIDXDaemonService()

        # Create temporary project structure
        temp_dir = tempfile.mkdtemp()
        project_path = Path(temp_dir) / "test_project"
        project_path.mkdir(parents=True, exist_ok=True)

        try:
            from unittest.mock import patch, MagicMock
            from code_indexer.services.temporal.temporal_search_service import (
                TemporalSearchResults,
            )

            # Mock dependencies
            with patch(
                "code_indexer.config.ConfigManager.create_with_backtrack"
            ) as mock_config:
                with patch(
                    "code_indexer.backends.backend_factory.BackendFactory.create"
                ) as mock_backend_factory:
                    with patch(
                        "code_indexer.services.temporal.temporal_fusion_dispatch.execute_temporal_query_with_fusion"
                    ) as mock_execute_fusion:
                        # Setup mocks
                        mock_config.return_value = MagicMock()
                        mock_backend = MagicMock()
                        mock_backend.get_vector_store_client.return_value = MagicMock()
                        mock_backend_factory.return_value = mock_backend

                        mock_execute_fusion.return_value = TemporalSearchResults(
                            results=[],
                            query="authentication",
                            filter_type="time_range",
                            filter_value="2024-01-01..2024-12-31",
                            total_found=0,
                        )

                        # Patch cache to avoid threading issues
                        with patch.object(service, "cache_lock"):
                            with patch.object(service, "_ensure_cache_loaded"):
                                with patch.object(
                                    service, "cache_entry"
                                ) as mock_cache_entry:
                                    mock_cache_entry.project_path = project_path

                                    # Call with multiple path filters
                                    service.exposed_query_temporal(
                                        project_path=str(project_path),
                                        query="authentication",
                                        time_range="2024-01-01..2024-12-31",
                                        limit=10,
                                        path_filter=["*.py", "*.js"],
                                        exclude_path=["*/tests/*", "*/docs/*"],
                                    )

                                    # Verify execute_temporal_query_with_fusion was called
                                    mock_execute_fusion.assert_called_once()
                                    call_kwargs = mock_execute_fusion.call_args[1]

                                    # Verify comma-joined strings passed correctly
                                    path_filter_arg = call_kwargs.get(
                                        "file_path_filter"
                                    )
                                    exclude_path_arg = call_kwargs.get("exclude_path")

                                    assert path_filter_arg == "*.py,*.js"
                                    assert exclude_path_arg == "*/tests/*,*/docs/*"
        finally:
            # Cleanup
            import shutil

            if Path(temp_dir).exists():
                shutil.rmtree(temp_dir)
