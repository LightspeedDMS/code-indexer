"""
Unit tests for v10.4.14 stub-healing in DescriptionRefreshScheduler.

Existing on-disk stub descriptions (artifact of v10.4.9 wipe-and-replace bug
or pre-v10.4.13 README/static-regex fallback) were NOT healed by the periodic
refresh path: _get_refresh_prompt() bailed out with WARN on missing last_analyzed
or terse body, never attempting full regeneration.

v10.4.14 adds stub-detection logic inside _get_refresh_prompt: when a stub is
detected (missing last_analyzed OR body length < _STUB_BODY_CHAR_THRESHOLD), it
dispatches a FULL re-analysis via BackgroundJobManager (non-blocking), then
always returns None (signaling "heal dispatched, skip regular refresh to avoid
race condition where regular refresh overwrites the healed description").

Stub-detection criteria (logical OR):
  (a) desc_data.get("last_analyzed") is None or empty string
  (b) body length (chars of content after YAML frontmatter) < _STUB_BODY_CHAR_THRESHOLD (800)

Anti-mock strategy:
  - Real scheduler is constructed with injectable backends (MagicMock).
  - Real _meta_dir with real .md files so _read_existing_description runs for real.
  - background_job_manager injected as MagicMock so dispatch contract is testable.
  - Only module-level boundary functions are patched for WORKER tests:
      get_claude_cli_manager  (OS/singleton boundary)
      _generate_repo_description  (Claude CLI boundary - external process)
      atomic_write_description  (filesystem write boundary)
  - No internal scheduler methods are patched.
  - MagicMock(spec=ClaudeCliManager) satisfies isinstance() guards in production code.

Test inventory:
  TestStubThresholdConstant:
    test_threshold_default_is_800

  TestStubDetectionCriteria:
    test_stub_detection_triggers_bjm_dispatch  (parametrized 3 cases)
    test_well_formed_description_no_stub_heal_invoked

  TestStubHealLogging:
    test_dispatch_emits_info_log_with_alias_and_job_id

  TestBackgroundJobDispatchContract:
    test_dispatch_calls_submit_job_with_description_stub_heal_operation_type
    test_dispatch_passes_repo_alias_kwarg_for_dedup
    test_dispatch_passes_system_username
    test_dispatch_passes_worker_function_and_args

  TestDuplicateJobErrorHandling:
    test_duplicate_job_error_treated_as_in_flight_does_not_raise
    test_duplicate_job_error_with_last_analyzed_returns_none

  TestNoBackgroundJobManagerWired:
    test_warning_logged_when_bjm_is_none
    test_falls_through_to_incremental_when_bjm_none_and_last_analyzed_present
    test_returns_none_when_bjm_none_and_no_last_analyzed

  TestNonBlockingDispatch:
    test_get_refresh_prompt_returns_quickly_even_if_worker_would_be_slow

  TestStubHealWorker:
    test_worker_returns_success_dict_when_heal_succeeds
    test_worker_returns_preconditions_dict_when_heal_returns_false
    test_worker_raises_runtime_error_when_heal_returns_none

  TestFullRegenInvocation (worker-direct):
    test_worker_calls_generate_repo_description_with_real_cli_manager
    test_worker_uses_alias_form_filename
    test_worker_writes_returned_md_content

  TestFullRegenFailureModes (worker-direct):
    test_cli_not_available_falls_through_to_incremental
    test_repo_url_lookup_failure_logs_and_returns_preconditions_dict
    test_generate_repo_description_runtime_error_raises_runtime_error
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import Iterator, Optional, cast
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_PAST_TIME = "2000-01-01T00:00:00+00:00"
_LONG_BODY = "x" * 900  # > 800 chars - well-formed
_SHORT_BODY = "x" * 100  # < 800 chars - stub
_DEFAULT_REPO_URL = "git@github.com:org/repo.git"
_SENTINEL_MD = "---\nname: x\nurl: y\nlast_analyzed: now\n---\nSENTINEL_BODY"

# Module paths for patching boundary functions.
# The heal function uses lazy imports, so names are looked up in their home modules.
_SCHEDULER_MODULE = "code_indexer.server.services.description_refresh_scheduler"
_CLI_MANAGER_MODULE = "code_indexer.server.services.claude_cli_manager"
_HOOK_MODULE = "code_indexer.global_repos.meta_description_hook"


# ---------------------------------------------------------------------------
# Shared builder helpers
# ---------------------------------------------------------------------------


def _make_full_content(
    alias: str,
    url: str,
    body: str,
    last_analyzed: Optional[str] = _PAST_TIME,
) -> str:
    """Return a synthetic .md file string with YAML frontmatter and *body*."""
    if last_analyzed:
        fm = f"---\nname: {alias}\nurl: {url}\nlast_analyzed: {last_analyzed}\n---\n"
    else:
        fm = f"---\nname: {alias}\nurl: {url}\n---\n"
    return fm + body


def _make_scheduler_with_meta(
    tmp_path: Path,
    alias: str,
    file_content: str,
    repo_url: str = _DEFAULT_REPO_URL,
):
    """
    Build a DescriptionRefreshScheduler with a real meta_dir containing one .md file.

    golden_backend.get_repo returns repo_url + clone_path so the stub healer
    can resolve the repo URL from the database without additional external calls.
    Uses a real _meta_dir so _read_existing_description works without mocking.

    Injects a MagicMock background_job_manager whose submit_job returns
    "fake-job-id-123" by default.  Tests that need different behavior
    (DuplicateJobError, exception) override per-test.

    Returns (scheduler, clone_dir).
    """
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        ServerConfig,
    )

    meta_dir = tmp_path / "cidx-meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / f"{alias}.md").write_text(file_content, encoding="utf-8")

    # _validate_refresh_inputs checks that clone_path resolves to an existing dir
    clone_dir = tmp_path / "repos" / alias
    clone_dir.mkdir(parents=True, exist_ok=True)

    tracking_backend = MagicMock(name="tracking_backend")
    golden_backend = MagicMock(name="golden_backend")
    golden_backend.get_repo.return_value = {
        "clone_path": str(clone_dir),
        "repo_url": repo_url,
    }

    config = ServerConfig(server_dir=str(tmp_path))
    config.claude_integration_config = ClaudeIntegrationConfig()
    config.claude_integration_config.description_refresh_enabled = True
    config.claude_integration_config.description_refresh_interval_hours = 24

    config_manager = MagicMock(name="config_manager")
    config_manager.load_config.return_value = config

    # Default BJM mock: submit_job returns a fake job_id
    mock_bjm = MagicMock(name="background_job_manager")
    mock_bjm.submit_job.return_value = "fake-job-id-123"

    scheduler = DescriptionRefreshScheduler(
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
        config_manager=config_manager,
        claude_cli_manager=MagicMock(name="claude_cli_manager"),
        meta_dir=meta_dir,
        background_job_manager=mock_bjm,
    )
    return scheduler, clone_dir


def _get_bjm(scheduler) -> MagicMock:
    """Return scheduler._background_job_manager for readable test assertions."""
    return cast(MagicMock, scheduler._background_job_manager)


def _make_cli_mock():
    """Return MagicMock(spec=ClaudeCliManager) with check_cli_available=True."""
    from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

    mock_cli = MagicMock(spec=ClaudeCliManager)
    mock_cli.check_cli_available.return_value = True
    return mock_cli


def _make_stub_scheduler(tmp_path: Path, alias: str):
    """
    Build a (scheduler, clone_dir) pair with a stub description (short body,
    last_analyzed present) ready for stub-heal tests.  Uses _DEFAULT_REPO_URL.
    """
    content = _make_full_content(alias, _DEFAULT_REPO_URL, _SHORT_BODY, _PAST_TIME)
    return _make_scheduler_with_meta(tmp_path, alias, content)


def _extract_error_messages(caplog) -> list:
    """Return the .message string for every ERROR-or-above record in caplog."""
    return [r.message for r in caplog.records if r.levelno >= logging.ERROR]


@contextlib.contextmanager
def _with_stub_heal_patches(
    cli_mock,
    gen_return_value: str = _SENTINEL_MD,
) -> Iterator[tuple]:
    """
    Context manager that patches the three module-level boundary functions used
    by the stub-healing path in _heal_stub_description_worker.

    The heal function uses lazy imports so names are resolved in their home modules:
      - get_claude_cli_manager  in _CLI_MANAGER_MODULE  -> returns cli_mock
      - _generate_repo_description  in _HOOK_MODULE  -> returns gen_return_value
      - atomic_write_description  in _HOOK_MODULE  -> no-op, call-args inspectable

    Yields (mock_gen, mock_write) so callers can assert call details.
    """
    with (
        patch(f"{_CLI_MANAGER_MODULE}.get_claude_cli_manager", return_value=cli_mock),
        patch(
            f"{_HOOK_MODULE}._generate_repo_description",
            return_value=gen_return_value,
        ) as mock_gen,
        patch(
            f"{_HOOK_MODULE}.atomic_write_description",
        ) as mock_write,
    ):
        yield mock_gen, mock_write


# ---------------------------------------------------------------------------
# TestStubThresholdConstant
# ---------------------------------------------------------------------------


class TestStubThresholdConstant:
    def test_threshold_default_is_800(self) -> None:
        """_STUB_BODY_CHAR_THRESHOLD must be 800 (per v10.4.14 spec)."""
        from code_indexer.server.services.description_refresh_scheduler import (
            _STUB_BODY_CHAR_THRESHOLD,
        )

        assert _STUB_BODY_CHAR_THRESHOLD == 800


# ---------------------------------------------------------------------------
# TestStubDetectionCriteria
# ---------------------------------------------------------------------------


class TestStubDetectionCriteria:
    @pytest.mark.parametrize(
        "body,last_analyzed,scenario",
        [
            (_LONG_BODY, None, "missing_last_analyzed"),
            (_SHORT_BODY, _PAST_TIME, "short_body_only"),
            (_SHORT_BODY, None, "both_missing"),
        ],
    )
    def test_stub_detection_triggers_bjm_dispatch(
        self,
        tmp_path: Path,
        body: str,
        last_analyzed: Optional[str],
        scenario: str,
    ) -> None:
        """
        Any stub-detection criterion triggers BJM dispatch via submit_job:
          - missing_last_analyzed: body > 800 but no last_analyzed field
          - short_body_only: last_analyzed present but body < 800 chars
          - both_missing: both criteria fail simultaneously

        In all cases submit_job must be called once with
        operation_type="description_stub_heal" and repo_alias=alias.

        For "short_body_only" (last_analyzed present) _get_refresh_prompt
        returns a non-None incremental prompt (heal in flight + interim refresh).
        For "missing_last_analyzed" and "both_missing" (no last_analyzed)
        _get_refresh_prompt returns None (nothing to incrementally refresh).
        """
        alias = f"repo-{scenario}"
        content = _make_full_content(alias, _DEFAULT_REPO_URL, body, last_analyzed)
        scheduler, clone_dir = _make_scheduler_with_meta(tmp_path, alias, content)
        bjm = _get_bjm(scheduler)

        result = scheduler._get_refresh_prompt(alias, str(clone_dir))

        # submit_job must have been called with correct operation_type
        bjm.submit_job.assert_called_once()
        call_args = bjm.submit_job.call_args
        assert call_args[0][0] == "description_stub_heal", (
            f"[{scenario}] expected operation_type='description_stub_heal', "
            f"got {call_args[0][0]!r}"
        )
        assert call_args.kwargs.get("repo_alias") == alias, (
            f"[{scenario}] expected repo_alias kwarg={alias!r}, "
            f"got {call_args.kwargs.get('repo_alias')!r}"
        )

        # All stub cases: heal dispatched -> return None to avoid race condition
        # where regular refresh overwrites the healed description.
        assert result is None, (
            f"[{scenario}] expected None (heal in flight, skip regular refresh), "
            f"got: {result!r}"
        )

    def test_well_formed_description_no_stub_heal_invoked(self, tmp_path: Path) -> None:
        """
        Body length > 800 AND last_analyzed present -> NOT a stub ->
        submit_job must NOT be called.

        _stage_and_build_prompt calls real RepoAnalyzer which may raise on a
        synthetic repo; we wrap in try/except so the assertion always executes.
        """
        alias = "repo-well-formed"
        content = _make_full_content(
            alias, _DEFAULT_REPO_URL, _LONG_BODY, last_analyzed=_PAST_TIME
        )
        scheduler, clone_dir = _make_scheduler_with_meta(tmp_path, alias, content)
        bjm = _get_bjm(scheduler)

        try:
            scheduler._get_refresh_prompt(alias, str(clone_dir))
        except Exception:  # noqa: BLE001  # intentional discard: see rationale below
            # EXPLICIT DISCARD: _stage_and_build_prompt calls RepoAnalyzer on disk.
            # A synthetic tmp_path has no git history so RepoAnalyzer may raise.
            # That downstream failure is irrelevant to this test: the assertion is
            # ONLY that the stub-heal path was NOT entered (submit_job not called).
            pass

        bjm.submit_job.assert_not_called()


# ---------------------------------------------------------------------------
# TestStubHealLogging
# ---------------------------------------------------------------------------


class TestStubHealLogging:
    """Verify informational logging emitted during a successful stub-heal dispatch."""

    def test_dispatch_emits_info_log_with_alias_and_job_id(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Successful stub-heal dispatch must emit an INFO log containing
        DESC-REFRESH-STUB-HEAL-014, the repo alias, and the job_id returned
        by submit_job.
        """
        alias = "repo-log-check"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        bjm = _get_bjm(scheduler)
        bjm.submit_job.return_value = "fake-job-id-456"

        with caplog.at_level(logging.INFO):
            scheduler._get_refresh_prompt(alias, str(clone_dir))

        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "DESC-REFRESH-STUB-HEAL-014" in msg
            and "fake-job-id-456" in msg
            and alias in msg
            for msg in info_messages
        ), (
            f"Expected INFO log with DESC-REFRESH-STUB-HEAL-014, job_id "
            f"'fake-job-id-456', and alias '{alias}'; "
            f"got INFO messages: {info_messages}"
        )


