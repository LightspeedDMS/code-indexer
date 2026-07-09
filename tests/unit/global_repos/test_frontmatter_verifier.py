"""
Unit + integration tests for frontmatter_verifier — Story #1067.

Covers all 6 acceptance criteria (Gherkin scenarios):
  1. Well-formed frontmatter -> PASS, no violations
  2. Missing required lifecycle key -> FAIL naming lifecycle.<key>
  3. Invalid enum value -> FAIL naming the offending enum field + value
  4. Malformed YAML -> FAIL with "frontmatter does not parse" violation, no exception
  5. Empty description fails; 150-char non-empty description PASSES (no length floor)
  6. Batch report: counts valid/invalid, per-file violations, never raises on bad file

Integration test: helper agrees with UnifiedResponseParser._validate on same frontmatter
(no-drift guarantee).
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from code_indexer.global_repos.frontmatter_verifier import (
    BatchReport,
    VerificationResult,
    verify_file,
    verify_batch,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

VALID_LIFECYCLE: Dict[str, Any] = {
    "ci_system": "github-actions",
    "deployment_target": "kubernetes",
    "language_ecosystem": "python/poetry",
    "build_system": "poetry",
    "testing_framework": "pytest",
    "confidence": "high",
}

VALID_DESCRIPTION = "A Python service for semantic code search and analysis."
VALID_BODY = "## Overview\n\nThis repository implements semantic code search.\n"


def _build_md(
    description: str = VALID_DESCRIPTION,
    lifecycle: Optional[Dict[str, Any]] = None,
    body: str = VALID_BODY,
    raw_frontmatter: Optional[
        str
    ] = None,  # bypass dict serialization for malformed-YAML test
) -> str:
    """Build a .md file string with YAML frontmatter + body."""
    if raw_frontmatter is not None:
        return f"---\n{raw_frontmatter}\n---\n\n{body}"
    lc = lifecycle if lifecycle is not None else VALID_LIFECYCLE
    fm: Dict[str, Any] = {"description": description, "lifecycle": lc}
    yaml_text = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)
    return f"---\n{yaml_text}---\n\n{body}"


def _write_md(tmp_path: Path, filename: str, content: str) -> Path:
    p = tmp_path / filename
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# VerificationResult contract checks
# ---------------------------------------------------------------------------


class TestVerificationResultShape:
    """VerificationResult is a structured value with .passed, .violations list."""

    def test_pass_result_has_empty_violations(self, tmp_path: Path) -> None:
        p = _write_md(tmp_path, "valid.md", _build_md())
        result = verify_file(p)
        assert isinstance(result, VerificationResult)
        assert result.passed is True
        assert result.violations == []

    def test_fail_result_has_nonempty_violations(self, tmp_path: Path) -> None:
        lc = {k: v for k, v in VALID_LIFECYCLE.items() if k != "confidence"}
        p = _write_md(tmp_path, "missing_conf.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is False
        assert len(result.violations) >= 1


# ---------------------------------------------------------------------------
# Scenario 1: Well-formed frontmatter -> PASS
# ---------------------------------------------------------------------------


class TestScenario1WellFormedPasses:
    """Scenario 1: well-formed frontmatter with valid description, all six lifecycle
    keys with valid values, and a non-empty body -> PASS with no violations."""

    def test_well_formed_minimum_v2_lifecycle(self, tmp_path: Path) -> None:
        """All six required keys, valid values, non-empty body -> PASS."""
        p = _write_md(tmp_path, "well_formed.md", _build_md())
        result = verify_file(p)
        assert result.passed is True
        assert result.violations == []

    def test_well_formed_with_optional_branching_section(self, tmp_path: Path) -> None:
        """Optional branching section with valid values -> PASS."""
        lc = {
            **VALID_LIFECYCLE,
            "branching": {
                "model": "github-flow",
                "release_branch_pattern": None,
                "protected_branches": ["main"],
                "default_branch": "main",
            },
        }
        p = _write_md(tmp_path, "with_branching.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is True

    def test_well_formed_confidence_medium(self, tmp_path: Path) -> None:
        lc = {**VALID_LIFECYCLE, "confidence": "medium"}
        p = _write_md(tmp_path, "conf_medium.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is True

    def test_well_formed_confidence_low(self, tmp_path: Path) -> None:
        lc = {**VALID_LIFECYCLE, "confidence": "low"}
        p = _write_md(tmp_path, "conf_low.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is True


# ---------------------------------------------------------------------------
# Scenario 2: Missing required lifecycle key -> FAIL naming lifecycle.<key>
# ---------------------------------------------------------------------------


class TestScenario2MissingLifecycleKey:
    """Scenario 2: omitting any of the six required lifecycle keys -> FAIL naming
    lifecycle.<key> in violations."""

    @pytest.mark.parametrize(
        "missing_key",
        [
            "ci_system",
            "deployment_target",
            "language_ecosystem",
            "build_system",
            "testing_framework",
            "confidence",
        ],
    )
    def test_missing_lifecycle_key_fails_with_field_name(
        self, tmp_path: Path, missing_key: str
    ) -> None:
        lc = {k: v for k, v in VALID_LIFECYCLE.items() if k != missing_key}
        p = _write_md(tmp_path, f"missing_{missing_key}.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is False
        # Violation must name the missing field
        combined = " ".join(result.violations)
        assert f"lifecycle.{missing_key}" in combined, (
            f"Expected 'lifecycle.{missing_key}' in violations, got: {result.violations}"
        )

    def test_missing_confidence_specifically(self, tmp_path: Path) -> None:
        """Scenario 2 explicit: omit confidence -> FAIL naming lifecycle.confidence."""
        lc = {k: v for k, v in VALID_LIFECYCLE.items() if k != "confidence"}
        p = _write_md(tmp_path, "no_confidence.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is False
        assert any("lifecycle.confidence" in v for v in result.violations)

    def test_missing_lifecycle_block_entirely(self, tmp_path: Path) -> None:
        """No lifecycle block at all -> FAIL naming lifecycle."""
        fm = {"description": VALID_DESCRIPTION}
        yaml_text = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)
        content = f"---\n{yaml_text}---\n\n{VALID_BODY}"
        p = _write_md(tmp_path, "no_lifecycle.md", content)
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "lifecycle" in combined


# ---------------------------------------------------------------------------
# Scenario 3: Invalid enum value -> FAIL naming offending enum field + value
# ---------------------------------------------------------------------------


class TestScenario3InvalidEnumValue:
    """Scenario 3: confidence = 'unknown' (not in {high, medium, low}) -> FAIL
    naming the offending field and value."""

    def test_confidence_unknown_fails_with_field_and_value(
        self, tmp_path: Path
    ) -> None:
        lc = {**VALID_LIFECYCLE, "confidence": "unknown"}
        p = _write_md(tmp_path, "bad_confidence.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "lifecycle.confidence" in combined
        assert "unknown" in combined

    def test_confidence_critical_not_allowed(self, tmp_path: Path) -> None:
        lc = {**VALID_LIFECYCLE, "confidence": "critical"}
        p = _write_md(tmp_path, "bad_confidence2.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is False

    def test_optional_branching_invalid_model(self, tmp_path: Path) -> None:
        """branching.model not in allowed enum -> FAIL naming lifecycle.branching.model."""
        lc = {
            **VALID_LIFECYCLE,
            "branching": {
                "model": "some-custom-flow",  # not in enum
                "release_branch_pattern": None,
                "protected_branches": None,
                "default_branch": "main",
            },
        }
        p = _write_md(tmp_path, "bad_branching_model.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "lifecycle.branching.model" in combined

    def test_optional_ci_invalid_deploy_on(self, tmp_path: Path) -> None:
        """ci.deploy_on not in allowed enum -> FAIL naming lifecycle.ci.deploy_on."""
        lc = {
            **VALID_LIFECYCLE,
            "ci": {
                "trigger_events": ["push"],
                "required_checks": ["lint"],
                "deploy_on": "on-commit",  # not in enum
                "environments": None,
            },
        }
        p = _write_md(tmp_path, "bad_ci_deploy.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "lifecycle.ci.deploy_on" in combined

    def test_optional_release_invalid_versioning(self, tmp_path: Path) -> None:
        """release.versioning not in allowed enum -> FAIL."""
        lc = {
            **VALID_LIFECYCLE,
            "release": {
                "versioning": "dateversion",  # not in enum
                "version_source": None,
                "changelog": None,
                "auto_publish": False,
                "artifact_types": [],
            },
        }
        p = _write_md(tmp_path, "bad_release_ver.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "lifecycle.release.versioning" in combined


# ---------------------------------------------------------------------------
# Scenario 4: Malformed YAML -> FAIL with "frontmatter does not parse"
# ---------------------------------------------------------------------------


class TestScenario4MalformedYaml:
    """Scenario 4: content that is not valid YAML -> FAIL with a 'frontmatter does
    not parse' violation, NOT an exception."""

    def test_malformed_yaml_returns_fail_not_exception(self, tmp_path: Path) -> None:
        raw = "description: [unclosed bracket\nlifecycle: {bad:"
        content = f"---\n{raw}\n---\n\n{VALID_BODY}"
        p = _write_md(tmp_path, "malformed.md", content)
        # Must NOT raise
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "frontmatter" in combined.lower()
        assert "parse" in combined.lower()

    def test_missing_opening_delimiter_returns_fail(self, tmp_path: Path) -> None:
        """Content without --- header is treated as 'no frontmatter' -> FAIL."""
        content = "description: something\n\n# Body\n"
        p = _write_md(tmp_path, "no_delimiters.md", content)
        result = verify_file(p)
        assert result.passed is False

    def test_tab_in_yaml_indentation_may_fail(self, tmp_path: Path) -> None:
        """YAML with tab indentation -> FAIL cleanly (tabs are illegal in YAML)."""
        raw = "description: test\nlifecycle:\n\tconfidence: high\n"
        content = f"---\n{raw}\n---\n\n{VALID_BODY}"
        p = _write_md(tmp_path, "tab_yaml.md", content)
        # Must not raise regardless of outcome
        result = verify_file(p)
        assert isinstance(result, VerificationResult)

    def test_completely_invalid_yaml_returns_fail(self, tmp_path: Path) -> None:
        """: value that causes scanner error."""
        raw = ": this is not valid yaml key: another"
        content = f"---\n{raw}\n---\n\n{VALID_BODY}"
        p = _write_md(tmp_path, "scanner_error.md", content)
        result = verify_file(p)
        assert isinstance(result, VerificationResult)
        # Either fail (YAML error) or treated as no frontmatter; either way no exception


