"""
Tests for FileCRUDService.get_write_exception_path() method (Story #197).

Tests the public method to retrieve exception paths for registered aliases.
"""

import pytest
from pathlib import Path
from code_indexer.server.services.file_crud_service import FileCRUDService


class TestGetWriteExceptionPath:
    """Test get_write_exception_path public method."""

    def test_returns_path_for_registered_exception(self):
        """Test that get_write_exception_path returns path for registered alias."""
        service = FileCRUDService()
        canonical_path = Path("/fake/path/to/cidx-meta")

        service.register_write_exception("cidx-meta-global", canonical_path)

        result = service.get_write_exception_path("cidx-meta-global")

        assert result == canonical_path

    def test_returns_none_for_unregistered_alias(self):
        """Test that get_write_exception_path returns None for non-registered alias."""
        service = FileCRUDService()

        result = service.get_write_exception_path("unknown-alias")

        assert result is None

    def test_returns_none_after_clearing_exceptions(self):
        """Test that method returns None after exceptions are cleared."""
        service = FileCRUDService()
        canonical_path = Path("/fake/cidx-meta")

        service.register_write_exception("cidx-meta-global", canonical_path)
        # Clear the map
        service._global_write_exceptions.clear()

        result = service.get_write_exception_path("cidx-meta-global")

        assert result is None

    def test_returns_correct_path_for_multiple_registrations(self):
        """Test that method returns correct path when multiple exceptions registered."""
        service = FileCRUDService()
        path1 = Path("/fake/cidx-meta")
        path2 = Path("/fake/other-repo")

        service.register_write_exception("cidx-meta-global", path1)
        service.register_write_exception("other-global", path2)

        assert service.get_write_exception_path("cidx-meta-global") == path1
        assert service.get_write_exception_path("other-global") == path2
        assert service.get_write_exception_path("unknown") is None
