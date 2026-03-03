"""Tests for scip_context timeout enforcement.

Bug #351: scip_context hangs indefinitely for large symbols on large repos.

Acceptance Criteria:
    AC1: scip_context call should return results (or an error) within a
         reasonable timeout (~30 seconds) -- it must NOT hang indefinitely.

These tests verify:
    1. get_smart_context() accepts a timeout_seconds parameter
    2. get_smart_context() raises an exception when the timeout is exceeded
    3. SCIPQueryService.get_context() passes timeout_seconds through
    4. scip_context MCP handler returns an error (not hang) when timeout occurs
"""

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class TestGetSmartContextTimeoutParameter:
    """get_smart_context() must accept a timeout_seconds parameter."""

    def test_get_smart_context_accepts_timeout_seconds(self, tmp_path: Path) -> None:
        """get_smart_context() must accept timeout_seconds keyword argument."""
        from code_indexer.scip.query.composites import get_smart_context

        # Calling with timeout_seconds must not raise TypeError
        # (empty dir = empty result, no DB work done)
        result = get_smart_context("Symbol", tmp_path, timeout_seconds=30)
        assert result is not None

    def test_get_smart_context_default_timeout_is_30_seconds(
        self, tmp_path: Path
    ) -> None:
        """get_smart_context() must have a default timeout_seconds of 30."""
        import inspect
        from code_indexer.scip.query.composites import get_smart_context

        sig = inspect.signature(get_smart_context)
        assert "timeout_seconds" in sig.parameters, (
            "get_smart_context() must have a timeout_seconds parameter"
        )
        default = sig.parameters["timeout_seconds"].default
        assert default == 30, (
            f"timeout_seconds default must be 30, got {default}"
        )

    def test_get_smart_context_raises_on_timeout(self, tmp_path: Path) -> None:
        """get_smart_context() must raise an exception when timeout is exceeded.

        Simulates a slow query by making analyze_impact hang, then verifies
        get_smart_context returns within timeout_seconds (not hang forever).
        """
        from code_indexer.scip.query.composites import get_smart_context

        # Create a fake .scip.db file so the function tries to do real work
        scip_dir = tmp_path / "repo" / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        db_file = scip_dir / "index.scip.db"
        db_file.write_bytes(b"")  # Empty but exists

        def slow_analyze_impact(*args: Any, **kwargs: Any) -> None:
            """Simulate a query that takes longer than the timeout."""
            time.sleep(10)  # Much longer than timeout_seconds=1

        with patch(
            "code_indexer.scip.query.composites.analyze_impact",
            side_effect=slow_analyze_impact,
        ):
            start = time.monotonic()
            # Must NOT hang: should raise or return within ~2 seconds
            with pytest.raises(Exception):
                get_smart_context("Symbol", tmp_path, timeout_seconds=1)
            elapsed = time.monotonic() - start

        # Must return well within double the timeout (generous for CI)
        assert elapsed < 5.0, (
            f"get_smart_context() took {elapsed:.1f}s — should have timed out within 1s"
        )

    def test_get_smart_context_timeout_exception_type(self, tmp_path: Path) -> None:
        """get_smart_context() must raise a QueryTimeoutError on timeout."""
        from code_indexer.scip.query.composites import get_smart_context
        from code_indexer.scip.database.queries import QueryTimeoutError

        scip_dir = tmp_path / "repo" / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        db_file = scip_dir / "index.scip.db"
        db_file.write_bytes(b"")

        def slow_analyze_impact(*args: Any, **kwargs: Any) -> None:
            time.sleep(10)

        with patch(
            "code_indexer.scip.query.composites.analyze_impact",
            side_effect=slow_analyze_impact,
        ):
            with pytest.raises(QueryTimeoutError):
                get_smart_context("Symbol", tmp_path, timeout_seconds=1)

    def test_get_smart_context_returns_results_within_timeout(
        self, tmp_path: Path
    ) -> None:
        """get_smart_context() returns normally when query finishes within timeout."""
        from code_indexer.scip.query.composites import get_smart_context, SmartContextResult

        # Empty dir = no SCIP files = fast return
        result = get_smart_context("Symbol", tmp_path, timeout_seconds=30)

        assert isinstance(result, SmartContextResult)
        assert result.target_symbol == "Symbol"
        assert result.total_files == 0


