"""
Unit tests for Story #724 Phase B1: verification pass wiring in meta_description_hook.

Verifies that invoke_verification_pass is wired correctly between
_generate_repo_description and atomic_write_description in on_repo_added:
- Flag off: verification not called, original body written, no AC9 log (AC12)
- Flag on: verified body from result written exactly once (AC1)
- Fallback: result.verified_document written (the fallback body on timeout)
- AC9 structured log emitted with exact repr() payload values after the write
- discovery_mode=False always passed to invoke_verification_pass
- atomic_write_description called exactly once per generation (AC1 / no double-lock)
"""

import shutil
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORIGINAL_BODY = "---\nname: test-repo\n---\n\n# test-repo\n\nOriginal body.\n"
_VERIFIED_BODY = "---\nname: test-repo\n---\n\n# test-repo\n\nVerified body.\n"
# Distinct body returned by VerificationResult.verified_document on fallback
_FALLBACK_BODY = "---\nname: test-repo\n---\n\n# test-repo\n\nFallback body.\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_dirs():
    """Create temporary golden-repos and clone directories."""
    golden_repos_dir = tempfile.mkdtemp()
    meta_dir = Path(golden_repos_dir) / "cidx-meta"
    meta_dir.mkdir()
    clone_dir = Path(golden_repos_dir) / "test-repo"
    clone_dir.mkdir()
    (clone_dir / "README.md").write_text("# Test Repo\n")
    yield {
        "golden_repos_dir": golden_repos_dir,
        "meta_dir": meta_dir,
        "clone_path": str(clone_dir),
        "repo_name": "test-repo",
        "repo_url": "https://github.com/test/test-repo",
    }
    shutil.rmtree(golden_repos_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_ci_config(*, enabled: bool) -> MagicMock:
    cfg = MagicMock()
    cfg.dep_map_fact_check_enabled = enabled
    cfg.dependency_map_pass_timeout_seconds = 600
    return cfg


def _make_server_config(*, enabled: bool) -> MagicMock:
    server_cfg = MagicMock()
    server_cfg.claude_integration_config = _make_ci_config(enabled=enabled)
    return server_cfg


def _make_verification_result(
    *,
    verified_document: str,
    fallback_reason: Optional[str] = None,
    counts: Optional[dict] = None,
    evidence: Optional[list] = None,
) -> MagicMock:
    result = MagicMock()
    result.verified_document = verified_document
    result.fallback_reason = fallback_reason
    result.counts = counts or {"verified": 3, "corrected": 1, "removed": 0, "added": 0}
    result.evidence = evidence or [
        {"claim": "repo uses Python", "evidence": "setup.py line 1"}
    ]
    return result


def _run_on_repo_added(
    dirs: dict,
    *,
    enabled: bool,
    verification_result: Optional[MagicMock] = None,
    invoke_side_effect=None,
    write_side_effect=None,
):
    """
    Run on_repo_added with all external dependencies mocked.

    Args:
        dirs: fixture dict from temp_dirs
        enabled: value for dep_map_fact_check_enabled
        verification_result: MagicMock returned by invoke_verification_pass;
            default is a success result with _VERIFIED_BODY
        invoke_side_effect: optional callable side effect for invoke_verification_pass;
            when set, overrides return_value (side_effect takes precedence in Mock)
        write_side_effect: optional callable side effect for atomic_write_description

    Returns:
        (mock_atomic_write, mock_invoke_verification_pass)
    """
    from code_indexer.global_repos.meta_description_hook import on_repo_added

    mock_cli_manager = MagicMock()
    mock_cli_manager.check_cli_available.return_value = True

    mock_config_service = MagicMock()
    mock_config_service.get_config.return_value = _make_server_config(enabled=enabled)

    default_vresult = verification_result or _make_verification_result(
        verified_document=_VERIFIED_BODY
    )

    if invoke_side_effect is not None:
        mock_invoke = MagicMock(side_effect=invoke_side_effect)
    else:
        mock_invoke = MagicMock(return_value=default_vresult)

    mock_atomic_write = MagicMock(side_effect=write_side_effect)

    with (
        patch(
            "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
            return_value=mock_cli_manager,
        ),
        patch(
            "code_indexer.global_repos.meta_description_hook._generate_repo_description",
            return_value=(_ORIGINAL_BODY, "success"),
        ),
        patch(
            "code_indexer.global_repos.meta_description_hook.atomic_write_description",
            mock_atomic_write,
        ),
        patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=mock_config_service,
        ),
        patch(
            "code_indexer.global_repos.meta_description_hook.get_config_service",
            return_value=mock_config_service,
            create=True,
        ),
        patch(
            "code_indexer.global_repos.dependency_map_analyzer.DependencyMapAnalyzer.invoke_verification_pass",
            mock_invoke,
        ),
    ):
        on_repo_added(
            repo_name=dirs["repo_name"],
            repo_url=dirs["repo_url"],
            clone_path=dirs["clone_path"],
            golden_repos_dir=dirs["golden_repos_dir"],
        )

    return mock_atomic_write, mock_invoke