# ---------------------------------------------------------------------------
# TestBackgroundJobDispatchContract
# ---------------------------------------------------------------------------


class TestBackgroundJobDispatchContract:
    """Verify the exact call signature passed to BackgroundJobManager.submit_job."""

    def test_dispatch_calls_submit_job_with_description_stub_heal_operation_type(
        self, tmp_path: Path
    ) -> None:
        """First positional arg to submit_job must be 'description_stub_heal'."""
        alias = "repo-contract-optype"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        bjm = _get_bjm(scheduler)

        scheduler._get_refresh_prompt(alias, str(clone_dir))

        bjm.submit_job.assert_called_once()
        assert bjm.submit_job.call_args[0][0] == "description_stub_heal"

    def test_dispatch_passes_repo_alias_kwarg_for_dedup(self, tmp_path: Path) -> None:
        """submit_job must receive repo_alias as keyword arg (used by BJM DuplicateJobError gate)."""
        alias = "repo-contract-alias"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        bjm = _get_bjm(scheduler)

        scheduler._get_refresh_prompt(alias, str(clone_dir))

        bjm.submit_job.assert_called_once()
        assert bjm.submit_job.call_args.kwargs.get("repo_alias") == alias

    def test_dispatch_passes_system_username(self, tmp_path: Path) -> None:
        """submit_job must receive submitter_username='system' as keyword arg."""
        alias = "repo-contract-username"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        bjm = _get_bjm(scheduler)

        scheduler._get_refresh_prompt(alias, str(clone_dir))

        bjm.submit_job.assert_called_once()
        assert bjm.submit_job.call_args.kwargs.get("submitter_username") == "system"

    def test_dispatch_passes_worker_function_and_args(self, tmp_path: Path) -> None:
        """
        submit_job positional arg[1] must be the bound _heal_stub_description_worker
        method, and positional args[2,3] must be alias and str(repo_path_obj).
        """
        alias = "repo-contract-worker"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        bjm = _get_bjm(scheduler)

        scheduler._get_refresh_prompt(alias, str(clone_dir))

        bjm.submit_job.assert_called_once()
        call_args = bjm.submit_job.call_args
        # arg[1]: worker function
        assert call_args[0][1] == scheduler._heal_stub_description_worker, (
            "Second positional arg must be scheduler._heal_stub_description_worker"
        )
        # arg[2]: repo_alias string
        assert call_args[0][2] == alias
        # arg[3]: repo_path_str — must match clone_dir (resolved via _validate_refresh_inputs)
        assert call_args[0][3] == str(clone_dir.resolve())


