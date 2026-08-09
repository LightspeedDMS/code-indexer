"""
Bug #1535: submit_job's "Job submitted without repo_alias" WARNING fires on
EVERY successful temporal query, because Story #1400 deliberately omits
repo_alias for the "temporal_query" operation_type (see
temporal_live_dispatch.py's `_submit` closure comment) -- BGM's
register_job_if_no_conflict per-(operation_type, repo_alias) uniqueness gate
is the wrong dedup tool for temporal queries, so repo_alias is never passed.

This is happy-path log noise, not a caller mistake: nothing is wrong, and the
WARNING channel that the mandatory post-E2E log-audit gate relies on to spot
real problems gets flooded proportionally to temporal query volume.

Fix: submit_job downgrades this specific, known-intentional omission
(operation_type == "temporal_query") to DEBUG. Every OTHER operation_type
submitted without repo_alias is unchanged -- still a genuine WARNING, since
for those callers a missing repo_alias usually does indicate a bug (the
original AC5 intent).
"""

import logging
import tempfile
import time
from pathlib import Path

import pytest

from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
)
from code_indexer.server.utils.config_manager import BackgroundJobsConfig

pytestmark = pytest.mark.slow

_COMPLETION_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.02


def _noop_worker():
    return {"status": "success"}


def _warning_messages(caplog) -> list:
    return [r.message for r in caplog.records if r.levelno >= logging.WARNING]


def _wait_completed(manager: BackgroundJobManager, job_id: str) -> None:
    deadline = time.monotonic() + _COMPLETION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = manager.get_job_status(job_id, username="u1")
        if status is not None and status.get("status") in ("completed", "failed"):
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"job {job_id} did not complete within {_COMPLETION_TIMEOUT_SECONDS}s"
    )


class _ManagerFixture:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.job_storage_path = Path(self.temp_dir) / "jobs.json"
        self.manager = None

    def teardown_method(self):
        if self.manager is not None:
            self.manager.shutdown()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_manager(self, **config_kwargs) -> BackgroundJobManager:
        config = BackgroundJobsConfig(**config_kwargs)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )
        return self.manager


class TestTemporalQueryRepoAliasOmissionIsNotWarning(_ManagerFixture):
    def test_temporal_query_without_repo_alias_does_not_warn(self, caplog):
        """RED/GREEN target: submitting a 'temporal_query' job with
        repo_alias=None (Story #1400's deliberate, documented omission)
        must NOT produce a WARNING-level log record."""
        manager = self._make_manager()

        with caplog.at_level(logging.DEBUG):
            job_id = manager.submit_job(
                "temporal_query",
                _noop_worker,
                submitter_username="u1",
                repo_alias=None,
                lane="temporal",
            )
        _wait_completed(manager, job_id)

        warning_messages = _warning_messages(caplog)
        assert not any("without repo_alias" in m for m in warning_messages), (
            f"Unexpected WARNING for happy-path temporal_query dispatch: {warning_messages}"
        )

    def test_temporal_query_without_repo_alias_still_logs_at_debug(self, caplog):
        """The information is not silently dropped -- it is demoted, not
        deleted, so a future operator debugging session can still see it."""
        manager = self._make_manager()

        with caplog.at_level(logging.DEBUG):
            job_id = manager.submit_job(
                "temporal_query",
                _noop_worker,
                submitter_username="u1",
                repo_alias=None,
                lane="temporal",
            )
        _wait_completed(manager, job_id)

        debug_messages = [
            r.message
            for r in caplog.records
            if r.levelno == logging.DEBUG and "without repo_alias" in r.message
        ]
        assert debug_messages, "Expected a DEBUG record documenting the omission"

    def test_other_operation_without_repo_alias_still_warns(self, caplog):
        """Regression guard: the demotion is scoped to 'temporal_query' only.
        Any other operation_type submitted without repo_alias keeps its
        original WARNING -- this is still a signal of a genuine caller bug
        for those job types."""
        manager = self._make_manager()

        with caplog.at_level(logging.DEBUG):
            job_id = manager.submit_job(
                "add_golden_repo",
                _noop_worker,
                submitter_username="u1",
                repo_alias=None,
            )
        _wait_completed(manager, job_id)

        warning_messages = _warning_messages(caplog)
        assert any("without repo_alias" in m for m in warning_messages), (
            "Expected the pre-existing WARNING to be preserved for non-temporal operations"
        )