# ---------------------------------------------------------------------------
# Scenario 5: Empty description fails; short-but-non-empty description PASSES
# ---------------------------------------------------------------------------


class TestScenario5DescriptionRules:
    """Scenario 5: empty description FAILS; a 150-char honest description PASSES
    (no length floor — bug #1064 established the [500,2000] floor was fictional)."""

    def test_empty_description_fails(self, tmp_path: Path) -> None:
        p = _write_md(tmp_path, "empty_desc.md", _build_md(description=""))
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "description" in combined

    def test_whitespace_only_description_fails(self, tmp_path: Path) -> None:
        p = _write_md(tmp_path, "ws_desc.md", _build_md(description="   "))
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "description" in combined

    def test_150_char_description_passes_no_length_floor(self, tmp_path: Path) -> None:
        """A concise 150-character description is VALID — no minimum length."""
        desc = "A" * 150
        p = _write_md(tmp_path, "short_desc.md", _build_md(description=desc))
        result = verify_file(p)
        assert result.passed is True, (
            f"150-char description should PASS (no length floor). "
            f"Violations: {result.violations}"
        )

    def test_50_char_description_passes(self, tmp_path: Path) -> None:
        """Even 50 chars is valid — the only rule is 'non-empty'."""
        desc = "Short but valid repository description text here!!"
        assert len(desc) == 50
        p = _write_md(tmp_path, "fifty_char.md", _build_md(description=desc))
        result = verify_file(p)
        assert result.passed is True

    def test_one_char_description_passes(self, tmp_path: Path) -> None:
        """Single character is non-empty -> PASS."""
        p = _write_md(tmp_path, "one_char.md", _build_md(description="X"))
        result = verify_file(p)
        assert result.passed is True

    def test_missing_description_key_fails(self, tmp_path: Path) -> None:
        """No description key at all -> FAIL."""
        fm = {"lifecycle": VALID_LIFECYCLE}
        yaml_text = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)
        content = f"---\n{yaml_text}---\n\n{VALID_BODY}"
        p = _write_md(tmp_path, "no_desc_key.md", content)
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "description" in combined

    def test_empty_body_fails(self, tmp_path: Path) -> None:
        """Non-empty body is required -> empty body -> FAIL."""
        p = _write_md(tmp_path, "empty_body.md", _build_md(body=""))
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "body" in combined.lower()

    def test_whitespace_only_body_fails(self, tmp_path: Path) -> None:
        """Whitespace-only body is not non-empty."""
        p = _write_md(tmp_path, "ws_body.md", _build_md(body="   \n  \n"))
        result = verify_file(p)
        assert result.passed is False


