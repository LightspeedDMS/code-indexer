"""
Unit tests for Story #967: ActivatedReaperService.

TDD: Tests written BEFORE implementation. All should fail (red phase) until
ActivatedReaperService is implemented.

Acceptance Criteria covered:
  AC1 - Idle repo (last_accessed older than TTL) -> deactivation job submitted
  AC2 - Recent repo (last_accessed within TTL) -> not deactivated
  AC3 - Cycle has correct scanned/reaped/skipped/error counts
  AC4 - TTL re-read from config_service each cycle (no caching)
  AC5 - Repo with null/missing last_accessed -> treated as expired
  AC6 - Error on one repo deactivation doesn't abort whole cycle
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_repo(
    username: str,
    user_alias: str,
    last_accessed: Optional[str],
) -> Dict[str, Any]:
    """Build a minimal repo dict as returned by list_all_activated_repositories."""
    return {
        "username": username,
        "user_alias": user_alias,
        "last_accessed": last_accessed,
        "golden_repo_alias": "golden-repo",
    }


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_background_job_manager():
    mgr = MagicMock()
    mgr.submit_job.return_value = "job-001"
    return mgr


@pytest.fixture
def mock_config_service_30d():
    """ConfigService mock with ttl_days=30."""
    svc = MagicMock()
    svc.get_config.return_value.activated_reaper_config.ttl_days = 30
    return svc


@pytest.fixture
def service_factory(mock_background_job_manager, mock_config_service_30d):
    """Factory to build ActivatedReaperService with provided repos list."""
    from code_indexer.server.services.activated_reaper_service import (
        ActivatedReaperService,
    )

    def _build(repos):
        mgr = MagicMock()
        mgr.list_all_activated_repositories.return_value = repos
        return ActivatedReaperService(
            activated_repo_manager=mgr,
            background_job_manager=mock_background_job_manager,
            config_service=mock_config_service_30d,
        )

    return _build


# ---------------------------------------------------------------------------
# ReapCycleResult dataclass shape
# ---------------------------------------------------------------------------


class TestReapCycleResultShape:
    """ReapCycleResult must expose required attributes."""

    def test_result_dataclass_has_all_fields(self):
        from code_indexer.server.services.activated_reaper_service import (
            ReapCycleResult,
        )

        r = ReapCycleResult(scanned=3, reaped=[], skipped=[], errors=[])
        assert hasattr(r, "scanned")
        assert hasattr(r, "reaped")
        assert hasattr(r, "skipped")
        assert hasattr(r, "errors")
        assert r.scanned == 3


# ---------------------------------------------------------------------------
# AC1: Idle repo -> deactivation job submitted
# ---------------------------------------------------------------------------


class TestReapCycleExpiredRepo:
    """AC1: Expired repos get deactivation jobs submitted."""

    def test_submits_job_for_expired_repo(
        self, service_factory, mock_background_job_manager
    ):
        """Single repo older than TTL results in deactivation job submitted."""
        old_ts = _iso(_utcnow() - timedelta(days=35))
        service = service_factory([_make_repo("alice", "my-repo", old_ts)])

        service.run_reap_cycle()

        mock_background_job_manager.submit_job.assert_called_once()
        call_kwargs = mock_background_job_manager.submit_job.call_args
        assert call_kwargs[0][0] == "deactivate_repository"
        assert call_kwargs[1]["submitter_username"] == "system"
        assert call_kwargs[1]["is_admin"] is True
        assert call_kwargs[1]["username"] == "alice"
        assert call_kwargs[1]["user_alias"] == "my-repo"

    def test_reaped_count_is_one_for_single_expired_repo(
        self, service_factory, mock_background_job_manager
    ):
        """ReapCycleResult.reaped has length 1 for one expired repo."""
        old_ts = _iso(_utcnow() - timedelta(days=35))
        service = service_factory([_make_repo("alice", "my-repo", old_ts)])

        result = service.run_reap_cycle()

        assert len(result["reaped"]) == 1
        assert result["reaped"][0]["username"] == "alice"
        assert result["reaped"][0]["user_alias"] == "my-repo"


# ---------------------------------------------------------------------------
# AC2: Recent repo -> skipped
# ---------------------------------------------------------------------------


class TestReapCycleRecentRepo:
    """AC2: Recently accessed repos are not deactivated."""

    def test_skips_recent_repo(self, service_factory, mock_background_job_manager):
        """Repo accessed 5 days ago (within 30-day TTL) should be skipped."""
        recent_ts = _iso(_utcnow() - timedelta(days=5))
        service = service_factory([_make_repo("bob", "active-repo", recent_ts)])

        result = service.run_reap_cycle()

        mock_background_job_manager.submit_job.assert_not_called()
        assert len(result["skipped"]) == 1
        assert len(result["reaped"]) == 0

    def test_skipped_entry_has_expected_keys(self, service_factory):
        """Skipped entry should contain username and user_alias."""
        recent_ts = _iso(_utcnow() - timedelta(days=1))
        service = service_factory([_make_repo("carol", "fresh-repo", recent_ts)])

        result = service.run_reap_cycle()

        assert result["skipped"][0]["username"] == "carol"
        assert result["skipped"][0]["user_alias"] == "fresh-repo"


# ---------------------------------------------------------------------------
# AC3: Result counts
# ---------------------------------------------------------------------------


class TestReapCycleResultCounts:
    """AC3: ReapCycleResult has correct scanned/reaped/skipped/errors counts."""

    def test_scanned_equals_total_repos(
        self, mock_background_job_manager, mock_config_service_30d
    ):
        """scanned equals total repos; reaped+skipped partition them."""
        from code_indexer.server.services.activated_reaper_service import (
            ActivatedReaperService,
        )

        old_ts = _iso(_utcnow() - timedelta(days=40))
        recent_ts = _iso(_utcnow() - timedelta(days=2))
        repos = [
            _make_repo("u1", "r1", old_ts),
            _make_repo("u2", "r2", recent_ts),
            _make_repo("u3", "r3", old_ts),
        ]

        mgr = MagicMock()
        mgr.list_all_activated_repositories.return_value = repos
        service = ActivatedReaperService(
            activated_repo_manager=mgr,
            background_job_manager=mock_background_job_manager,
            config_service=mock_config_service_30d,
        )

        result = service.run_reap_cycle()

        assert result["scanned"] == 3
        assert len(result["reaped"]) == 2
        assert len(result["skipped"]) == 1
        assert len(result["errors"]) == 0

    def test_empty_repo_list_returns_zero_counts(
        self, service_factory, mock_background_job_manager
    ):
        """When no repos exist, all counts are zero."""
        service = service_factory([])

        result = service.run_reap_cycle()

        assert result["scanned"] == 0
        assert len(result["reaped"]) == 0
        assert len(result["skipped"]) == 0
        assert len(result["errors"]) == 0
        mock_background_job_manager.submit_job.assert_not_called()


# ---------------------------------------------------------------------------
# AC4: TTL re-read each cycle
# ---------------------------------------------------------------------------


class TestReapConfigRereadEachCycle:
    """AC4: TTL re-read from config_service each cycle (not cached)."""

    def test_uses_current_ttl_per_cycle(self, mock_background_job_manager):
        """If config TTL changes between calls, each call uses the current value."""
        from code_indexer.server.services.activated_reaper_service import (
            ActivatedReaperService,
        )

        # 25 days old: skipped at TTL=30, reaped at TTL=20
        ts = _iso(_utcnow() - timedelta(days=25))
        repo = _make_repo("dave", "repo-x", ts)

        mgr = MagicMock()
        mgr.list_all_activated_repositories.return_value = [repo]

        config_30 = MagicMock()
        config_30.activated_reaper_config.ttl_days = 30
        config_20 = MagicMock()
        config_20.activated_reaper_config.ttl_days = 20

        config_service = MagicMock()
        config_service.get_config.side_effect = [config_30, config_20]

        service = ActivatedReaperService(
            activated_repo_manager=mgr,
            background_job_manager=mock_background_job_manager,
            config_service=config_service,
        )

        result1 = service.run_reap_cycle()
        result2 = service.run_reap_cycle()

        assert len(result1["skipped"]) == 1  # 25 days < 30 day TTL
        assert len(result1["reaped"]) == 0
        assert len(result2["reaped"]) == 1  # 25 days > 20 day TTL
        assert len(result2["skipped"]) == 0


# ---------------------------------------------------------------------------
# AC5: Null/missing last_accessed -> treated as expired
# ---------------------------------------------------------------------------


class TestReapCycleNullLastAccessed:
    """AC5: Repo with null/missing last_accessed treated as expired."""

    def test_null_last_accessed_triggers_deactivation(
        self, service_factory, mock_background_job_manager
    ):
        """Repo with last_accessed=None is treated as expired."""
        service = service_factory([_make_repo("eve", "null-repo", None)])

        result = service.run_reap_cycle()

        mock_background_job_manager.submit_job.assert_called_once()
        assert len(result["reaped"]) == 1

    def test_missing_last_accessed_key_triggers_deactivation(
        self, mock_background_job_manager, mock_config_service_30d
    ):
        """Repo dict with no last_accessed key is treated as expired."""
        from code_indexer.server.services.activated_reaper_service import (
            ActivatedReaperService,
        )

        repo = {
            "username": "frank",
            "user_alias": "no-ts-repo",
            "golden_repo_alias": "golden",
            # last_accessed intentionally absent
        }

        mgr = MagicMock()
        mgr.list_all_activated_repositories.return_value = [repo]
        service = ActivatedReaperService(
            activated_repo_manager=mgr,
            background_job_manager=mock_background_job_manager,
            config_service=mock_config_service_30d,
        )

        result = service.run_reap_cycle()

        mock_background_job_manager.submit_job.assert_called_once()
        assert len(result["reaped"]) == 1


# ---------------------------------------------------------------------------
# AC6: Error on one repo doesn't abort the cycle
# ---------------------------------------------------------------------------


class TestReapCycleErrorHandling:
    """AC6: Error on one repo deactivation doesn't abort the whole cycle."""

    def test_error_on_one_repo_continues_with_rest(self, mock_config_service_30d):
        """If submit_job raises for one repo, others are still processed."""
        from code_indexer.server.services.activated_reaper_service import (
            ActivatedReaperService,
        )

        old_ts = _iso(_utcnow() - timedelta(days=40))
        repos = [
            _make_repo("u1", "repo-fail", old_ts),
            _make_repo("u2", "repo-ok", old_ts),
        ]

        mgr = MagicMock()
        mgr.list_all_activated_repositories.return_value = repos

        background_job_manager = MagicMock()
        background_job_manager.submit_job.side_effect = [
            RuntimeError("deactivation failed"),
            "job-ok",
        ]

        service = ActivatedReaperService(
            activated_repo_manager=mgr,
            background_job_manager=background_job_manager,
            config_service=mock_config_service_30d,
        )

        result = service.run_reap_cycle()

        assert len(result["errors"]) == 1
        assert len(result["reaped"]) == 1
        assert result["scanned"] == 2

    def test_error_entry_has_username_user_alias_and_message(
        self, mock_config_service_30d
    ):
        """Error entry should include username, user_alias, error fields."""
        from code_indexer.server.services.activated_reaper_service import (
            ActivatedReaperService,
        )

        old_ts = _iso(_utcnow() - timedelta(days=40))
        repo = _make_repo("grace", "bad-repo", old_ts)

        mgr = MagicMock()
        mgr.list_all_activated_repositories.return_value = [repo]

        background_job_manager = MagicMock()
        background_job_manager.submit_job.side_effect = RuntimeError("boom")

        service = ActivatedReaperService(
            activated_repo_manager=mgr,
            background_job_manager=background_job_manager,
            config_service=mock_config_service_30d,
        )

        result = service.run_reap_cycle()

        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert err["username"] == "grace"
        assert err["user_alias"] == "bad-repo"
        assert "boom" in err["error"]


