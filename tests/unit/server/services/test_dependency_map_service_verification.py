"""
Unit tests for Story #724 v2: verification pass wiring in DependencyMapService.

v2 contract: invoke_verification_pass(document_path, repo_list, config) -> None
  - edits document_path in place
  - no _run_verification_and_log helper (deleted)
  - no VerificationResult, no discovery_mode
  - VerificationFailed propagates (no except-swallower on delta merge path)

Coverage (_update_domain_file — real method, temp files):
- Flag off: invoke_verification_pass not called; file written with merge content
- Flag on: invoke_verification_pass called with domain_file; in-place edit reflected on disk
- VerificationFailed propagates (no swallower)
- repo_list built from changed + new + removed aliases (all three sources)
"""

import tempfile
from pathlib import Path
from typing import Any, Generator, cast
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.dependency_map_analyzer import VerificationFailed
from code_indexer.server.services.dependency_map_service import DependencyMapService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DOMAIN_FILE_WITH_FRONTMATTER = (
    "---\n"
    "domain: services\n"
    "last_analyzed: 2026-01-01T00:00:00+00:00\n"
    "---\n\n"
    "Original body text.\n"
)
_MERGE_RESULT_BODY = "Updated body after delta merge.\n"
_VERIFIED_MERGE_BODY = "Verified delta merge body.\n"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_ci_config(*, fact_check_enabled: bool) -> MagicMock:
    cfg = MagicMock()
    cfg.dep_map_fact_check_enabled = fact_check_enabled
    cfg.dependency_map_pass_timeout_seconds = 600
    cfg.dependency_map_delta_max_turns = 5
    return cfg


def _build_service(
    *, fact_check_enabled: bool = True
) -> "tuple[DependencyMapService, MagicMock, MagicMock]":
    """Return (service, mock_analyzer, mock_journal) with real DependencyMapService."""
    mock_analyzer = MagicMock()
    mock_config_manager = MagicMock()
    mock_config_manager.get_claude_integration_config.return_value = _make_ci_config(
        fact_check_enabled=fact_check_enabled
    )

    service = DependencyMapService(
        golden_repos_manager=MagicMock(),
        config_manager=mock_config_manager,
        tracking_backend=MagicMock(),
        analyzer=mock_analyzer,
    )

    mock_journal = MagicMock()
    mock_journal.is_active = False
    mock_journal.journal_path = None
    service._activity_journal = mock_journal

    return service, mock_analyzer, mock_journal


def _prime_analyzer(
    mock_analyzer: MagicMock,
    *,
    merge_body: str = _MERGE_RESULT_BODY,
    invoke_side_effect=None,
) -> None:
    """Configure mock_analyzer for a delta merge that produces merge_body."""
    mock_analyzer.build_delta_merge_prompt.return_value = "mock prompt"
    mock_analyzer.invoke_delta_merge_file.return_value = merge_body
    if invoke_side_effect is not None:
        mock_analyzer.invoke_verification_pass.side_effect = invoke_side_effect
    else:
        mock_analyzer.invoke_verification_pass.return_value = None  # v2: returns None


def _extract_repo_list(mock_analyzer: MagicMock) -> list[Any]:
    """Extract the repo_list argument from the first invoke_verification_pass call."""
    call_args = mock_analyzer.invoke_verification_pass.call_args
    # Accept both positional (path, repo_list, config) and keyword forms
    # cast needed: MagicMock call_args indexing returns Any
    if call_args[0] and len(call_args[0]) > 1:
        return cast(list[Any], call_args[0][1])
    return cast(list[Any], call_args[1].get("repo_list", []))


# ---------------------------------------------------------------------------
# TestDeltaMergeVerificationWiring — real _update_domain_file with temp files
# ---------------------------------------------------------------------------


