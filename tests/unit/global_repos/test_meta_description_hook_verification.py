"""
Unit tests for Story #724 v2: verification pass wiring in meta_description_hook.

Bug #1038 contract: invoke_verification_pass(document_path, repo_list, config) -> bool
  - edits document_path in place when successful
  - returns True on success, False on failure
  - on False: caller logs warning and uses unverified content (no exception propagation)
  - atomic_write_description IS called regardless of verification outcome

Coverage:
- Flag off: verification not called, original body written (AC12)
- Flag on + success (True): mock edits file in place, atomic_write_description receives verified body
- Flag on + failure (False): unverified content IS written via atomic_write_description
- Ordering: invoke_verification_pass called before atomic_write_description
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORIGINAL_BODY = "---\nname: test-repo\n---\n\n# test-repo\n\nOriginal body.\n"
_VERIFIED_BODY = "---\nname: test-repo\n---\n\n# test-repo\n\nVerified body.\n"


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


def _run_on_repo_added(
    dirs: dict,
    *,
    enabled: bool,
    invoke_side_effect=None,
    invoke_return_value: bool = True,
    write_side_effect=None,
):
    """
    Run on_repo_added with all external dependencies mocked.

    The mock for invoke_verification_pass defaults to returning True (success),
    simulating a successful in-place edit.  Pass invoke_side_effect to override
    (e.g. to write _VERIFIED_BODY to the path arg), or invoke_return_value=False
    to simulate a verification failure.

    Returns:
        (mock_atomic_write, mock_invoke_verification_pass)
    """
    from code_indexer.global_repos.meta_description_hook import on_repo_added

    mock_cli_manager = MagicMock()
    mock_cli_manager.check_cli_available.return_value = True

    mock_config_service = MagicMock()
    mock_config_service.get_config.return_value = _make_server_config(enabled=enabled)

    mock_invoke = MagicMock(
        side_effect=invoke_side_effect, return_value=invoke_return_value
    )
    mock_atomic_write = MagicMock(side_effect=write_side_effect)

    with (
        patch(
            "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
            return_value=mock_cli_manager,
        ),
        patch(
            "code_indexer.global_repos.meta_description_hook._generate_repo_description",
            return_value=_ORIGINAL_BODY,
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

    def test_flag_false_writes_original_body(self, temp_dirs):
        """atomic_write_description must receive the original (unverified) body."""
        mock_atomic_write, _ = _run_on_repo_added(temp_dirs, enabled=False)
        assert mock_atomic_write.call_count == 1
        written_content = mock_atomic_write.call_args[0][1]
        assert written_content == _ORIGINAL_BODY


# ===========================================================================
# TestWiringWritePath
# ===========================================================================


class TestWiringWritePath:
    """atomic_write_description receives verified content read from the temp file."""

    def test_flag_true_writes_verified_content(self, temp_dirs):
        """
        Spec AC8 contract: invoke_verification_pass edits the TEMP file in-place,
        then atomic_write_description is called ONCE with the verified (edited) body.
        """

        def _edit_in_place(document_path, *args, **kwargs):
            # Simulate Claude editing the temp file in-place
            document_path.write_text(_VERIFIED_BODY)
            return True

        mock_atomic_write, mock_invoke = _run_on_repo_added(
            temp_dirs, enabled=True, invoke_side_effect=_edit_in_place
        )

        assert mock_invoke.call_count == 1
        assert mock_atomic_write.call_count == 1
        # atomic_write_description receives the VERIFIED body (read from temp after edit)
        written_content = mock_atomic_write.call_args[0][1]
        assert written_content == _VERIFIED_BODY

    def test_flag_true_single_atomic_write_call(self, temp_dirs):
        """atomic_write_description called exactly once — no double-lock (AC1)."""
        mock_atomic_write, _ = _run_on_repo_added(temp_dirs, enabled=True)
        assert mock_atomic_write.call_count == 1


# ===========================================================================
# TestVerificationFailed
# ===========================================================================


class TestVerificationFailed:
    """When invoke_verification_pass returns False, unverified content is still written (Bug #1038).

    The old behavior raised VerificationFailed and skipped atomic_write_description.
    The new behavior (Bug #1038): False return causes warning log and fallback to
    unverified content — atomic_write_description IS called with the original body.
    """

    def test_verification_false_still_writes_original_content(self, temp_dirs):
        """Bug #1038: when invoke_verification_pass returns False,
        atomic_write_description is called with the original (unverified) body."""
        mock_atomic_write, mock_invoke = _run_on_repo_added(
            temp_dirs, enabled=True, invoke_return_value=False
        )

        # atomic_write_description MUST still be called (graceful fallback)
        assert mock_atomic_write.call_count == 1
        written_content = mock_atomic_write.call_args[0][1]
        assert written_content == _ORIGINAL_BODY

    def test_verification_false_invoke_called_once(self, temp_dirs):
        """invoke_verification_pass called exactly once even when it returns False."""
        _, mock_invoke = _run_on_repo_added(
            temp_dirs, enabled=True, invoke_return_value=False
        )
        assert mock_invoke.call_count == 1


# ===========================================================================
# TestWiringOrdering
# ===========================================================================


class TestWiringOrdering:
    """invoke_verification_pass called BEFORE atomic_write_description (Spec AC8)."""

    def test_verification_before_atomic_write(self, temp_dirs):
        """Spec AC8 call order: invoke_verification_pass first, then atomic_write_description.
        Verification runs on the temp file; atomic write ships the verified content."""
        call_order: list = []

        def _invoke_effect(document_path, *args, **kwargs):
            call_order.append("invoke_verification_pass")
            return True

        def _write_effect(*args, **kwargs):
            call_order.append("atomic_write_description")

        _run_on_repo_added(
            temp_dirs,
            enabled=True,
            invoke_side_effect=_invoke_effect,
            write_side_effect=_write_effect,
        )

        assert call_order == ["invoke_verification_pass", "atomic_write_description"], (
            f"Expected verification before write, got: {call_order}"
        )
