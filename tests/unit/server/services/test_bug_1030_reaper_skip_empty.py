"""
Tests for Bug #1030 Fix B: Reaper must skip repos with empty username/user_alias.

Root Cause:
  run_reap_cycle() submits deactivation jobs for repos where username or
  user_alias is empty string (phantom entries from list_all_activated_repositories).
  These jobs always fail because there is no way to locate a repo with an empty
  identity. This contributes to the endless fail-reschedule loop.

Fix B:
  Skip entries where username or user_alias is empty/missing.
  Add to errors list with a "Skipped: missing username or user_alias" message
  so the problem is visible in cycle results without aborting the cycle.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock


from code_indexer.server.services.activated_reaper_service import (
    ActivatedReaperService,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _expired_iso() -> str:
    """Return an ISO timestamp older than any reasonable TTL."""
    return (_utcnow() - timedelta(days=999)).isoformat()


def _make_repo(
    username: str,
    user_alias: str,
    last_accessed: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "username": username,
        "user_alias": user_alias,
        "last_accessed": last_accessed or _expired_iso(),
        "golden_repo_alias": "golden-repo",
    }


def _build_service(repos, ttl_days: int = 30) -> ActivatedReaperService:
    """Build an ActivatedReaperService with mocked dependencies."""
    mgr = MagicMock()
    mgr.list_all_activated_repositories.return_value = repos

    background_job_manager = MagicMock()
    background_job_manager.submit_job.return_value = "job-001"

    config_service = MagicMock()
    config_service.get_config.return_value.activated_reaper_config.ttl_days = ttl_days

    return ActivatedReaperService(
        activated_repo_manager=mgr,
        background_job_manager=background_job_manager,
        config_service=config_service,
    )


# ---------------------------------------------------------------------------
# Tests for Fix B: skip entries with empty identity fields
# ---------------------------------------------------------------------------


class TestBug1030ReaperSkipEmptyIdentity:
    """Reaper must skip phantom repos with empty username or user_alias."""

    def test_bug_1030_reaper_skips_empty_username(self) -> None:
        """
        A repo with empty username must NOT have a deactivation job submitted.
        It must appear in errors (not reaped), and the cycle must continue.
        """
        repos = [_make_repo(username="", user_alias="some-repo")]
        service = _build_service(repos)

        result = service.run_reap_cycle()

        # No job submitted for the empty-username entry
        service._background_job_manager.submit_job.assert_not_called()
        # The entry must appear in errors (visible problem, not silently dropped)
        assert len(result["errors"]) == 1
        assert result["errors"][0]["username"] == ""
        assert result["errors"][0]["user_alias"] == "some-repo"
        assert "missing" in result["errors"][0]["error"].lower()
        # Not counted as reaped
        assert len(result["reaped"]) == 0

    def test_bug_1030_reaper_skips_empty_user_alias(self) -> None:
        """
        A repo with empty user_alias must NOT have a deactivation job submitted.
        It must appear in errors.
        """
        repos = [_make_repo(username="alice", user_alias="")]
        service = _build_service(repos)

        result = service.run_reap_cycle()

        service._background_job_manager.submit_job.assert_not_called()
        assert len(result["errors"]) == 1
        assert result["errors"][0]["username"] == "alice"
        assert result["errors"][0]["user_alias"] == ""
        assert "missing" in result["errors"][0]["error"].lower()
        assert len(result["reaped"]) == 0

    def test_bug_1030_reaper_skips_both_empty(self) -> None:
        """
        A repo with both username and user_alias empty must be skipped.
        """
        repos = [_make_repo(username="", user_alias="")]
        service = _build_service(repos)

        result = service.run_reap_cycle()

        service._background_job_manager.submit_job.assert_not_called()
        assert len(result["errors"]) == 1
        assert "missing" in result["errors"][0]["error"].lower()

    def test_bug_1030_reaper_skips_phantom_but_processes_valid(self) -> None:
        """
        Mix: one phantom (empty username) + one valid expired repo.
        The phantom is skipped; the valid one gets a deactivation job.
        """
        repos = [
            _make_repo(username="", user_alias="ghost-repo"),
            _make_repo(username="bob", user_alias="real-repo"),
        ]
        service = _build_service(repos)

        result = service.run_reap_cycle()

        # Only one job submitted (for bob/real-repo)
        assert service._background_job_manager.submit_job.call_count == 1
        # One reaped, one error
        assert len(result["reaped"]) == 1
        assert result["reaped"][0]["username"] == "bob"
        assert result["reaped"][0]["user_alias"] == "real-repo"
        assert len(result["errors"]) == 1
        assert result["errors"][0]["username"] == ""

    def test_bug_1030_reaper_missing_fields_treated_as_empty(self) -> None:
        """
        Repos returned with missing keys (not just empty strings) must also be skipped.
        repo.get("username", "") returns "" when key is absent.
        """
        repos = [
            {"last_accessed": _expired_iso(), "golden_repo_alias": "g"}
        ]  # no username/user_alias keys
        service = _build_service(repos)

        result = service.run_reap_cycle()

        service._background_job_manager.submit_job.assert_not_called()
        assert len(result["errors"]) == 1
        assert "missing" in result["errors"][0]["error"].lower()

    def test_bug_1030_scanned_count_includes_phantom_repos(self) -> None:
        """
        scanned count must reflect ALL repos including phantoms,
        so operators can spot the problem in cycle metrics.
        """
        repos = [
            _make_repo(username="", user_alias="ghost"),
            _make_repo(username="alice", user_alias="real"),
        ]
        service = _build_service(repos)

        result = service.run_reap_cycle()

        assert result["scanned"] == 2