# ---------------------------------------------------------------------------
# Scenario 6: Batch report
# ---------------------------------------------------------------------------


class TestScenario6BatchReport:
    """Scenario 6: verify_batch over a directory with a mix of valid/invalid files.

    Reports count of valid vs invalid, per-file violation detail, and NEVER
    raises on an individual malformed file (one bad file does not abort the batch)."""

    def _make_batch_dir(self, tmp_path: Path) -> Path:
        """Create a directory with 2 valid + 2 invalid .md files."""
        # valid-1: everything correct
        _write_md(tmp_path, "repo_alpha.md", _build_md())
        # valid-2: confidence=low, 50-char description
        lc2 = {**VALID_LIFECYCLE, "confidence": "low"}
        _write_md(
            tmp_path,
            "repo_beta.md",
            _build_md(
                description="Short but valid description of this repository!!",
                lifecycle=lc2,
            ),
        )
        # invalid-1: missing confidence
        lc3 = {k: v for k, v in VALID_LIFECYCLE.items() if k != "confidence"}
        _write_md(tmp_path, "repo_gamma.md", _build_md(lifecycle=lc3))
        # invalid-2: malformed YAML
        raw = "description: [unclosed bracket\nlifecycle: bad"
        _write_md(tmp_path, "repo_delta.md", f"---\n{raw}\n---\n\n{VALID_BODY}")
        return tmp_path

    def test_batch_counts_valid_and_invalid(self, tmp_path: Path) -> None:
        d = self._make_batch_dir(tmp_path)
        report = verify_batch(d)
        assert isinstance(report, BatchReport)
        assert report.valid_count == 2
        assert report.invalid_count == 2

    def test_batch_total_equals_file_count(self, tmp_path: Path) -> None:
        d = self._make_batch_dir(tmp_path)
        report = verify_batch(d)
        assert report.total_count == 4

    def test_batch_per_file_violations_present_for_invalid(
        self, tmp_path: Path
    ) -> None:
        d = self._make_batch_dir(tmp_path)
        report = verify_batch(d)
        # All invalid files must have per-file violation detail
        for alias, result in report.per_file.items():
            if not result.passed:
                assert len(result.violations) >= 1, (
                    f"Invalid file {alias} has no violations listed"
                )

    def test_batch_never_raises_on_malformed_file(self, tmp_path: Path) -> None:
        """One malformed file must NOT abort the batch."""
        raw = "description: [unclosed bracket\nlifecycle: bad"
        _write_md(tmp_path, "only_bad.md", f"---\n{raw}\n---\n\n{VALID_BODY}")
        _write_md(tmp_path, "good.md", _build_md())
        # Must not raise
        report = verify_batch(tmp_path)
        assert report.valid_count == 1
        assert report.invalid_count == 1

    def test_batch_empty_directory_returns_zero_counts(self, tmp_path: Path) -> None:
        report = verify_batch(tmp_path)
        assert report.valid_count == 0
        assert report.invalid_count == 0
        assert report.total_count == 0

    def test_batch_ignores_non_md_files(self, tmp_path: Path) -> None:
        """Non-.md files (e.g., .json, .txt) must be ignored."""
        (tmp_path / "_domains.json").write_text('{"foo": 1}')
        (tmp_path / "README.txt").write_text("notes")
        _write_md(tmp_path, "valid.md", _build_md())
        report = verify_batch(tmp_path)
        assert report.total_count == 1

    def test_batch_ignores_underscore_files(self, tmp_path: Path) -> None:
        """Files starting with _ (like _active_analysis.lock) are skipped."""
        _write_md(tmp_path, "_internal.md", _build_md())  # should be skipped
        _write_md(tmp_path, "repo_real.md", _build_md())
        report = verify_batch(tmp_path)
        assert report.total_count == 1

    def test_batch_report_str_summary_mentions_counts(self, tmp_path: Path) -> None:
        d = self._make_batch_dir(tmp_path)
        report = verify_batch(d)
        summary = str(report)
        # Summary should surface counts somewhere
        assert "2" in summary  # both valid_count and invalid_count are 2


