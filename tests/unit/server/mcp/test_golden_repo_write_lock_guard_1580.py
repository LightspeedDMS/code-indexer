"""Regression tests for golden_repo_write_lock_guard (Bug #1580 follow-up,
adversarial-review round 2).

Two HIGH-severity defects were found by adversarial review of the
original fix:

1. The guard acquired with WriteLockManager's DEFAULT 1-hour TTL. This
   project's indexing-path invariant documents NO job/subprocess timeout
   -- a large repo can legitimately take hours -- so a 1-hour TTL
   silently expires mid-run, defeating the lock. Fixed with a 24-hour
   TTL, matching temporal_legacy_migration/locking.py's
   TEMPORAL_LEGACY_MIGRATION_LOCK_TTL_SECONDS convention.

2. The original fix for the empty-alias crash made a blank alias
   silently proceed unlocked WHENEVER a scheduler was wired.
   _resolve_provider_job_repo_path() can genuinely leave repo_alias
   empty for a real, non-versioned repo path, so this is a wiring bug,
   not a no-op. Fixed: no scheduler wired at all stays a legitimate
   silent no-op; a scheduler wired + blank/whitespace alias now raises.

A MEDIUM finding (release() failure not surfaced) is covered in the same
third test alongside the TTL assertion, to keep this file within a
single small test class.
"""

import logging
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.mcp.handlers._utils import (
    GOLDEN_REPO_WRITE_LOCK_GUARD_TTL_SECONDS,
    golden_repo_write_lock_guard,
)

_PATCH_TARGET = "code_indexer.server.mcp.handlers._utils._get_app_refresh_scheduler"


@contextmanager
def _scheduler_wired(mock_scheduler):
    with patch(_PATCH_TARGET, return_value=mock_scheduler):
        yield mock_scheduler


class TestGoldenRepoWriteLockGuardBug1580Round2:
    @pytest.mark.parametrize("blank_alias", ["", "   "])
    def test_blank_alias_with_scheduler_present_raises(self, blank_alias):
        """A wired scheduler + blank/whitespace alias must raise loudly
        instead of proceeding unlocked, and must never call
        acquire_write_lock at all."""
        mock_scheduler = MagicMock()

        with _scheduler_wired(mock_scheduler):
            with pytest.raises(ValueError):
                with golden_repo_write_lock_guard(blank_alias, owner_name="test_owner"):
                    pass

        mock_scheduler.acquire_write_lock.assert_not_called()
        mock_scheduler.release_write_lock.assert_not_called()

    def test_no_scheduler_wired_blank_alias_is_legitimate_noop(self):
        """No RefreshScheduler wired at all (solo/CLI mode) is a genuine
        no-op regardless of alias -- must yield True, never raise."""
        with patch(_PATCH_TARGET, return_value=None):
            with golden_repo_write_lock_guard("", owner_name="test_owner") as held:
                assert held is True

    def test_ttl_is_24_hours_and_failed_release_logs_error(self, caplog):
        """Combines the two remaining findings in one test: (a) acquire
        must request an explicit 24h TTL, not the 1-hour library
        default; (b) a release() failure must surface as an ERROR log
        naming the alias and owner, not be swallowed silently."""
        assert GOLDEN_REPO_WRITE_LOCK_GUARD_TTL_SECONDS == 24 * 60 * 60

        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True
        mock_scheduler.release_write_lock.return_value = False

        with _scheduler_wired(mock_scheduler):
            with caplog.at_level(
                logging.ERROR, logger="code_indexer.server.mcp.handlers._utils"
            ):
                with golden_repo_write_lock_guard("my-repo", owner_name="test_owner"):
                    pass

        _, acquire_kwargs = mock_scheduler.acquire_write_lock.call_args
        assert acquire_kwargs.get("ttl_seconds") == 24 * 60 * 60, (
            f"expected ttl_seconds=86400, got call kwargs: {acquire_kwargs}"
        )

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 1, (
            f"expected exactly one ERROR record on release failure, got: "
            f"{[r.message for r in caplog.records]}"
        )
        message = error_records[0].getMessage()
        assert "my-repo" in message and "test_owner" in message
