"""
Unit tests for Story #724 verification pass — envelope parsing, evidence filter,
safety guards, and return contract.

Tests: TestEnvelopeParsing, TestEvidenceFilter, TestSafetyGuards, TestReturnContract
"""

import json
import subprocess
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.dependency_map_analyzer import (
    DependencyMapAnalyzer,
    VerificationResult,
    _VERIFICATION_SEMAPHORE_STATE,
)


def _make_analyzer() -> DependencyMapAnalyzer:
    """Return a minimally configured DependencyMapAnalyzer for unit testing."""
    return DependencyMapAnalyzer(
        golden_repos_root=Path("/tmp/fake-repos"),
        cidx_meta_path=Path("/tmp/fake-meta"),
        pass_timeout=60,
        analysis_model="opus",
    )


def _make_config(timeout: int = 60, max_concurrent: int = 2) -> MagicMock:
    """Return a mock ClaudeIntegrationConfig with given parameters."""
    cfg = MagicMock()
    cfg.fact_check_timeout_seconds = timeout
    cfg.max_concurrent_claude_cli = max_concurrent
    return cfg


def _wrap_as_claude_output(inner_obj: object, is_error: bool = False) -> str:
    """Wrap inner_obj as a Claude CLI --output-format json envelope stdout."""
    inner_json = json.dumps(inner_obj)
    outer = {"result": inner_json, "is_error": is_error}
    return json.dumps(outer)


def _make_valid_inner(
    corrected_document: str = "corrected",
    evidence: list = None,
    counts: dict = None,
) -> dict:
    """Build a valid inner response dict."""
    if evidence is None:
        evidence = []
    if counts is None:
        counts = {"verified": 0, "corrected": 0, "removed": 0, "added": 0}
    return {
        "corrected_document": corrected_document,
        "evidence": evidence,
        "counts": counts,
    }