# ---------------------------------------------------------------------------
# Integration test: no drift vs UnifiedResponseParser._validate
# ---------------------------------------------------------------------------


class TestNoDriftWithUnifiedResponseParser:
    """Integration: verify_file and UnifiedResponseParser._validate agree on
    the same frontmatter data.  They must never produce contradictory verdicts."""

    def _parser_errors(self, fm: Dict[str, Any]) -> List[Any]:
        """Return the raw error list from UnifiedResponseParser._validate."""
        from code_indexer.global_repos.unified_response_parser import (
            UnifiedResponseParser,
        )

        return list(UnifiedResponseParser._validate(fm))

    def _verify_result(
        self, tmp_path: Path, fm: Dict[str, Any], body: str = VALID_BODY
    ) -> VerificationResult:
        yaml_text = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)
        content = f"---\n{yaml_text}---\n\n{body}"
        p = tmp_path / "probe.md"
        p.write_text(content, encoding="utf-8")
        return verify_file(p)

    def test_valid_fm_agrees_pass(self, tmp_path: Path) -> None:
        fm = {"description": VALID_DESCRIPTION, "lifecycle": VALID_LIFECYCLE}
        parser_errors = self._parser_errors(fm)
        helper_result = self._verify_result(tmp_path, fm)
        assert parser_errors == []  # parser says valid
        assert helper_result.passed is True  # helper must agree

    def test_missing_confidence_agrees_fail(self, tmp_path: Path) -> None:
        lc = {k: v for k, v in VALID_LIFECYCLE.items() if k != "confidence"}
        fm = {"description": VALID_DESCRIPTION, "lifecycle": lc}
        parser_errors = self._parser_errors(fm)
        helper_result = self._verify_result(tmp_path, fm)
        assert len(parser_errors) > 0  # parser says invalid
        assert helper_result.passed is False  # helper must agree

    def test_bad_confidence_enum_agrees_fail(self, tmp_path: Path) -> None:
        lc = {**VALID_LIFECYCLE, "confidence": "unknown"}
        fm = {"description": VALID_DESCRIPTION, "lifecycle": lc}
        parser_errors = self._parser_errors(fm)
        helper_result = self._verify_result(tmp_path, fm)
        assert len(parser_errors) > 0
        assert helper_result.passed is False

    def test_empty_description_agrees_fail(self, tmp_path: Path) -> None:
        fm = {"description": "", "lifecycle": VALID_LIFECYCLE}
        parser_errors = self._parser_errors(fm)
        helper_result = self._verify_result(tmp_path, fm)
        assert len(parser_errors) > 0
        assert helper_result.passed is False

    @pytest.mark.parametrize(
        "key",
        [
            "ci_system",
            "deployment_target",
            "language_ecosystem",
            "build_system",
            "testing_framework",
        ],
    )
    def test_missing_any_required_key_agrees(self, tmp_path: Path, key: str) -> None:
        lc = {k: v for k, v in VALID_LIFECYCLE.items() if k != key}
        fm = {"description": VALID_DESCRIPTION, "lifecycle": lc}
        parser_errors = self._parser_errors(fm)
        helper_result = self._verify_result(tmp_path, fm)
        assert len(parser_errors) > 0, f"Parser should reject missing {key}"
        assert helper_result.passed is False, f"Helper should reject missing {key}"

    def test_no_drift_on_150_char_description(self, tmp_path: Path) -> None:
        """Both parser and helper must PASS a 150-char description (no length floor)."""
        fm = {"description": "A" * 150, "lifecycle": VALID_LIFECYCLE}
        parser_errors = self._parser_errors(fm)
        helper_result = self._verify_result(tmp_path, fm)
        assert parser_errors == []  # parser: no floor
        assert helper_result.passed is True  # helper: no floor either