# ===========================================================================
# TestWiringWhenFlagDisabled
# ===========================================================================


class TestWiringWhenFlagDisabled:
    """Verification is skipped entirely when dep_map_fact_check_enabled=False (AC12)."""

    def test_flag_false_skips_verification_call(self, temp_dirs):
        """invoke_verification_pass must NOT be called when flag is False."""
        _, mock_invoke = _run_on_repo_added(temp_dirs, enabled=False)
        assert mock_invoke.call_count == 0

    def test_flag_false_writes_original_body_not_verified(self, temp_dirs):
        """atomic_write_description must receive the original (unverified) body."""
        mock_atomic_write, _ = _run_on_repo_added(temp_dirs, enabled=False)
        assert mock_atomic_write.call_count == 1
        written_content = mock_atomic_write.call_args[0][1]
        assert written_content == _ORIGINAL_BODY

    def test_flag_false_does_not_log_verification_entry(self, temp_dirs, caplog):
        """No AC9 structured log entry is emitted when flag is False."""
        import logging

        with caplog.at_level(
            logging.INFO,
            logger="code_indexer.global_repos.meta_description_hook",
        ):
            _run_on_repo_added(temp_dirs, enabled=False)

        ac9_entries = [
            r for r in caplog.records if "AC9 description verification" in r.message
        ]
        assert len(ac9_entries) == 0


# ===========================================================================
# TestWiringWritePath
# ===========================================================================


class TestWiringWritePath:
    """Correct content reaches atomic_write_description when flag is enabled."""

    def test_flag_true_writes_verified_document_not_original(self, temp_dirs):
        """atomic_write_description receives result.verified_document."""
        vresult = _make_verification_result(verified_document=_VERIFIED_BODY)
        mock_atomic_write, _ = _run_on_repo_added(
            temp_dirs, enabled=True, verification_result=vresult
        )
        written_content = mock_atomic_write.call_args[0][1]
        assert written_content == _VERIFIED_BODY
        assert written_content != _ORIGINAL_BODY

    def test_flag_true_fallback_writes_result_verified_document(self, temp_dirs):
        """
        When verification returns fallback_reason='timeout', result.verified_document
        is written.  _FALLBACK_BODY is distinct from _ORIGINAL_BODY, so a buggy
        implementation that hardcodes the original will fail this assertion.
        """
        fallback_result = _make_verification_result(
            verified_document=_FALLBACK_BODY,
            fallback_reason="timeout",
        )
        mock_atomic_write, _ = _run_on_repo_added(
            temp_dirs, enabled=True, verification_result=fallback_result
        )
        written_content = mock_atomic_write.call_args[0][1]
        assert written_content == _FALLBACK_BODY
        assert written_content != _ORIGINAL_BODY

    def test_flag_true_single_lock_acquisition(self, temp_dirs):
        """
        atomic_write_description encapsulates lock acquisition.
        Must be called exactly once — no double-lock, no second write (AC1).
        """
        mock_atomic_write, _ = _run_on_repo_added(temp_dirs, enabled=True)
        assert mock_atomic_write.call_count == 1