def _make_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    """Build a fake subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


class TestEnvelopeParsing(TestCase):
    """Tests for _parse_verification_envelope (AC2)."""

    def setUp(self) -> None:
        _VERIFICATION_SEMAPHORE_STATE.clear()
        self.analyzer = _make_analyzer()

    def test_happy_path_returns_inner_dict(self) -> None:
        inner = _make_valid_inner(corrected_document="doc")
        stdout = _wrap_as_claude_output(inner)
        result = self.analyzer._parse_verification_envelope(stdout, "original")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["corrected_document"], "doc")

    def test_malformed_outer_json_returns_fallback(self) -> None:
        result = self.analyzer._parse_verification_envelope("{not json}", "original")
        self.assertIsInstance(result, VerificationResult)
        self.assertEqual(result.fallback_reason, "envelope_parse_error")
        self.assertEqual(result.verified_document, "original")

    def test_outer_not_dict_returns_fallback(self) -> None:
        stdout = json.dumps([1, 2, 3])  # valid JSON but not a dict
        result = self.analyzer._parse_verification_envelope(stdout, "original")
        self.assertIsInstance(result, VerificationResult)
        self.assertEqual(result.fallback_reason, "envelope_parse_error")

    def test_is_error_true_returns_fallback(self) -> None:
        outer = json.dumps({"is_error": True, "error": "auth failed", "result": None})
        result = self.analyzer._parse_verification_envelope(outer, "original")
        self.assertIsInstance(result, VerificationResult)
        self.assertEqual(result.fallback_reason, "envelope_is_error")

    def test_missing_result_key_returns_fallback(self) -> None:
        outer = json.dumps({"is_error": False})
        result = self.analyzer._parse_verification_envelope(outer, "original")
        self.assertIsInstance(result, VerificationResult)
        self.assertEqual(result.fallback_reason, "missing_required_field")

    def test_malformed_inner_json_returns_fallback(self) -> None:
        outer = json.dumps({"is_error": False, "result": "{bad json"})
        result = self.analyzer._parse_verification_envelope(outer, "original")
        self.assertIsInstance(result, VerificationResult)
        self.assertEqual(result.fallback_reason, "inner_parse_error")

    def test_inner_not_dict_returns_fallback(self) -> None:
        outer = json.dumps({"is_error": False, "result": json.dumps([1, 2, 3])})
        result = self.analyzer._parse_verification_envelope(outer, "original")
        self.assertIsInstance(result, VerificationResult)
        self.assertEqual(result.fallback_reason, "inner_parse_error")

    def test_missing_corrected_document_key_returns_fallback(self) -> None:
        inner = {"evidence": [], "counts": {}}
        stdout = _wrap_as_claude_output(inner)
        result = self.analyzer._parse_verification_envelope(stdout, "original")
        self.assertIsInstance(result, VerificationResult)
        self.assertEqual(result.fallback_reason, "missing_required_field")

    def test_missing_evidence_key_returns_fallback(self) -> None:
        inner = {"corrected_document": "x", "counts": {}}
        stdout = _wrap_as_claude_output(inner)
        result = self.analyzer._parse_verification_envelope(stdout, "original")
        self.assertIsInstance(result, VerificationResult)
        self.assertEqual(result.fallback_reason, "missing_required_field")

    def test_missing_counts_key_returns_fallback(self) -> None:
        inner = {"corrected_document": "x", "evidence": []}
        stdout = _wrap_as_claude_output(inner)
        result = self.analyzer._parse_verification_envelope(stdout, "original")
        self.assertIsInstance(result, VerificationResult)
        self.assertEqual(result.fallback_reason, "missing_required_field")


class TestEvidenceFilter(TestCase):
    """Tests for _apply_verification_evidence_filters (AC3 + AC4)."""

    def setUp(self) -> None:
        _VERIFICATION_SEMAPHORE_STATE.clear()
        self.analyzer = _make_analyzer()

    def test_verified_no_evidence_kept(self) -> None:
        items = [{"disposition": "VERIFIED", "claim": "X calls Y"}]
        filtered, counts, _ = self.analyzer._apply_verification_evidence_filters(
            items, True
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(counts["verified"], 1)

    def test_corrected_with_file_evidence_kept(self) -> None:
        items = [
            {
                "disposition": "CORRECTED",
                "claim": "X",
                "file_path": "a.py",
                "line_range": [1, 2],
            }
        ]
        filtered, counts, _ = self.analyzer._apply_verification_evidence_filters(
            items, True
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(counts["corrected"], 1)

    def test_corrected_with_symbol_evidence_kept(self) -> None:
        items = [
            {
                "disposition": "CORRECTED",
                "claim": "X",
                "symbol": "Foo",
                "definition_location": "a.py:10",
            }
        ]
        filtered, counts, _ = self.analyzer._apply_verification_evidence_filters(
            items, True
        )
        self.assertEqual(len(filtered), 1)

    def test_corrected_no_evidence_discarded(self) -> None:
        items = [{"disposition": "CORRECTED", "claim": "X"}]
        filtered, counts, _ = self.analyzer._apply_verification_evidence_filters(
            items, True
        )
        self.assertEqual(len(filtered), 0)
        self.assertEqual(counts["corrected"], 0)

    def test_added_with_evidence_discovery_mode_true_kept(self) -> None:
        items = [
            {
                "disposition": "ADDED",
                "claim": "new dep",
                "file_path": "b.py",
                "line_range": [5, 5],
            }
        ]
        filtered, counts, _ = self.analyzer._apply_verification_evidence_filters(
            items, True
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(counts["added"], 1)

    def test_added_with_evidence_discovery_mode_false_dropped(self) -> None:
        items = [
            {
                "disposition": "ADDED",
                "claim": "new dep",
                "file_path": "b.py",
                "line_range": [5, 5],
            }
        ]
        filtered, counts, _ = self.analyzer._apply_verification_evidence_filters(
            items, False
        )
        self.assertEqual(len(filtered), 0)
        self.assertEqual(counts["added"], 0)

    def test_added_no_evidence_any_mode_discarded(self) -> None:
        items = [{"disposition": "ADDED", "claim": "no proof"}]
        for discovery_mode in (True, False):
            filtered, _, _discarded = (
                self.analyzer._apply_verification_evidence_filters(
                    items, discovery_mode
                )
            )
            self.assertEqual(len(filtered), 0, f"discovery_mode={discovery_mode}")

    def test_removed_no_evidence_kept_as_signal(self) -> None:
        items = [{"disposition": "REMOVED", "claim": "gone"}]
        filtered, counts, _ = self.analyzer._apply_verification_evidence_filters(
            items, True
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(counts["removed"], 1)

    def test_non_dict_item_skipped(self) -> None:
        items = ["not-a-dict", {"disposition": "VERIFIED", "claim": "ok"}]
        filtered, counts, _ = self.analyzer._apply_verification_evidence_filters(
            items, True
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(counts["verified"], 1)


class TestSafetyGuards(TestCase):
    """Tests for _check_verification_safety_guards (AC7) — boundary values."""

    def setUp(self) -> None:
        _VERIFICATION_SEMAPHORE_STATE.clear()
        self.analyzer = _make_analyzer()

    def _counts(
        self, verified: int = 0, corrected: int = 0, removed: int = 0, added: int = 0
    ) -> dict:
        return {
            "verified": verified,
            "corrected": corrected,
            "removed": removed,
            "added": added,
        }

    # --- Length guard (boundary: 49%, 50%, 51% of original) ---

    def test_length_49_percent_fires_guard(self) -> None:
        original = "x" * 100
        corrected = "x" * 49  # 49% < 50%
        result = self.analyzer._check_verification_safety_guards(
            corrected, original, self._counts(verified=1)
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.fallback_reason, "guard_length_under_threshold")
        self.assertEqual(result.verified_document, original)

    def test_length_exactly_50_percent_fires_guard(self) -> None:
        original = "x" * 100
        corrected = (
            "x" * 50
        )  # exactly 50% — boundary is < 0.5 * orig, so 50==50 is NOT < 50
        result = self.analyzer._check_verification_safety_guards(
            corrected, original, self._counts(verified=1)
        )
        # 50 is NOT < 0.5 * 100, so guard should NOT fire
        self.assertIsNone(result)

    def test_length_51_percent_does_not_fire_guard(self) -> None:
        original = "x" * 100
        corrected = "x" * 51  # 51% > 50%
        result = self.analyzer._check_verification_safety_guards(
            corrected, original, self._counts(verified=1)
        )
        self.assertIsNone(result)

    # --- Removed ratio guard (boundary: 49%, 50%, 51% of total) ---

    def test_removed_49_percent_does_not_fire(self) -> None:
        original = "x" * 100
        corrected = "x" * 100
        counts = self._counts(verified=51, removed=49)  # 49/100 = 49%
        result = self.analyzer._check_verification_safety_guards(
            corrected, original, counts
        )
        self.assertIsNone(result)

    def test_removed_50_percent_fires_guard(self) -> None:
        original = "x" * 100
        corrected = "x" * 100
        counts = self._counts(verified=50, removed=50)  # 50/100 = 50% >= threshold
        result = self.analyzer._check_verification_safety_guards(
            corrected, original, counts
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.fallback_reason, "guard_removed_ratio_over_threshold")

    def test_removed_51_percent_fires_guard(self) -> None:
        original = "x" * 100
        corrected = "x" * 100
        counts = self._counts(verified=49, removed=51)  # 51/100 > 50%
        result = self.analyzer._check_verification_safety_guards(
            corrected, original, counts
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.fallback_reason, "guard_removed_ratio_over_threshold")

    # --- Empty counts guard ---

    def test_empty_counts_on_nonempty_document_fires_guard(self) -> None:
        result = self.analyzer._check_verification_safety_guards(
            "corrected", "original non-empty", self._counts()
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.fallback_reason, "guard_empty_counts_on_nonempty_doc")

    def test_empty_counts_on_empty_document_no_guard(self) -> None:
        result = self.analyzer._check_verification_safety_guards("", "", self._counts())
        self.assertIsNone(result)


class TestReturnContract(TestCase):
    """Tests for invoke_verification_pass return contract (happy path and fallbacks)."""

    def setUp(self) -> None:
        _VERIFICATION_SEMAPHORE_STATE.clear()
        self.analyzer = _make_analyzer()
        self.config = _make_config(timeout=10, max_concurrent=2)

    def _run_with_stdout(self, stdout: str) -> VerificationResult:
        """Patch subprocess.run to return given stdout and invoke the method."""
        completed = _make_completed(stdout)
        with patch("subprocess.run", return_value=completed):
            return self.analyzer.invoke_verification_pass(
                document_content="original " * 20,
                repo_list=[{"alias": "r1", "clone_path": "/r1"}],
                discovery_mode=False,
                claude_integration_config=self.config,
            )

    def test_happy_path_fallback_reason_is_none(self) -> None:
        original = "The service calls the database." * 5
        inner = _make_valid_inner(
            corrected_document=original,
            evidence=[{"disposition": "VERIFIED", "claim": "service calls database"}],
            counts={"verified": 1, "corrected": 0, "removed": 0, "added": 0},
        )
        stdout = _wrap_as_claude_output(inner)
        completed = _make_completed(stdout)
        with patch("subprocess.run", return_value=completed):
            result = self.analyzer.invoke_verification_pass(
                document_content=original,
                repo_list=[],
                discovery_mode=False,
                claude_integration_config=self.config,
            )
        self.assertIsNone(result.fallback_reason)
        self.assertEqual(result.verified_document, original)
        self.assertIsInstance(result.evidence, list)
        self.assertIsInstance(result.counts, dict)

    def test_envelope_parse_error_fallback_carries_original(self) -> None:
        result = self._run_with_stdout("{invalid json")
        self.assertEqual(result.fallback_reason, "envelope_parse_error")
        self.assertIn("original", result.verified_document)

    def test_missing_required_field_fallback(self) -> None:
        inner = {"corrected_document": "x", "evidence": []}  # missing "counts"
        stdout = _wrap_as_claude_output(inner)
        result = self._run_with_stdout(stdout)
        self.assertEqual(result.fallback_reason, "missing_required_field")

    def test_guard_fires_fallback_carries_original(self) -> None:
        original = "x" * 100
        inner = _make_valid_inner(
            corrected_document="x" * 10,  # 10% < 50% threshold — triggers length guard
            evidence=[{"disposition": "VERIFIED", "claim": "c"}],
            counts={"verified": 1, "corrected": 0, "removed": 0, "added": 0},
        )
        stdout = _wrap_as_claude_output(inner)
        completed = _make_completed(stdout)
        with patch("subprocess.run", return_value=completed):
            result = self.analyzer.invoke_verification_pass(
                document_content=original,
                repo_list=[],
                discovery_mode=False,
                claude_integration_config=self.config,
            )
        self.assertEqual(result.fallback_reason, "guard_length_under_threshold")
        self.assertEqual(result.verified_document, original)


def _make_analyzer_for_public_contract(tmp_path: object) -> DependencyMapAnalyzer:
    """Return a minimally configured DependencyMapAnalyzer for public-contract tests.

    tmp_path is accepted for API symmetry with the semaphore test module but is
    not used here — invoke_verification_pass does not require a real on-disk repo.
    """
    return _make_analyzer()


class TestInvokePublicContractFallback(TestCase):
    """AC3/4 public contract: any discarded evidence item causes invoke_verification_pass
    to fall back to the original document with fallback_reason='unsupported_claims_in_document'.
    These tests exercise the public method, not the private helper."""

    def setUp(self) -> None:
        _VERIFICATION_SEMAPHORE_STATE.clear()
        self.analyzer = _make_analyzer_for_public_contract(None)
        self.config = _make_config(timeout=10, max_concurrent=2)

    def test_discarded_corrected_item_triggers_fallback(self) -> None:
        """CORRECTED item missing both evidence forms -> public method returns fallback."""
        original = "ORIGINAL CONTENT " * 5
        inner = {
            "corrected_document": "MODEL MODIFIED CONTENT",
            "evidence": [
                {"claim": "thing", "disposition": "CORRECTED"},  # no evidence fields
                {"claim": "other", "disposition": "VERIFIED"},
            ],
            "counts": {"verified": 1, "corrected": 0, "removed": 0, "added": 0},
        }
        completed = _make_completed(_wrap_as_claude_output(inner))
        with patch("subprocess.run", return_value=completed):
            result = self.analyzer.invoke_verification_pass(
                document_content=original,
                repo_list=[],
                discovery_mode=False,
                claude_integration_config=self.config,
            )
        self.assertEqual(result.fallback_reason, "unsupported_claims_in_document")
        self.assertEqual(result.verified_document, original)

    def test_discarded_added_under_discovery_mode_false_triggers_fallback(self) -> None:
        """ADDED item with valid evidence still triggers fallback when
        discovery_mode=False (all ADDED items are discarded in that mode)."""
        original = "ORIGINAL " * 5
        inner = {
            "corrected_document": "MODEL CONTENT WITH NEW CLAIM",
            "evidence": [
                {
                    "claim": "A depends on B",
                    "disposition": "ADDED",
                    "file_path": "src/a.py",
                    "line_range": [10, 15],
                },
                {"claim": "existing", "disposition": "VERIFIED"},
            ],
            "counts": {"verified": 1, "corrected": 0, "removed": 0, "added": 0},
        }
        completed = _make_completed(_wrap_as_claude_output(inner))
        with patch("subprocess.run", return_value=completed):
            result = self.analyzer.invoke_verification_pass(
                document_content=original,
                repo_list=[],
                discovery_mode=False,
                claude_integration_config=self.config,
            )
        self.assertEqual(result.fallback_reason, "unsupported_claims_in_document")
        self.assertEqual(result.verified_document, original)

    def test_no_discards_returns_verified_document(self) -> None:
        """Happy path: all evidence items are valid -> verified_document is the model corrected,
        which is DISTINCT from the original input to prove the method returns model output."""
        original = "The service calls the database. " * 5
        corrected = "The service calls the cache layer, which calls the database. " * 5
        # Precondition: corrected and original must differ so the assertion is meaningful.
        self.assertNotEqual(corrected, original)
        inner = {
            "corrected_document": corrected,
            "evidence": [
                {
                    "claim": "keep this",
                    "disposition": "CORRECTED",
                    "file_path": "src/x.py",
                    "line_range": [1, 5],
                },
                {"claim": "verified item", "disposition": "VERIFIED"},
            ],
            "counts": {"verified": 1, "corrected": 1, "removed": 0, "added": 0},
        }
        completed = _make_completed(_wrap_as_claude_output(inner))
        with patch("subprocess.run", return_value=completed):
            result = self.analyzer.invoke_verification_pass(
                document_content=original,
                repo_list=[],
                discovery_mode=False,
                claude_integration_config=self.config,
            )
        self.assertIsNone(result.fallback_reason)
        self.assertNotEqual(result.verified_document, original)
        self.assertEqual(result.verified_document, corrected)


import pytest  # noqa: E402 — placed after unittest imports for test discovery


def _make_success_envelope() -> str:
    """Return a Claude CLI --output-format json success envelope with empty verification."""
    return json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": json.dumps(
                {
                    "corrected_document": "OK",
                    "evidence": [],
                    "counts": {
                        "verified": 0,
                        "corrected": 0,
                        "removed": 0,
                        "added": 0,
                    },
                }
            ),
        }
    )


class TestVerificationCliArgs:
    """Story #724 bug caught on staging E2E: verification Claude CLI call was
    missing --dangerously-skip-permissions, causing exit 1 in tool-use mode.

    Fixtures: ``verification_analyzer``, ``default_cfg``, ``patched_subprocess``.
    Tests mutate ``default_cfg`` before calling ``_invoke`` when a non-default
    config value is needed; otherwise they call ``_invoke`` directly.
    """

    @pytest.fixture()
    def verification_analyzer(self, tmp_path):
        """Minimally configured DependencyMapAnalyzer for CLI-args tests."""
        return _make_analyzer_for_public_contract(tmp_path)

    @pytest.fixture()
    def default_cfg(self):
        """Default mock ClaudeIntegrationConfig with delta_max_turns=30."""
        cfg = MagicMock()
        cfg.max_concurrent_claude_cli = 2
        cfg.fact_check_timeout_seconds = 60
        cfg.dependency_map_delta_max_turns = 30
        return cfg

    @pytest.fixture()
    def patched_subprocess(self, monkeypatch):
        """Install a fake subprocess.run that returns a success envelope.

        Returns a dict that is populated with ``{"cmd": [...]}`` on first call.
        """
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=_make_success_envelope(),
                stderr="",
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        return captured

    def _invoke(self, analyzer, cfg, captured) -> list:
        """Call invoke_verification_pass and return the captured cmd list."""
        analyzer.invoke_verification_pass(
            document_content="orig",
            repo_list=[],
            discovery_mode=False,
            claude_integration_config=cfg,
        )
        return captured["cmd"]

    def test_cmd_includes_dangerously_skip_permissions(
        self, verification_analyzer, default_cfg, patched_subprocess
    ):
        """The verification cmd list must include --dangerously-skip-permissions
        because tool-use mode (max-turns > 0) requires it in a non-interactive context."""
        cmd = self._invoke(verification_analyzer, default_cfg, patched_subprocess)
        assert "--dangerously-skip-permissions" in cmd, (
            f"verification cmd must include --dangerously-skip-permissions; got {cmd}"
        )

    def test_cmd_max_turns_comes_from_config(
        self, verification_analyzer, default_cfg, patched_subprocess
    ):
        """The --max-turns value must come from dependency_map_delta_max_turns
        (default 30), not a hardcoded value like 1. Uses sentinel value 42."""
        default_cfg.dependency_map_delta_max_turns = 42
        cmd = self._invoke(verification_analyzer, default_cfg, patched_subprocess)
        assert "--max-turns" in cmd, f"cmd missing --max-turns: {cmd}"
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "42", f"expected --max-turns 42, got {cmd[idx + 1]}"

    def test_cmd_uses_output_format_json(
        self, verification_analyzer, default_cfg, patched_subprocess
    ):
        """Output-format json must be present (AC2 two-layer parse depends on it)."""
        cmd = self._invoke(verification_analyzer, default_cfg, patched_subprocess)
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"
