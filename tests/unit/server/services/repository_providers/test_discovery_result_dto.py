"""
Tests for Bug #685: RepositoryDiscoveryResult DTO cursor-based fields.

Covers only the DTO contract changes:
- New fields: has_next_page, next_cursor, partial_due_to_cap, source_total
- Removal of old fields: page, total_pages, total_count
"""

import pytest
from pydantic import ValidationError


def _make_valid_result(**overrides):
    """Build a RepositoryDiscoveryResult with all required cursor fields."""
    from code_indexer.server.models.auto_discovery import RepositoryDiscoveryResult

    defaults = {
        "repositories": [],
        "page_size": 50,
        "platform": "github",
        "has_next_page": False,
        "next_cursor": None,
        "partial_due_to_cap": False,
        "source_total": None,
    }
    defaults.update(overrides)
    return RepositoryDiscoveryResult(**defaults)


class TestRepositoryDiscoveryResultNewFields:
    """New cursor-based fields must be present and correctly typed."""

    def test_next_cursor_none_accepted(self):
        result = _make_valid_result(next_cursor=None)
        assert result.next_cursor is None

    def test_next_cursor_string_accepted(self):
        result = _make_valid_result(
            next_cursor="opaque_cursor_string", has_next_page=True
        )
        assert result.next_cursor == "opaque_cursor_string"

    def test_has_next_page_false(self):
        result = _make_valid_result(has_next_page=False)
        assert result.has_next_page is False

    def test_has_next_page_true(self):
        result = _make_valid_result(has_next_page=True, next_cursor="c")
        assert result.has_next_page is True

    def test_partial_due_to_cap_false(self):
        result = _make_valid_result(partial_due_to_cap=False)
        assert result.partial_due_to_cap is False

    def test_partial_due_to_cap_true(self):
        result = _make_valid_result(partial_due_to_cap=True, has_next_page=True)
        assert result.partial_due_to_cap is True

    def test_source_total_none_accepted(self):
        result = _make_valid_result(source_total=None)
        assert result.source_total is None

    def test_source_total_integer_accepted(self):
        result = _make_valid_result(source_total=250)
        assert result.source_total == 250


class TestRepositoryDiscoveryResultOldFieldsRemoved:
    """Old pagination fields must not be accepted even when all new fields are supplied."""

    def test_page_field_causes_validation_error(self):
        """Supplying the old 'page' field alongside valid new fields must raise ValidationError."""
        from code_indexer.server.models.auto_discovery import RepositoryDiscoveryResult

        with pytest.raises(ValidationError) as exc_info:
            RepositoryDiscoveryResult(
                repositories=[],
                page_size=50,
                platform="github",
                has_next_page=False,
                next_cursor=None,
                partial_due_to_cap=False,
                source_total=None,
                # legacy fields that must be rejected:
                page=1,
                total_pages=5,
                total_count=250,
            )
        # Pydantic v2 raises on extra fields when model is configured to forbid them.
        # The error must mention at least one of the legacy field names.
        error_text = str(exc_info.value).lower()
        assert any(
            name in error_text for name in ("page", "total_pages", "total_count")
        ), f"Expected legacy field name in validation error, got: {exc_info.value}"

    def test_result_has_no_page_attribute(self):
        result = _make_valid_result()
        assert not hasattr(result, "page")
        assert not hasattr(result, "total_pages")
        assert not hasattr(result, "total_count")