class TestOtherIntentionalRepoAliasOmissionsAreNotWarning(_ManagerFixture):
    """Codex review of commit 650665a9 found the original fix only covered
    'temporal_query', but two OTHER operation_types are equally
    intentional, permanent repo_alias=None omissions:

    - "xray_search_batch" (xray_batch.py): "no per-repo dedup; concurrent
      batches allowed" -- an explicit design comment at the call site.
    - "query_analytics_export" (inline_admin_ops.py): a global,
      cross-repo export job with no single owning repo_alias.
    """

    def test_xray_search_batch_without_repo_alias_does_not_warn(self, caplog):
        manager = self._make_manager()

        with caplog.at_level(logging.DEBUG):
            job_id = manager.submit_job(
                "xray_search_batch",
                _noop_worker,
                submitter_username="u1",
                repo_alias=None,
            )
        _wait_completed(manager, job_id)

        warning_messages = _warning_messages(caplog)
        assert not any("without repo_alias" in m for m in warning_messages), (
            f"Unexpected WARNING for happy-path xray_search_batch dispatch: {warning_messages}"
        )

    def test_query_analytics_export_without_repo_alias_does_not_warn(self, caplog):
        manager = self._make_manager()

        with caplog.at_level(logging.DEBUG):
            job_id = manager.submit_job(
                "query_analytics_export",
                _noop_worker,
                submitter_username="u1",
                repo_alias=None,
            )
        _wait_completed(manager, job_id)

        warning_messages = _warning_messages(caplog)
        assert not any("without repo_alias" in m for m in warning_messages), (
            f"Unexpected WARNING for happy-path query_analytics_export dispatch: {warning_messages}"
        )


class TestDiscoveryFamilyRepoAliasOmissionIsNotWarning(_ManagerFixture):
    """The "{platform}_discovery" family (web/routes.py's discovery_start)
    is dynamically constructed per platform -- CLAUDE.md's own
    "Auto-Discovery Background Job Pattern" documents
    "repo_alias=None bypasses the DB gate" for this shape. The fix must be
    a pattern/suffix match, not a fixed set of known platform strings."""

    def test_platform_discovery_family_without_repo_alias_does_not_warn(self, caplog):
        manager = self._make_manager()

        for op_type in (
            "github_discovery",
            "gitlab_discovery",
            "bitbucket_discovery",
            "future_platform_discovery",
        ):
            with caplog.at_level(logging.DEBUG):
                job_id = manager.submit_job(
                    op_type,
                    _noop_worker,
                    submitter_username="u1",
                    repo_alias=None,
                )
            _wait_completed(manager, job_id)

            warning_messages = _warning_messages(caplog)
            assert not any("without repo_alias" in m for m in warning_messages), (
                f"Unexpected WARNING for happy-path {op_type} dispatch: {warning_messages}"
            )
            caplog.clear()

    def test_unlisted_operation_still_warns(self, caplog):
        """Regression guard: an operation_type that is NOT one of the
        documented intentional-omission shapes still gets the original
        WARNING -- the expanded fix must not become an unbounded escape
        hatch."""
        manager = self._make_manager()

        with caplog.at_level(logging.DEBUG):
            job_id = manager.submit_job(
                "some_unrelated_operation",
                _noop_worker,
                submitter_username="u1",
                repo_alias=None,
            )
        _wait_completed(manager, job_id)

        warning_messages = _warning_messages(caplog)
        assert any("without repo_alias" in m for m in warning_messages), (
            "Expected the pre-existing WARNING to be preserved for a genuinely "
            "unlisted operation_type"
        )