# ---------------------------------------------------------------------------
# TestDuplicateJobErrorHandling
# ---------------------------------------------------------------------------


class TestDuplicateJobErrorHandling:
    """Verify DuplicateJobError is treated as 'already in flight' (not an error)."""

    def test_duplicate_job_error_treated_as_in_flight_does_not_raise(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        When submit_job raises DuplicateJobError, _get_refresh_prompt must NOT raise.
        INFO log DESC-REFRESH-STUB-HEAL-011 and existing_job_id must appear in caplog.
        Alias must be added to _stub_heal_no_quarantine_aliases (heal in flight).
        """
        from code_indexer.server.repositories.background_jobs import DuplicateJobError

        alias = "repo-dup-no-last-analyzed"
        content = _make_full_content(
            alias, _DEFAULT_REPO_URL, _SHORT_BODY, last_analyzed=None
        )
        scheduler, clone_dir = _make_scheduler_with_meta(tmp_path, alias, content)
        bjm = _get_bjm(scheduler)
        bjm.submit_job.side_effect = DuplicateJobError(
            operation_type="description_stub_heal",
            repo_alias=alias,
            existing_job_id="prior-job-789",
        )

        with caplog.at_level(logging.INFO):
            result = scheduler._get_refresh_prompt(alias, str(clone_dir))

        # Must not raise; DuplicateJobError => dispatched=True
        # No last_analyzed => returns None (nothing to incrementally refresh)
        assert result is None

        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "DESC-REFRESH-STUB-HEAL-011" in msg and "prior-job-789" in msg
            for msg in info_messages
        ), (
            f"Expected INFO log with DESC-REFRESH-STUB-HEAL-011 and 'prior-job-789'; "
            f"got INFO messages: {info_messages}"
        )
        assert alias in scheduler._stub_heal_no_quarantine_aliases, (
            "Alias must be in _stub_heal_no_quarantine_aliases when DuplicateJobError fires"
        )

    def test_duplicate_job_error_with_last_analyzed_returns_none(
        self, tmp_path: Path
    ) -> None:
        """
        DuplicateJobError + last_analyzed present: dispatched=True + has_last_analyzed=True
        => _get_refresh_prompt returns None to avoid race condition where regular
        refresh overwrites the healed description (v10.4.14 race condition fix).
        """
        from code_indexer.server.repositories.background_jobs import DuplicateJobError

        alias = "repo-dup-with-last-analyzed"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        bjm = _get_bjm(scheduler)
        bjm.submit_job.side_effect = DuplicateJobError(
            operation_type="description_stub_heal",
            repo_alias=alias,
            existing_job_id="prior-job-999",
        )

        result = scheduler._get_refresh_prompt(alias, str(clone_dir))

        # dispatched=True (DuplicateJobError) => None regardless of last_analyzed
        # to avoid race where regular refresh overwrites healed description
        assert result is None, (
            f"Expected None (heal in flight, skip regular refresh), got: {result!r}"
        )


# ---------------------------------------------------------------------------
# TestNoBackgroundJobManagerWired
# ---------------------------------------------------------------------------


class TestNoBackgroundJobManagerWired:
    """Verify graceful degradation when BackgroundJobManager is not wired."""

    def test_warning_logged_when_bjm_is_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        When _background_job_manager is None, WARNING HEAL-013 must be emitted
        and no HEAL-014 INFO log must appear.
        """
        alias = "repo-bjm-none-warn"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        scheduler._background_job_manager = None

        with caplog.at_level(logging.WARNING):
            scheduler._get_refresh_prompt(alias, str(clone_dir))

        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("DESC-REFRESH-STUB-HEAL-013" in msg for msg in warning_messages), (
            f"Expected WARNING DESC-REFRESH-STUB-HEAL-013; got: {warning_messages}"
        )
        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert not any("DESC-REFRESH-STUB-HEAL-014" in msg for msg in info_messages), (
            "HEAL-014 must NOT appear when BJM is None"
        )

    def test_falls_through_to_incremental_when_bjm_none_and_last_analyzed_present(
        self, tmp_path: Path
    ) -> None:
        """
        BJM None + last_analyzed present => dispatched=False + has_last_analyzed=True
        => fall through to incremental refresh (non-None prompt).
        Alias must NOT be in _stub_heal_no_quarantine_aliases (no heal in flight;
        if incremental fails, normal quarantine should apply so operators see misconfig).
        """
        alias = "repo-bjm-none-incremental"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        scheduler._background_job_manager = None

        result = scheduler._get_refresh_prompt(alias, str(clone_dir))

        assert isinstance(result, str), (
            f"Expected incremental prompt string, got: {result!r}"
        )
        assert alias not in scheduler._stub_heal_no_quarantine_aliases, (
            "Alias must NOT be in no-quarantine set when BJM is None and last_analyzed present"
        )

    def test_returns_none_when_bjm_none_and_no_last_analyzed(
        self, tmp_path: Path
    ) -> None:
        """
        BJM None + no last_analyzed => dispatched=False + has_last_analyzed=False
        => returns None. Alias IS added to _stub_heal_no_quarantine_aliases so
        quarantine counter is not incremented.
        """
        alias = "repo-bjm-none-no-la"
        content = _make_full_content(
            alias, _DEFAULT_REPO_URL, _SHORT_BODY, last_analyzed=None
        )
        scheduler, clone_dir = _make_scheduler_with_meta(tmp_path, alias, content)
        scheduler._background_job_manager = None

        result = scheduler._get_refresh_prompt(alias, str(clone_dir))

        assert result is None
        assert alias in scheduler._stub_heal_no_quarantine_aliases, (
            "Alias must be in no-quarantine set when BJM None and no last_analyzed"
        )


# ---------------------------------------------------------------------------
# TestNonBlockingDispatch
# ---------------------------------------------------------------------------


class TestNonBlockingDispatch:
    """Verify _get_refresh_prompt returns quickly (dispatch is non-blocking)."""

    def test_get_refresh_prompt_returns_quickly_even_if_worker_would_be_slow(
        self, tmp_path: Path
    ) -> None:
        """
        submit_job must return immediately without invoking the worker.
        _get_refresh_prompt must complete within 0.5 seconds even if the worker
        would have taken minutes.
        """
        alias = "repo-nonblocking"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        bjm = _get_bjm(scheduler)

        # submit_job records call and returns immediately — does NOT call the worker
        submitted_calls: list = []

        def fake_submit(op_type, worker, *args, **kwargs):
            submitted_calls.append((op_type, args, kwargs))
            return "job-nonblocking-123"

        bjm.submit_job.side_effect = fake_submit

        start = time.monotonic()
        scheduler._get_refresh_prompt(alias, str(clone_dir))
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, (
            f"_get_refresh_prompt took {elapsed:.3f}s; expected < 0.5s "
            f"(dispatch must be non-blocking)"
        )
        assert len(submitted_calls) == 1, "submit_job must have been called once"


# ---------------------------------------------------------------------------
# TestStubHealWorker
# ---------------------------------------------------------------------------


class TestStubHealWorker:
    """Verify _heal_stub_description_worker adapts tri-valued return to BJM contract."""

    def test_worker_returns_success_dict_when_heal_succeeds(
        self, tmp_path: Path
    ) -> None:
        """
        When _heal_stub_description returns True, worker must return dict
        with status='success', operation_type='description_stub_heal', repo_alias=alias.
        """
        alias = "repo-worker-success"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)

        with patch.object(scheduler, "_heal_stub_description", return_value=True):
            result = scheduler._heal_stub_description_worker(alias, str(clone_dir))

        assert result == {
            "status": "success",
            "operation_type": "description_stub_heal",
            "repo_alias": alias,
        }

    def test_worker_returns_preconditions_dict_when_heal_returns_false(
        self, tmp_path: Path
    ) -> None:
        """
        When _heal_stub_description returns False, worker must return dict
        with status='preconditions_unmet'.
        """
        alias = "repo-worker-precond"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)

        with patch.object(scheduler, "_heal_stub_description", return_value=False):
            result = scheduler._heal_stub_description_worker(alias, str(clone_dir))

        assert result == {
            "status": "preconditions_unmet",
            "operation_type": "description_stub_heal",
            "repo_alias": alias,
        }

    def test_worker_raises_runtime_error_when_heal_returns_none(
        self, tmp_path: Path
    ) -> None:
        """
        When _heal_stub_description returns None (runtime failure), worker must
        raise RuntimeError (so BJM marks job FAILED for dashboard visibility).
        """
        alias = "repo-worker-runtime-fail"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)

        with patch.object(scheduler, "_heal_stub_description", return_value=None):
            with pytest.raises(RuntimeError, match="runtime failure"):
                scheduler._heal_stub_description_worker(alias, str(clone_dir))