class TestDeltaMergeVerificationWiring:
    """Integration tests of the real _update_domain_file method.

    Uses a real temp domain file. Mocks analyzer collaborators so no Claude CLI runs.
    Verifies the verification gate, disk write, and VerificationFailed propagation.
    """

    @pytest.fixture()
    def domain_file(self) -> Generator[Path, None, None]:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "services.md"
            p.write_text(_DOMAIN_FILE_WITH_FRONTMATTER)
            yield p

    def test_flag_false_no_verification_call(self, domain_file: Path) -> None:
        """Flag=False: invoke_verification_pass not called; file still written."""
        service, mock_analyzer, _ = _build_service(fact_check_enabled=False)
        _prime_analyzer(mock_analyzer)
        config = _make_ci_config(fact_check_enabled=False)

        service._update_domain_file(
            domain_name="services",
            domain_file=domain_file,
            changed_repos=["repo-a"],
            new_repos=[],
            removed_repos=[],
            domain_list=["services"],
            config=config,
        )

        mock_analyzer.invoke_verification_pass.assert_not_called()
        assert domain_file.exists() and len(domain_file.read_text()) > 0

    def test_flag_false_merge_content_written(self, domain_file: Path) -> None:
        """Flag=False: merge body appears in file after call."""
        service, mock_analyzer, _ = _build_service(fact_check_enabled=False)
        _prime_analyzer(mock_analyzer, merge_body=_MERGE_RESULT_BODY)
        config = _make_ci_config(fact_check_enabled=False)

        service._update_domain_file(
            domain_name="services",
            domain_file=domain_file,
            changed_repos=["repo-a"],
            new_repos=[],
            removed_repos=[],
            domain_list=["services"],
            config=config,
        )

        assert _MERGE_RESULT_BODY in domain_file.read_text()

    def test_flag_true_calls_verification_with_temp_file_in_same_dir(
        self, domain_file: Path
    ) -> None:
        """Flag=True: invoke_verification_pass receives a temp path in domain_file.parent
        (not domain_file itself) — spec AC8 temp-file pattern."""
        service, mock_analyzer, _ = _build_service(fact_check_enabled=True)
        _prime_analyzer(mock_analyzer)
        config = _make_ci_config(fact_check_enabled=True)

        service._update_domain_file(
            domain_name="services",
            domain_file=domain_file,
            changed_repos=["repo-a"],
            new_repos=[],
            removed_repos=[],
            domain_list=["services"],
            config=config,
        )

        mock_analyzer.invoke_verification_pass.assert_called_once()
        call_args = mock_analyzer.invoke_verification_pass.call_args
        received_path = (
            call_args[0][0] if call_args[0] else call_args[1].get("document_path")
        )
        # Must be a Path in the same directory as domain_file, but NOT domain_file itself
        assert isinstance(received_path, Path)
        assert received_path.parent == domain_file.parent
        assert received_path != domain_file

    def test_flag_true_in_place_edit_reflected_on_disk(self, domain_file: Path) -> None:
        """Flag=True: when verification edits domain_file in-place, new content on disk."""
        service, mock_analyzer, _ = _build_service(fact_check_enabled=True)

        def _edit_in_place(document_path, *args, **kwargs):
            document_path.write_text(_VERIFIED_MERGE_BODY)

        _prime_analyzer(mock_analyzer, invoke_side_effect=_edit_in_place)
        config = _make_ci_config(fact_check_enabled=True)

        service._update_domain_file(
            domain_name="services",
            domain_file=domain_file,
            changed_repos=["repo-a"],
            new_repos=[],
            removed_repos=[],
            domain_list=["services"],
            config=config,
        )

        assert _VERIFIED_MERGE_BODY in domain_file.read_text()

    def test_flag_true_repo_list_includes_all_alias_sources(
        self, domain_file: Path
    ) -> None:
        """repo_list passed to invoke_verification_pass contains aliases from
        changed_repos, new_repos, and removed_repos — all three sources."""
        service, mock_analyzer, _ = _build_service(fact_check_enabled=True)
        _prime_analyzer(mock_analyzer)
        config = _make_ci_config(fact_check_enabled=True)

        service._update_domain_file(
            domain_name="services",
            domain_file=domain_file,
            changed_repos=["changed-repo"],
            new_repos=["new-repo"],
            removed_repos=["removed-repo"],
            domain_list=["services"],
            config=config,
        )

        repo_list = _extract_repo_list(mock_analyzer)
        aliases = [r["alias"] for r in repo_list]
        assert "changed-repo" in aliases, f"changed alias missing: {aliases}"
        assert "new-repo" in aliases, f"new alias missing: {aliases}"
        assert "removed-repo" in aliases, f"removed alias missing: {aliases}"

    def test_flag_true_verification_failed_propagates(self, domain_file: Path) -> None:
        """VerificationFailed must propagate — no except-swallower on delta merge path.
        Also confirms domain_file is NOT overwritten with unverified content (AC8)."""
        service, mock_analyzer, _ = _build_service(fact_check_enabled=True)
        original_content = domain_file.read_text()

        def _raise_failed(document_path, *args, **kwargs):
            raise VerificationFailed("both attempts failed")

        _prime_analyzer(mock_analyzer, invoke_side_effect=_raise_failed)
        config = _make_ci_config(fact_check_enabled=True)

        with pytest.raises(VerificationFailed):
            service._update_domain_file(
                domain_name="services",
                domain_file=domain_file,
                changed_repos=["repo-a"],
                new_repos=[],
                removed_repos=[],
                domain_list=["services"],
                config=config,
            )

        # domain_file must still contain the original content — unverified merge never written
        assert domain_file.read_text() == original_content
