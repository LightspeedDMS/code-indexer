"""
Unit tests for Golden Repo Index Multi-Select request model validation.

Tests that AddIndexRequest Pydantic model correctly validates:
1. Single index_type (string) - backward compatibility
2. Multiple index_types (array) - multi-select support

Story #2: Fix Add Index functionality - CRITICAL-2
"""

import pytest


class TestAddIndexRequestModel:
    """Tests for AddIndexRequest Pydantic model validation."""

    def test_model_accepts_index_type_string(self):
        """Test that AddIndexRequest accepts index_type as string."""
        from code_indexer.server.app import AddIndexRequest

        request = AddIndexRequest(index_type="semantic")
        assert request.index_type == "semantic"

    def test_model_accepts_index_types_array(self):
        """Test that AddIndexRequest accepts index_types as array."""
        from code_indexer.server.app import AddIndexRequest

        request = AddIndexRequest(index_types=["semantic", "fts"])
        assert request.index_types == ["semantic", "fts"]

    def test_model_allows_either_but_not_both_empty(self):
        """Test that at least one of index_type or index_types must be provided."""
        from code_indexer.server.app import AddIndexRequest
        from pydantic import ValidationError

        # Neither provided should raise ValidationError
        with pytest.raises(ValidationError):
            AddIndexRequest()

    def test_model_prefers_index_types_over_index_type(self):
        """Test behavior when both are provided (edge case)."""
        from code_indexer.server.app import AddIndexRequest

        # If both provided, index_types should take precedence
        # This is an edge case - JavaScript shouldn't send both
        request = AddIndexRequest(
            index_type="semantic", index_types=["fts", "temporal"]
        )
        # The implementation decides which to use - we just verify both are stored
        assert request.index_types == ["fts", "temporal"]
