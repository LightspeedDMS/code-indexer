"""FileService._get_repository_path log-level discipline.

A repository that is simply absent on disk is an EXPECTED not-found condition,
not a server error.  It must still raise ``FileNotFoundError`` (callers depend on
that for the Story #1039 global-recovery + clean not-found response), but it must
NOT spam an ERROR-level ``[CACHE-GENERAL-011]`` log line.

A genuine underlying failure (e.g. the activated-repo manager raising a
``RuntimeError``) IS a server error and MUST still be logged at ERROR level.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.file_service import FileListingService


def _make_file_service(arm: MagicMock) -> FileListingService:
    svc = FileListingService.__new__(FileListingService)  # bypass __init__ wiring
    svc.activated_repo_manager = arm  # type: ignore[attr-defined]
    return svc


class TestRepoPathMissingIsNotErrorLogged:
    def test_absent_repo_raises_filenotfound_without_error_log(self):
        arm = MagicMock()
        # Path resolves to a string that does not exist on disk.
        arm.get_activated_repo_path.return_value = "/nonexistent/path/repo"

        svc = _make_file_service(arm)

        with patch("code_indexer.server.services.file_service.logger") as mock_logger:
            with pytest.raises(FileNotFoundError):
                svc._get_repository_path("fastapi", "testuser")

        # Expected not-found: NO ERROR-level escalation.
        mock_logger.error.assert_not_called()

    def test_genuine_runtime_failure_still_logs_error(self):
        arm = MagicMock()
        arm.get_activated_repo_path.side_effect = RuntimeError("db exploded")

        svc = _make_file_service(arm)

        with patch("code_indexer.server.services.file_service.logger") as mock_logger:
            with pytest.raises(RuntimeError):
                svc._get_repository_path("fastapi", "testuser")

        # Genuine failure: ERROR log MUST be emitted.
        mock_logger.error.assert_called_once()