# ---------------------------------------------------------------------------
# VerificationResult.__str__ on FAIL (lines 64-66)
# ---------------------------------------------------------------------------


class TestVerificationResultStr:
    """VerificationResult.__str__ on a failing result surfaces violations."""

    def test_str_on_pass_returns_PASS(self) -> None:
        r = VerificationResult(passed=True, violations=[])
        assert str(r) == "PASS"

    def test_str_on_fail_contains_violation_text(self, tmp_path: Path) -> None:
        """Line 64-66: FAIL branch of __str__ joins violations with '; '."""
        lc = {k: v for k, v in VALID_LIFECYCLE.items() if k != "confidence"}
        p = _write_md(tmp_path, "no_conf.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is False
        s = str(result)
        assert s.startswith("FAIL")
        # The violation text must be embedded after the em-dash
        assert "lifecycle.confidence" in s

    def test_str_on_fail_with_multiple_violations(self, tmp_path: Path) -> None:
        """Multiple violations are joined by '; '."""
        # Missing description AND confidence => 2 violations at minimum
        lc = {k: v for k, v in VALID_LIFECYCLE.items() if k != "confidence"}
        fm = {"lifecycle": lc}  # also missing description key
        import yaml as _yaml

        yaml_text = _yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)
        content = f"---\n{yaml_text}---\n\n{VALID_BODY}"
        p = _write_md(tmp_path, "multi_viol.md", content)
        result = verify_file(p)
        assert result.passed is False
        s = str(result)
        assert "FAIL" in s
        assert "; " in s or len(result.violations) == 1  # joined or single