class TestSCIPQueryServiceContextTimeout:
    """SCIPQueryService.get_context() must pass timeout to get_smart_context."""

    def test_get_context_passes_timeout_to_get_smart_context(
        self, tmp_path: Path
    ) -> None:
        """SCIPQueryService.get_context() must forward timeout_seconds."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.query.composites import SmartContextResult

        golden_repos = tmp_path / "golden-repos"
        golden_repos.mkdir()

        service = SCIPQueryService(str(golden_repos), None)

        mock_result = SmartContextResult(
            target_symbol="Symbol",
            summary="Read these 0 file(s)",
            files=[],
            total_files=0,
            total_symbols=0,
            avg_relevance=0.0,
        )

        with patch(
            "code_indexer.scip.query.composites.get_smart_context",
            return_value=mock_result,
        ) as mock_ctx:
            service.get_context("Symbol", timeout_seconds=45)

        call_kwargs = mock_ctx.call_args[1]
        assert "timeout_seconds" in call_kwargs, (
            "get_context() must pass timeout_seconds to get_smart_context()"
        )
        assert call_kwargs["timeout_seconds"] == 45

    def test_get_context_default_timeout_is_30_seconds(self, tmp_path: Path) -> None:
        """SCIPQueryService.get_context() must default timeout_seconds=30."""
        import inspect
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        sig = inspect.signature(SCIPQueryService.get_context)
        assert "timeout_seconds" in sig.parameters, (
            "get_context() must have a timeout_seconds parameter"
        )
        default = sig.parameters["timeout_seconds"].default
        assert default == 30, (
            f"get_context timeout_seconds default must be 30, got {default}"
        )

    def test_get_context_propagates_timeout_error(self, tmp_path: Path) -> None:
        """SCIPQueryService.get_context() must propagate timeout as exception."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService
        from code_indexer.scip.database.queries import QueryTimeoutError

        golden_repos = tmp_path / "golden-repos"
        golden_repos.mkdir()

        service = SCIPQueryService(str(golden_repos), None)

        with patch(
            "code_indexer.scip.query.composites.get_smart_context",
            side_effect=QueryTimeoutError("Query exceeded timeout limit"),
        ):
            with pytest.raises(QueryTimeoutError):
                service.get_context("Symbol", timeout_seconds=1)


class TestScipContextHandlerTimeout:
    """scip_context MCP handler must not hang when timeout is exceeded."""

    def test_scip_context_handler_returns_error_on_timeout(self) -> None:
        """scip_context MCP handler must return error dict (not hang) on timeout."""
        from code_indexer.server.mcp.handlers import scip_context
        from code_indexer.scip.database.queries import QueryTimeoutError
        import json

        mock_user = MagicMock()
        mock_user.username = "testuser"

        mock_service = MagicMock()
        mock_service.get_context.side_effect = QueryTimeoutError(
            "Query exceeded timeout limit"
        )

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            response = scip_context({"symbol": "GoldenRepoManager"}, mock_user)

        # Must return a response (not raise, not hang)
        assert "content" in response, "Handler must return MCP content on timeout"
        data = json.loads(response["content"][0]["text"])
        assert data["success"] is False, "Handler must return success=False on timeout"
        assert "error" in data, "Handler must include error field on timeout"
        assert "timeout" in data["error"].lower() or "exceeded" in data["error"].lower(), (
            f"Error message must indicate timeout, got: {data['error']}"
        )

    def test_scip_context_handler_passes_timeout_to_service(self) -> None:
        """scip_context handler must pass a timeout to service.get_context()."""
        from code_indexer.server.mcp.handlers import scip_context

        mock_user = MagicMock()
        mock_user.username = "testuser"

        mock_service = MagicMock()
        mock_service.get_context.return_value = {
            "target_symbol": "Symbol",
            "summary": "Read these 0 file(s)",
            "files": [],
            "total_files": 0,
            "total_symbols": 0,
            "avg_relevance": 0.0,
        }

        with patch(
            "code_indexer.server.mcp.handlers._get_scip_query_service",
            return_value=mock_service,
        ):
            scip_context({"symbol": "Symbol"}, mock_user)

        call_kwargs = mock_service.get_context.call_args[1]
        assert "timeout_seconds" in call_kwargs, (
            "scip_context handler must pass timeout_seconds to service.get_context()"
        )
        # Default timeout must be >= 30 seconds (reasonable for large repos)
        assert call_kwargs["timeout_seconds"] >= 30, (
            f"timeout_seconds must be >= 30, got {call_kwargs['timeout_seconds']}"
        )
