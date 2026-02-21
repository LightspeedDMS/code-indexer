"""
Tests for fail-closed write mode check (Finding 6, services layer review).

When _golden_repos_dir is None and repo IS a write-exception,
_check_write_mode_active() must raise PermissionError (fail-closed),
not silently allow the operation (fail-open).
"""

import pytest
from unittest.mock import MagicMock
from code_indexer.server.services.file_crud_service import FileCRUDService


class TestWriteModeFailClosed:
    """Finding 6: _check_write_mode_active must be fail-closed."""

    def test_raises_when_golden_repos_dir_none_and_write_exception(self):
        """Write-exception repo + no golden_repos_dir = PermissionError."""
        service = FileCRUDService.__new__(FileCRUDService)
        service._golden_repos_dir = None
        service._global_write_exceptions = {"cidx-meta-global": None}

        with pytest.raises(PermissionError, match="golden_repos_dir not configured"):
            service._check_write_mode_active("cidx-meta-global")

    def test_no_error_when_not_write_exception(self):
        """Non-write-exception repo + no golden_repos_dir = no error (check skipped)."""
        service = FileCRUDService.__new__(FileCRUDService)
        service._golden_repos_dir = None
        service._global_write_exceptions = {"cidx-meta-global": None}

        # Should return without error - repo is NOT a write exception
        service._check_write_mode_active("some-other-repo-global")

    def test_raises_when_no_marker_file(self):
        """Write-exception repo + golden_repos_dir set + no marker = PermissionError."""
        service = FileCRUDService.__new__(FileCRUDService)
        service._golden_repos_dir = MagicMock()
        service._global_write_exceptions = {"cidx-meta-global": None}

        # Mock the marker file path to not exist
        mock_marker = MagicMock()
        mock_marker.exists.return_value = False
        service._golden_repos_dir.__truediv__ = MagicMock(
            return_value=MagicMock(
                **{"__truediv__": MagicMock(return_value=mock_marker)}
            )
        )

        with pytest.raises(PermissionError, match="requires write mode"):
            service._check_write_mode_active("cidx-meta-global")