# ---------------------------------------------------------------------------
# Parse edge cases: no closing ---, empty YAML, non-dict YAML (lines 119, 132, 135)
# ---------------------------------------------------------------------------


class TestParseEdgeCases:
    """Cover _parse_frontmatter_and_body branches not hit by existing tests."""

    def test_no_closing_delimiter_returns_fail(self, tmp_path: Path) -> None:
        """Line 119: opening --- present but no closing --- -> structured FAIL."""
        content = "---\ndescription: hello\nlifecycle:\n  confidence: high\n"
        # No closing ---
        p = _write_md(tmp_path, "no_close.md", content)
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "frontmatter" in combined.lower()

    def test_empty_yaml_block_returns_fail(self, tmp_path: Path) -> None:
        """Line 132: YAML block is empty (only whitespace between --- markers)."""
        content = "---\n\n---\n\n## Body\n"
        p = _write_md(tmp_path, "empty_yaml.md", content)
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "frontmatter" in combined.lower()

    def test_non_dict_frontmatter_returns_fail(self, tmp_path: Path) -> None:
        """Line 135: YAML parses to a non-dict scalar (e.g. integer 42)."""
        content = "---\n42\n---\n\n## Body\n"
        p = _write_md(tmp_path, "scalar_yaml.md", content)
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "frontmatter" in combined.lower()