# ===========================================================================
# TestWiringInvocationArgs
# ===========================================================================


class TestWiringInvocationArgs:
    """invoke_verification_pass is called with the correct arguments and ordering."""

    def test_flag_true_calls_verification_once_before_atomic_write(self, temp_dirs):
        """
        invoke_verification_pass is called exactly once, strictly before
        atomic_write_description.
        """
        call_order: list[str] = []
        vresult = _make_verification_result(verified_document=_VERIFIED_BODY)

        def _invoke_effect(*args, **kwargs):
            call_order.append("invoke_verification_pass")
            return vresult

        def _write_effect(*args, **kwargs):
            call_order.append("atomic_write_description")

        mock_atomic_write, mock_invoke = _run_on_repo_added(
            temp_dirs,
            enabled=True,
            invoke_side_effect=_invoke_effect,
            write_side_effect=_write_effect,
        )

        assert mock_invoke.call_count == 1
        assert mock_atomic_write.call_count == 1
        assert call_order == ["invoke_verification_pass", "atomic_write_description"], (
            f"Expected verification before write, got: {call_order}"
        )

    def test_flag_true_passes_discovery_mode_false(self, temp_dirs):
        """invoke_verification_pass must be called with discovery_mode=False."""
        _, mock_invoke = _run_on_repo_added(temp_dirs, enabled=True)
        assert mock_invoke.call_count == 1
        _, kwargs = mock_invoke.call_args
        assert kwargs.get("discovery_mode") is False


# ===========================================================================
# TestWiringLogging
# ===========================================================================


class TestWiringLogging:
    """AC9 structured log is emitted with exact repr() payload values after atomic write."""

    def test_flag_true_emits_ac9_structured_log_with_payload(self, temp_dirs, caplog):
        """
        logger.info("verification_pass", extra={...}) is emitted once.
        Assert payload values are present as LogRecord attributes set via extra=.
        Keys: domain_or_repo, counts, evidence, diff_summary, duration_ms, fallback_reason.
        """
        import logging

        known_counts = {"verified": 5, "corrected": 2, "removed": 0, "added": 0}
        known_evidence = [
            {"claim": "uses FastAPI", "evidence": "requirements.txt line 3"}
        ]
        known_fallback_reason = None

        vresult = _make_verification_result(
            verified_document=_VERIFIED_BODY,
            fallback_reason=known_fallback_reason,
            counts=known_counts,
            evidence=known_evidence,
        )

        with caplog.at_level(
            logging.INFO,
            logger="code_indexer.global_repos.meta_description_hook",
        ):
            _run_on_repo_added(temp_dirs, enabled=True, verification_result=vresult)

        ac9_entries = [
            r for r in caplog.records if r.getMessage() == "verification_pass"
        ]
        assert len(ac9_entries) == 1, (
            f"Expected 1 AC9 log entry, got {len(ac9_entries)}"
        )
        rec = ac9_entries[0]

        # repo alias present in domain_or_repo extra key
        assert hasattr(rec, "domain_or_repo"), "AC9 log missing 'domain_or_repo' key"
        assert rec.domain_or_repo == "test-repo"

        # structured extra keys all present
        for key in (
            "counts",
            "evidence",
            "diff_summary",
            "duration_ms",
            "fallback_reason",
        ):
            assert hasattr(rec, key), f"AC9 log missing key: {key}"

        assert rec.counts == known_counts
        assert rec.evidence == known_evidence
        assert rec.fallback_reason == known_fallback_reason
        assert isinstance(rec.duration_ms, int) and rec.duration_ms >= 0
