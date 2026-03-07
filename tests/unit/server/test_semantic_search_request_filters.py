"""
Unit tests for Story #375: SemanticSearchRequest model filter fields.

Tests cover AC1-AC4 model validation:
- AC1: path_filter field
- AC2: language and exclude_language fields
- AC3: exclude_path field
- AC4: accuracy field

TDD red phase: tests written FIRST to define expected model shape.
Production file: src/code_indexer/server/models/api_models.py
"""

import pytest
from src.code_indexer.server.models.api_models import SemanticSearchRequest


class TestSemanticSearchRequestFilterFields:
    """AC1-AC4: SemanticSearchRequest model must accept all filter parameters."""

    def test_accepts_path_filter(self):
        """AC1: SemanticSearchRequest accepts path_filter field."""
        request = SemanticSearchRequest(
            query="authentication logic",
            limit=10,
            path_filter="*/src/*",
        )
        assert request.path_filter == "*/src/*"

    def test_accepts_language(self):
        """AC2: SemanticSearchRequest accepts language field."""
        request = SemanticSearchRequest(
            query="authentication logic",
            limit=10,
            language="python",
        )
        assert request.language == "python"

    def test_accepts_exclude_language(self):
        """AC2: SemanticSearchRequest accepts exclude_language field."""
        request = SemanticSearchRequest(
            query="authentication logic",
            limit=10,
            exclude_language="javascript",
        )
        assert request.exclude_language == "javascript"

    def test_accepts_exclude_path(self):
        """AC3: SemanticSearchRequest accepts exclude_path field."""
        request = SemanticSearchRequest(
            query="authentication logic",
            limit=10,
            exclude_path="*/tests/*",
        )
        assert request.exclude_path == "*/tests/*"

    def test_accepts_accuracy(self):
        """AC4: SemanticSearchRequest accepts accuracy field."""
        request = SemanticSearchRequest(
            query="authentication logic",
            limit=10,
            accuracy="high",
        )
        assert request.accuracy == "high"

    def test_all_filter_fields_are_optional_with_none_default(self):
        """Backward compatibility: all filter fields are optional (default None)."""
        request = SemanticSearchRequest(
            query="authentication logic",
            limit=10,
        )
        assert request.path_filter is None
        assert request.language is None
        assert request.exclude_language is None
        assert request.exclude_path is None
        assert request.accuracy is None

    def test_all_filter_fields_set_simultaneously(self):
        """All filter fields can be set at the same time."""
        request = SemanticSearchRequest(
            query="authentication logic",
            limit=10,
            include_source=True,
            path_filter="*/src/*",
            language="python",
            exclude_language="javascript",
            exclude_path="*/tests/*",
            accuracy="high",
        )
        assert request.path_filter == "*/src/*"
        assert request.language == "python"
        assert request.exclude_language == "javascript"
        assert request.exclude_path == "*/tests/*"
        assert request.accuracy == "high"

    def test_existing_fields_still_work(self):
        """Backward compatibility: existing fields (query, limit, include_source) unchanged."""
        request = SemanticSearchRequest(
            query="test query",
            limit=5,
            include_source=False,
        )
        assert request.query == "test query"
        assert request.limit == 5
        assert request.include_source is False

    def test_accuracy_accepts_fast(self):
        """AC4: accuracy field accepts 'fast' value."""
        request = SemanticSearchRequest(query="test", accuracy="fast")
        assert request.accuracy == "fast"

    def test_accuracy_accepts_balanced(self):
        """AC4: accuracy field accepts 'balanced' value."""
        request = SemanticSearchRequest(query="test", accuracy="balanced")
        assert request.accuracy == "balanced"

    def test_accuracy_accepts_high(self):
        """AC4: accuracy field accepts 'high' value."""
        request = SemanticSearchRequest(query="test", accuracy="high")
        assert request.accuracy == "high"