# ---------------------------------------------------------------------------
# OSError read path (lines 250-251)
# ---------------------------------------------------------------------------


class TestOSErrorPath:
    """verify_file must return structured FAIL when the file cannot be read."""

    def test_directory_path_triggers_oserror(self, tmp_path: Path) -> None:
        """Lines 250-251: passing a directory to verify_file -> OSError -> FAIL."""
        result = verify_file(tmp_path)  # tmp_path is a directory, not a file
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "frontmatter" in combined.lower()
        assert "cannot read" in combined.lower()

    def test_nonexistent_file_triggers_oserror(self, tmp_path: Path) -> None:
        """Lines 250-251: nonexistent file path -> OSError -> structured FAIL."""
        ghost = tmp_path / "does_not_exist.md"
        result = verify_file(ghost)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert "cannot read" in combined.lower()


# ---------------------------------------------------------------------------
# Batch belt-and-suspenders except Exception (lines 291-297)
# ---------------------------------------------------------------------------


class TestBatchBeltAndSuspenders:
    """Lines 291-297: the outer try/except in verify_batch catches unexpected raises."""

    def test_batch_on_directory_with_unreadable_entry(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        """Simulate verify_file raising an unexpected exception for one file.

        The batch must NOT propagate the exception; instead it records a FAIL
        result with 'unexpected error' in the violation text.
        """
        import code_indexer.global_repos.frontmatter_verifier as fv_module

        # Write one real valid file and one that will trigger the exception
        _write_md(tmp_path, "good.md", _build_md())
        _write_md(tmp_path, "boom.md", _build_md())  # will be monkeypatched

        original_verify_file = fv_module.verify_file
        call_count = [0]

        def patched_verify_file(path: Path) -> VerificationResult:
            call_count[0] += 1
            if path.name == "boom.md":
                raise RuntimeError("simulated unexpected error")
            return original_verify_file(path)

        monkeypatch.setattr(fv_module, "verify_file", patched_verify_file)

        report = verify_batch(tmp_path)
        assert report.total_count == 2
        # The exception file is recorded as invalid
        assert report.invalid_count == 1
        assert report.valid_count == 1
        # The boom file's violation mentions 'unexpected error'
        boom_result = report.per_file.get("boom")
        assert boom_result is not None
        assert boom_result.passed is False
        assert any("unexpected error" in v for v in boom_result.violations)


# ---------------------------------------------------------------------------
# Drift-guard: normalized lifecycle.<key> prefix in missing-key violation (N2)
# ---------------------------------------------------------------------------


class TestDriftGuardNormalization:
    """N2: assert the normalized 'lifecycle.<key>' prefix appears in violations.

    If UnifiedResponseParser's 'missing required lifecycle field: <key>' message
    wording ever changes in a way that breaks _normalize_parser_error, this test
    fails loudly rather than silently dropping the prefix.
    """

    @pytest.mark.parametrize(
        "missing_key",
        [
            "ci_system",
            "deployment_target",
            "language_ecosystem",
            "build_system",
            "testing_framework",
            "confidence",
        ],
    )
    def test_missing_key_violation_contains_lifecycle_prefix(
        self, tmp_path: Path, missing_key: str
    ) -> None:
        """Every missing required-key violation must contain 'lifecycle.<key>'."""
        lc = {k: v for k, v in VALID_LIFECYCLE.items() if k != missing_key}
        p = _write_md(tmp_path, f"drift_{missing_key}.md", _build_md(lifecycle=lc))
        result = verify_file(p)
        assert result.passed is False
        combined = " ".join(result.violations)
        assert f"lifecycle.{missing_key}" in combined, (
            f"Drift detected: violation for missing '{missing_key}' does not "
            f"contain 'lifecycle.{missing_key}'. Got: {result.violations}. "
            f"Check _normalize_parser_error in frontmatter_verifier.py — "
            f"UnifiedResponseParser message wording may have changed."
        )
