"""
Unit tests for Story #724 Phase B2: verification pass wiring in DependencyMapService.

Coverage:
- _run_verification_and_log: call signature, return contract, AC9 log, AC10 journal gating
- _update_domain_file (real method, temp files): delta merge flag gating, verified content
  written to disk, AC9 context label, AC10 journal gating

NOTE: The Pass 2 per-domain loop guard inside run_full_analysis is NOT exercised here —
calling run_full_analysis requires mocking the entire pipeline (staging dirs, Pass 1,
domain list construction, etc.). That guard is a single `if config.dep_map_fact_check_enabled:`
before calling _run_verification_and_log; it is implicitly covered because the helper
tests prove the helper's contract and the delta-merge real-method tests prove the gate
pattern works end-to-end through _update_domain_file.
"""

import logging
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.dependency_map_analyzer import VerificationResult
from code_indexer.server.services.dependency_map_service import DependencyMapService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORIGINAL_CONTENT = "# Domain: services\n\nOriginal generated body.\n"
_VERIFIED_CONTENT = "# Domain: services\n\nVerified body (corrected).\n"
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


def _make_verification_result(
    *,
    verified_document: str,
    fallback_reason: Optional[str] = None,
    counts: Optional[dict] = None,
    evidence: Optional[list] = None,
) -> VerificationResult:
    return VerificationResult(
        verified_document=verified_document,
        fallback_reason=fallback_reason,
        counts=counts or {"verified": 2, "corrected": 1, "removed": 0, "added": 0},
        evidence=evidence or ["ev1"],
    )


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


# ---------------------------------------------------------------------------
# TestRunVerificationAndLogHelper
# ---------------------------------------------------------------------------


class TestRunVerificationAndLogHelper:
    """Unit tests of _run_verification_and_log — call contract, AC9, AC10."""

    @pytest.mark.parametrize("fallback_reason", [None, "timeout", "double_timeout"])
    def test_returns_verified_document(self, fallback_reason: Optional[str]) -> None:
        """Helper returns result.verified_document regardless of fallback_reason."""
        service, mock_analyzer, _ = _build_service()
        expected = _VERIFIED_CONTENT if fallback_reason is None else _ORIGINAL_CONTENT
        mock_analyzer.invoke_verification_pass.return_value = _make_verification_result(
            verified_document=expected, fallback_reason=fallback_reason
        )

        returned = service._run_verification_and_log(
            document_content=_ORIGINAL_CONTENT,
            repo_list=[],
            discovery_mode=False,
            context_label="pass2:services",
        )

        assert returned == expected

    def test_calls_invoke_with_correct_kwargs(self) -> None:
        """invoke_verification_pass receives all required kwargs including discovery_mode=False."""
        service, mock_analyzer, _ = _build_service()
        mock_analyzer.invoke_verification_pass.return_value = _make_verification_result(
            verified_document=_VERIFIED_CONTENT
        )
        repo_list = [{"alias": "repo-a"}]
        ci_config = service._config_manager.get_claude_integration_config()

        service._run_verification_and_log(
            document_content=_ORIGINAL_CONTENT,
            repo_list=repo_list,
            discovery_mode=False,
            context_label="pass2:services",
        )

        mock_analyzer.invoke_verification_pass.assert_called_once_with(
            document_content=_ORIGINAL_CONTENT,
            repo_list=repo_list,
            discovery_mode=False,
            claude_integration_config=ci_config,
        )

    @pytest.mark.parametrize("journal_active", [True, False])
    def test_ac9_always_emitted(
        self, caplog: pytest.LogCaptureFixture, journal_active: bool
    ) -> None:
        """AC9: structured 'verification_pass' logger.info emitted regardless of journal state."""
        service, mock_analyzer, mock_journal = _build_service()
        mock_journal.is_active = journal_active
        mock_analyzer.invoke_verification_pass.return_value = _make_verification_result(
            verified_document=_VERIFIED_CONTENT
        )

        with caplog.at_level(
            logging.INFO,
            logger="code_indexer.server.services.dependency_map_service",
        ):
            service._run_verification_and_log(
                document_content=_ORIGINAL_CONTENT,
                repo_list=[],
                discovery_mode=False,
                context_label="pass2:services",
            )

        vp_records = [
            r for r in caplog.records if r.getMessage() == "verification_pass"
        ]
        assert len(vp_records) == 1
        rec = vp_records[0]
        for key in (
            "domain_or_repo",
            "counts",
            "evidence",
            "fallback_reason",
            "diff_summary",
            "duration_ms",
        ):
            assert hasattr(rec, key), f"AC9 log missing key: {key}"
        assert rec.domain_or_repo == "pass2:services"

    def test_ac10_journal_written_when_active(self) -> None:
        """AC10: journal.log called with context_label in summary when is_active=True."""
        service, mock_analyzer, mock_journal = _build_service()
        mock_journal.is_active = True
        mock_analyzer.invoke_verification_pass.return_value = _make_verification_result(
            verified_document=_VERIFIED_CONTENT
        )

        service._run_verification_and_log(
            document_content=_ORIGINAL_CONTENT,
            repo_list=[],
            discovery_mode=False,
            context_label="pass2:services",
        )

        mock_journal.log.assert_called_once()
        assert "pass2:services" in mock_journal.log.call_args[0][0]

    def test_ac10_journal_skipped_when_inactive(self) -> None:
        """AC10: journal.log NOT called when is_active=False."""
        service, mock_analyzer, mock_journal = _build_service()
        mock_journal.is_active = False
        mock_analyzer.invoke_verification_pass.return_value = _make_verification_result(
            verified_document=_VERIFIED_CONTENT
        )

        service._run_verification_and_log(
            document_content=_ORIGINAL_CONTENT,
            repo_list=[],
            discovery_mode=False,
            context_label="pass2:services",
        )

        mock_journal.log.assert_not_called()

    @pytest.mark.parametrize("domain", ["services", "data", "api"])
    def test_multiple_invocations_emit_separate_ac9_logs(
        self, caplog: pytest.LogCaptureFixture, domain: str
    ) -> None:
        """Each call emits exactly one AC9 log with matching context."""
        service, mock_analyzer, _ = _build_service()
        mock_analyzer.invoke_verification_pass.return_value = _make_verification_result(
            verified_document=_VERIFIED_CONTENT
        )

        with caplog.at_level(
            logging.INFO,
            logger="code_indexer.server.services.dependency_map_service",
        ):
            service._run_verification_and_log(
                document_content=_ORIGINAL_CONTENT,
                repo_list=[],
                discovery_mode=False,
                context_label=f"pass2:{domain}",
            )

        vp_records = [
            r for r in caplog.records if r.getMessage() == "verification_pass"
        ]
        assert len(vp_records) == 1
        assert vp_records[0].domain_or_repo == f"pass2:{domain}"


