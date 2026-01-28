"""Unit tests for handlers _apply_*_truncation sync conversion.

Story #50: Sync Payload Cache and Truncation Helpers
AC3: Handler Truncation Functions Sync Conversion

These tests verify that _apply_*_truncation functions are synchronous (not coroutines).
TDD: Tests written BEFORE implementation.
"""

import inspect
import pytest


class TestHandlersTruncationFunctionsSyncMethods:
    """Tests verifying _apply_*_truncation functions are sync (not async)."""

    def test_apply_payload_truncation_is_sync_function(self):
        """
        _apply_payload_truncation() should be a sync function, not async.

        Given handlers module
        When checking if _apply_payload_truncation is a coroutine function
        Then it should return False (sync function)
        """
        from code_indexer.server.mcp.handlers import _apply_payload_truncation

        # Verify _apply_payload_truncation is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            _apply_payload_truncation
        ), "_apply_payload_truncation() should be sync, not async"

    def test_apply_fts_payload_truncation_is_sync_function(self):
        """
        _apply_fts_payload_truncation() should be a sync function, not async.

        Given handlers module
        When checking if _apply_fts_payload_truncation is a coroutine function
        Then it should return False (sync function)
        """
        from code_indexer.server.mcp.handlers import _apply_fts_payload_truncation

        # Verify _apply_fts_payload_truncation is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            _apply_fts_payload_truncation
        ), "_apply_fts_payload_truncation() should be sync, not async"

    def test_apply_regex_payload_truncation_is_sync_function(self):
        """
        _apply_regex_payload_truncation() should be a sync function, not async.

        Given handlers module
        When checking if _apply_regex_payload_truncation is a coroutine function
        Then it should return False (sync function)
        """
        from code_indexer.server.mcp.handlers import _apply_regex_payload_truncation

        # Verify _apply_regex_payload_truncation is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            _apply_regex_payload_truncation
        ), "_apply_regex_payload_truncation() should be sync, not async"

    def test_apply_temporal_payload_truncation_is_sync_function(self):
        """
        _apply_temporal_payload_truncation() should be a sync function, not async.

        Given handlers module
        When checking if _apply_temporal_payload_truncation is a coroutine function
        Then it should return False (sync function)
        """
        from code_indexer.server.mcp.handlers import _apply_temporal_payload_truncation

        # Verify _apply_temporal_payload_truncation is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            _apply_temporal_payload_truncation
        ), "_apply_temporal_payload_truncation() should be sync, not async"

    def test_apply_scip_payload_truncation_is_sync_function(self):
        """
        _apply_scip_payload_truncation() should be a sync function, not async.

        Given handlers module
        When checking if _apply_scip_payload_truncation is a coroutine function
        Then it should return False (sync function)
        """
        from code_indexer.server.mcp.handlers import _apply_scip_payload_truncation

        # Verify _apply_scip_payload_truncation is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            _apply_scip_payload_truncation
        ), "_apply_scip_payload_truncation() should be sync, not async"

    def test_truncate_regex_field_is_sync_function(self):
        """
        _truncate_regex_field() should be a sync function, not async.

        Given handlers module
        When checking if _truncate_regex_field is a coroutine function
        Then it should return False (sync function)
        """
        from code_indexer.server.mcp.handlers import _truncate_regex_field

        # Verify _truncate_regex_field is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            _truncate_regex_field
        ), "_truncate_regex_field() should be sync, not async"

    def test_truncate_field_is_sync_function(self):
        """
        _truncate_field() should be a sync function, not async.

        Given handlers module
        When checking if _truncate_field is a coroutine function
        Then it should return False (sync function)
        """
        from code_indexer.server.mcp.handlers import _truncate_field

        # Verify _truncate_field is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            _truncate_field
        ), "_truncate_field() should be sync, not async"