# ---------------------------------------------------------------------------
# TestFullRegenInvocation  (worker-direct tests)
# ---------------------------------------------------------------------------


class TestFullRegenInvocation:
    """
    Verify _heal_stub_description_worker dispatches correctly to external boundaries.
    These tests call the WORKER directly (not via _get_refresh_prompt) because
    _generate_repo_description and atomic_write_description are invoked ASYNCHRONOUSLY
    inside the worker — not inline in _get_refresh_prompt.
    """

    def test_worker_calls_generate_repo_description_with_real_cli_manager(
        self, tmp_path: Path
    ) -> None:
        """
        _generate_repo_description must be called once, and its 4th positional arg
        (cli_manager) must be the SAME object returned by get_claude_cli_manager.
        """
        alias = "repo-regen-cli"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        cli_mock = _make_cli_mock()
        with _with_stub_heal_patches(cli_mock) as (mock_gen, _):
            scheduler._heal_stub_description_worker(alias, str(clone_dir))

        mock_gen.assert_called_once()
        assert mock_gen.call_args[0][3] is cli_mock, (
            "4th positional arg to _generate_repo_description must be the "
            "ClaudeCliManager returned by get_claude_cli_manager"
        )

    def test_worker_writes_to_scheduler_read_path_filename(
        self, tmp_path: Path
    ) -> None:
        """
        atomic_write_description must be called with target ending in
        '{alias}.md' (bare form) — the same path _read_existing_description
        reads from. If heal writes a different filename (e.g. '-global.md'
        suffix), the scheduler's next tick reads the OLD file, still detects
        the stub, and re-dispatches the heal job in an infinite loop.
        """
        alias = "my-repo"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        with _with_stub_heal_patches(_make_cli_mock()) as (_, mock_write):
            scheduler._heal_stub_description_worker(alias, str(clone_dir))

        mock_write.assert_called_once()
        target_path = mock_write.call_args[0][0]
        assert str(target_path).endswith(f"{alias}.md"), (
            f"atomic_write_description target must end with '{alias}.md' "
            f"(bare form, matching _read_existing_description), got: {target_path}"
        )
        assert not str(target_path).endswith(f"{alias}-global.md"), (
            f"target must NOT end with '-global.md' suffix — that filename is "
            f"NOT what _read_existing_description reads, so heal would be "
            f"invisible to the next tick. got: {target_path}"
        )

    def test_worker_writes_returned_md_content(self, tmp_path: Path) -> None:
        """
        atomic_write_description must receive the exact string returned by
        _generate_repo_description (no transformation).
        """
        alias = "repo-content-check"
        sentinel_content = "SENTINEL_MD_CONTENT_12345"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        with _with_stub_heal_patches(
            _make_cli_mock(), gen_return_value=sentinel_content
        ) as (_, mock_write):
            scheduler._heal_stub_description_worker(alias, str(clone_dir))

        mock_write.assert_called_once()
        assert mock_write.call_args[0][1] == sentinel_content, (
            f"atomic_write_description must receive exact content from "
            f"_generate_repo_description; got: {mock_write.call_args[0][1]!r}"
        )