# ---------------------------------------------------------------------------
# TestDeltaMergeVerificationWiring — real _update_domain_file with temp files
# ---------------------------------------------------------------------------


class TestDeltaMergeVerificationWiring:
    """Integration tests of the real _update_domain_file method.

    Uses a real temp domain file. Mocks analyzer collaborators so no Claude CLI runs.
    Verifies the verification gate, disk write, AC9 label, and AC10 journal gating.
    """

    @pytest.fixture()
    def domain_file(self) -> "Path":
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "services.md"
            p.write_text(_DOMAIN_FILE_WITH_FRONTMATTER)
            yield p

    def _prime_analyzer(
        self,
        mock_analyzer: MagicMock,
        *,
        merge_body: str = _MERGE_RESULT_BODY,
        verified_document: str = _VERIFIED_MERGE_BODY,
        fallback_reason: Optional[str] = None,
    ) -> None:
        mock_analyzer.build_delta_merge_prompt.return_value = "mock prompt"
        mock_analyzer.invoke_delta_merge_file.return_value = merge_body
        mock_analyzer.invoke_verification_pass.return_value = _make_verification_result(
            verified_document=verified_document, fallback_reason=fallback_reason
        )

    def test_flag_false_no_verification_call_no_ac9_log(
        self, caplog: pytest.LogCaptureFixture, domain_file: Path
    ) -> None:
        """Flag=False: invoke_verification_pass not called; no AC9 log; file still written."""
        service, mock_analyzer, _ = _build_service(fact_check_enabled=False)
        self._prime_analyzer(mock_analyzer)
        config = _make_ci_config(fact_check_enabled=False)

        with caplog.at_level(
            logging.INFO,
            logger="code_indexer.server.services.dependency_map_service",
        ):
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
        vp = [r for r in caplog.records if r.getMessage() == "verification_pass"]
        assert len(vp) == 0
        assert domain_file.exists() and len(domain_file.read_text()) > 0

    def test_flag_true_writes_verified_content_to_disk(self, domain_file: Path) -> None:
        """Flag=True: verified_document fragment appears in the file after the call."""
        service, mock_analyzer, _ = _build_service(fact_check_enabled=True)
        self._prime_analyzer(mock_analyzer, verified_document=_VERIFIED_MERGE_BODY)
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

        written = domain_file.read_text()
        assert _VERIFIED_MERGE_BODY in written

    def test_flag_true_ac9_log_has_delta_merge_context(
        self, caplog: pytest.LogCaptureFixture, domain_file: Path
    ) -> None:
        """AC9: context label is 'delta_merge:services' on the delta path."""
        service, mock_analyzer, _ = _build_service(fact_check_enabled=True)
        self._prime_analyzer(mock_analyzer)
        config = _make_ci_config(fact_check_enabled=True)

        with caplog.at_level(
            logging.INFO,
            logger="code_indexer.server.services.dependency_map_service",
        ):
            service._update_domain_file(
                domain_name="services",
                domain_file=domain_file,
                changed_repos=["repo-a"],
                new_repos=[],
                removed_repos=[],
                domain_list=["services"],
                config=config,
            )

        vp = [r for r in caplog.records if r.getMessage() == "verification_pass"]
        assert len(vp) == 1
        assert vp[0].domain_or_repo == "delta_merge:services"

    def test_flag_true_ac10_journal_written_when_active(
        self, domain_file: Path
    ) -> None:
        """AC10: journal.log called on delta merge path when is_active=True."""
        service, mock_analyzer, mock_journal = _build_service(fact_check_enabled=True)
        mock_journal.is_active = True
        self._prime_analyzer(mock_analyzer)
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

        mock_journal.log.assert_called_once()

    def test_flag_true_ac10_journal_skipped_when_inactive(
        self, domain_file: Path
    ) -> None:
        """AC10: journal.log NOT called on delta merge path when is_active=False."""
        service, mock_analyzer, mock_journal = _build_service(fact_check_enabled=True)
        mock_journal.is_active = False
        self._prime_analyzer(mock_analyzer)
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

        mock_journal.log.assert_not_called()

    def test_flag_true_fallback_content_on_disk(self, domain_file: Path) -> None:
        """When verification returns fallback, fallback document written to disk."""
        service, mock_analyzer, _ = _build_service(fact_check_enabled=True)
        fallback_doc = _MERGE_RESULT_BODY  # fallback = same as merge body
        self._prime_analyzer(
            mock_analyzer,
            merge_body=_MERGE_RESULT_BODY,
            verified_document=fallback_doc,
            fallback_reason="timeout",
        )
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

        written = domain_file.read_text()
        assert fallback_doc in written