# ---------------------------------------------------------------------------
# _parse_last_accessed: Python 3.9 Z-suffix compatibility
# ---------------------------------------------------------------------------


class TestParseLastAccessed:
    """_parse_last_accessed handles Z suffix and edge cases (Python 3.9 compat)."""

    def test_z_suffix_parses_as_utc(self):
        """ISO string with Z suffix is parsed as UTC-aware datetime."""
        from code_indexer.server.services.activated_reaper_service import (
            _parse_last_accessed,
        )

        result = _parse_last_accessed("2024-01-15T10:30:00Z")

        assert result is not None
        assert result.tzinfo is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_normal_iso_string_parses(self):
        """ISO string without Z suffix parses normally."""
        from code_indexer.server.services.activated_reaper_service import (
            _parse_last_accessed,
        )

        result = _parse_last_accessed("2024-06-20T08:00:00+00:00")

        assert result is not None
        assert result.tzinfo is not None

    def test_none_returns_none(self):
        """None input returns None."""
        from code_indexer.server.services.activated_reaper_service import (
            _parse_last_accessed,
        )

        assert _parse_last_accessed(None) is None

    def test_invalid_string_returns_none(self):
        """Unparseable string returns None instead of raising."""
        from code_indexer.server.services.activated_reaper_service import (
            _parse_last_accessed,
        )

        assert _parse_last_accessed("not-a-date") is None

    def test_naive_datetime_string_gets_utc_tzinfo(self):
        """Naive ISO string (no TZ) is assigned UTC tzinfo."""
        from code_indexer.server.services.activated_reaper_service import (
            _parse_last_accessed,
        )

        result = _parse_last_accessed("2024-03-10T12:00:00")

        assert result is not None
        assert result.tzinfo is not None