# ---------------------------------------------------------------------------
# TestFullRegenFailureModes  (worker-direct tests)
# ---------------------------------------------------------------------------


class TestFullRegenFailureModes:
    """
    Messi Rule #13 anti-silent-failure: error paths in _heal_stub_description_worker.
    Tests call the WORKER directly because failure modes (cli_manager absent,
    runtime error, repo_url lookup failure) are worker concerns, not dispatch concerns.
    """

    @pytest.mark.parametrize(
        "cli_available,scenario",
        [
            (None, "cli_manager_is_none"),
            (False, "check_cli_available_false"),
        ],
    )
    def test_cli_not_available_falls_through_to_incremental(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        cli_available: Optional[bool],
        scenario: str,
    ) -> None:
        """
        When the CLI manager is absent (None) or reports CLI unavailable (False),
        _heal_stub_description returns False (preconditions unmet).
        Worker returns dict with status='preconditions_unmet'.
        WARNING DESC-REFRESH-STUB-HEAL-004 is emitted by _heal_stub_description.
        """
        alias = f"repo-{scenario}"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)

        from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

        if cli_available is None:
            cli_return = None
        else:
            cli_return = MagicMock(spec=ClaudeCliManager)
            cli_return.check_cli_available.return_value = cli_available

        with (
            patch(
                f"{_CLI_MANAGER_MODULE}.get_claude_cli_manager",
                return_value=cli_return,
            ),
            patch(f"{_HOOK_MODULE}._generate_repo_description") as mock_gen,
            patch(f"{_HOOK_MODULE}.atomic_write_description") as mock_write,
            caplog.at_level(logging.WARNING),
        ):
            result = scheduler._heal_stub_description_worker(alias, str(clone_dir))

        assert result.get("status") == "preconditions_unmet", (
            f"[{scenario}] expected status='preconditions_unmet', got: {result!r}"
        )
        mock_gen.assert_not_called()
        mock_write.assert_not_called()
        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("DESC-REFRESH-STUB-HEAL-004" in msg for msg in warning_messages), (
            f"[{scenario}] expected DESC-REFRESH-STUB-HEAL-004 at WARNING level; "
            f"got warnings: {warning_messages}"
        )

    def test_repo_url_lookup_failure_logs_and_returns_preconditions_dict(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        golden_backend.get_repo raises (repo_url lookup failure) ->
        _heal_stub_description returns False -> worker returns status='preconditions_unmet'.
        Both _generate_repo_description and atomic_write_description suppressed.
        ERROR DESC-REFRESH-STUB-HEAL-006 emitted.
        """
        alias = "repo-url-fails"
        content = _make_full_content(
            alias, _DEFAULT_REPO_URL, _SHORT_BODY, last_analyzed=None
        )
        scheduler, clone_dir = _make_scheduler_with_meta(tmp_path, alias, content)
        scheduler._golden_backend.get_repo.side_effect = RuntimeError(
            "DB connection lost"
        )
        with (
            patch(
                f"{_CLI_MANAGER_MODULE}.get_claude_cli_manager",
                return_value=_make_cli_mock(),
            ),
            patch(f"{_HOOK_MODULE}._generate_repo_description") as mock_gen,
            patch(f"{_HOOK_MODULE}.atomic_write_description") as mock_write,
            caplog.at_level(logging.ERROR),
        ):
            result = scheduler._heal_stub_description_worker(alias, str(clone_dir))

        assert result.get("status") == "preconditions_unmet"
        mock_gen.assert_not_called()
        mock_write.assert_not_called()
        assert any(
            "DESC-REFRESH-STUB-HEAL-006" in msg
            for msg in _extract_error_messages(caplog)
        ), (
            f"Expected DESC-REFRESH-STUB-HEAL-006; got: {_extract_error_messages(caplog)}"
        )

    def test_generate_repo_description_runtime_error_raises_runtime_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        _generate_repo_description raises RuntimeError -> _heal_stub_description
        returns None -> worker re-raises RuntimeError (so BJM marks job FAILED).
        atomic_write_description must NOT be called.
        ERROR DESC-REFRESH-STUB-HEAL-005 emitted by _heal_stub_description.
        """
        alias = "repo-gen-raises"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        with (
            patch(
                f"{_CLI_MANAGER_MODULE}.get_claude_cli_manager",
                return_value=_make_cli_mock(),
            ),
            patch(
                f"{_HOOK_MODULE}._generate_repo_description",
                side_effect=RuntimeError("simulated v10.4.13 anti-fallback"),
            ) as mock_gen,
            patch(f"{_HOOK_MODULE}.atomic_write_description") as mock_write,
            caplog.at_level(logging.ERROR),
        ):
            with pytest.raises(RuntimeError, match="runtime failure"):
                scheduler._heal_stub_description_worker(alias, str(clone_dir))

        mock_gen.assert_called_once()  # was invoked, raised
        mock_write.assert_not_called()  # write suppressed after exception
        assert any(
            "DESC-REFRESH-STUB-HEAL-005" in msg
            for msg in _extract_error_messages(caplog)
        ), (
            f"Expected DESC-REFRESH-STUB-HEAL-005; got: {_extract_error_messages(caplog)}"
        )
