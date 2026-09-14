"""S0a cap and regex-driver regression floor for X-Ray (story #1789).

Two AC groups the first S0a delivery left open:

  * max_results / max_files cap boundaries (0, 1, exactly the cap, cap+1)
  * the Phase 1 regex-driver matrix (no matches, all match, case sensitivity,
    invalid regex, pcre2 off vs on, pcre2 match-limit exhaustion)

Anti-mock posture: nothing is mocked, stubbed or replaced. Every test drives
the real two-phase pipeline -- real ripgrep for Phase 1, and a real rustc
compile of a real Rust evaluator executed by the real ``xray-cli`` subprocess
for Phase 2.

Phase 2 timeout / cancellation coverage lives in
``test_s0a_engine_timeout_1789.py``.
"""

from __future__ import annotations

import pytest

from tests.unit.xray.s0a_1789_support import run_engine, write_java_files


@pytest.fixture
def s0a_search_engine():
    """Instantiate a real XRaySearchEngine, skipping if extras are absent."""
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


@pytest.fixture
def s0a_requires_xray_cli():
    """Skip when the compiled xray-cli binary is not present.

    Phase 2 is a real subprocess; without the binary these tests would assert
    against a BinaryNotFound error instead of the behaviour under test.
    """
    from code_indexer.xray.rust_backend import RustNativeBackend

    backend = RustNativeBackend()
    if not backend._xray_cli_path.exists():
        pytest.skip(
            f"xray-cli not built at {backend._xray_cli_path}; "
            "run: cd rust && cargo build --release"
        )


#: Run length of the repeated character the catastrophic-backtracking pattern
#: is pointed at. Long enough that PCRE2 exhausts its internal match limit
#: (which is what the test pins) rather than simply matching quickly.
PCRE2_MATCH_LIMIT_TRIGGER_LENGTH = 3000

# ---------------------------------------------------------------------------
# max_results / max_files cap boundaries
#
# `max_results` is the MCP-facing name; xray.py forwards it verbatim as the
# engine's `max_files`. The cap is applied as `len(candidates) > max_files`,
# so "exactly the cap" is deliberately NOT a capped run.
# ---------------------------------------------------------------------------

#: (files on disk, driver regex, cap, expected files_total,
#:  expected files_processed, expected truncation)
CAP_BOUNDARY_CASES = [
    pytest.param(
        3, "nothing_matches_this", 2, 0, 0, False, id="zero-candidates-under-cap"
    ),
    pytest.param(1, "target", 1, 1, 1, False, id="one-candidate-cap-one"),
    pytest.param(4, "target", 4, 4, 4, False, id="candidates-exactly-equal-cap"),
    pytest.param(5, "target", 4, 5, 4, True, id="one-candidate-over-cap"),
    pytest.param(6, "target", None, 6, 6, False, id="no-cap-at-all"),
]


class TestCapBoundaries:
    @pytest.mark.parametrize(
        "file_count,driver_regex,cap,expected_total,expected_processed,expect_capped",
        CAP_BOUNDARY_CASES,
    )
    def test_cap_truncation_boundary(
        self,
        s0a_search_engine,
        tmp_path,
        s0a_requires_xray_cli,
        file_count,
        driver_regex,
        cap,
        expected_total,
        expected_processed,
        expect_capped,
    ):
        write_java_files(tmp_path, file_count)

        result = run_engine(
            s0a_search_engine, tmp_path, driver_regex=driver_regex, max_files=cap
        )

        # files_total reports what Phase 1 FOUND, files_processed what Phase 2
        # actually evaluated -- the pair is what makes truncation visible.
        assert result["files_total"] == expected_total
        assert result["files_processed"] == expected_processed

        if expect_capped:
            assert result["partial"] is True
            assert result["max_files_reached"] is True
        else:
            # Not capped: neither an empty result set nor a run that exactly
            # fills the cap may be reported as truncated.
            assert "partial" not in result
            assert "max_files_reached" not in result


class TestCapValidation:
    def test_cap_of_zero_is_rejected_rather_than_silently_returning_nothing(
        self, s0a_search_engine, tmp_path
    ):
        # Kept separate from the parametrized cases above: this asserts a
        # raised exception, not a result shape.
        write_java_files(tmp_path, 2)
        with pytest.raises(ValueError, match="max_files must be > 0"):
            run_engine(s0a_search_engine, tmp_path, max_files=0)


# ---------------------------------------------------------------------------
# Phase 1 regex-driver matrix
# ---------------------------------------------------------------------------


class TestRegexDriverMatchVolume:
    def test_pattern_matching_no_files_yields_no_candidates_and_no_failure(
        self, s0a_search_engine, tmp_path, s0a_requires_xray_cli
    ):
        write_java_files(tmp_path, 3)
        result = run_engine(
            s0a_search_engine, tmp_path, driver_regex="zzz_no_such_token"
        )
        assert result["files_total"] == 0
        assert result["matches"] == []
        # "found nothing" must never be reported as a driver failure.
        assert "phase1_failed" not in result
        assert result["evaluation_errors"] == []

    def test_pattern_matching_every_file_yields_every_candidate(
        self, s0a_search_engine, tmp_path, s0a_requires_xray_cli
    ):
        write_java_files(tmp_path, 5)
        result = run_engine(s0a_search_engine, tmp_path, driver_regex="class")
        assert result["files_total"] == 5
        assert result["files_processed"] == 5
        assert "phase1_failed" not in result

    def test_case_sensitivity_is_honoured_by_the_driver(
        self, s0a_search_engine, tmp_path, s0a_requires_xray_cli
    ):
        write_java_files(tmp_path, 2)
        sensitive = run_engine(s0a_search_engine, tmp_path, driver_regex="TARGET")
        insensitive = run_engine(
            s0a_search_engine, tmp_path, driver_regex="TARGET", case_sensitive=False
        )
        assert sensitive["files_total"] == 0
        assert insensitive["files_total"] == 2


class TestRegexDriverRejectedPatterns:
    def test_invalid_regex_surfaces_phase1_failed_instead_of_empty_results(
        self, s0a_search_engine, tmp_path
    ):
        # Regression guard for the "silently empty" failure mode: an
        # unparseable pattern must be reported, never rendered as "no matches".
        write_java_files(tmp_path, 2)
        result = run_engine(s0a_search_engine, tmp_path, driver_regex="[unclosed")
        assert result["phase1_failed"] is True
        assert result["partial"] is True
        assert result["matches"] == []
        assert result["files_total"] == 0
        assert result["phase1_error"]

    def test_lookahead_without_pcre2_is_reported_as_a_driver_failure(
        self, s0a_search_engine, tmp_path
    ):
        # The default ripgrep engine has no look-around. The contract is a loud
        # phase1_failed, not a silent zero-result.
        write_java_files(tmp_path, 2)
        result = run_engine(
            s0a_search_engine, tmp_path, driver_regex="target(?=0)", pcre2=False
        )
        assert result["phase1_failed"] is True
        assert result["files_total"] == 0

    def test_pcre2_match_limit_exhaustion_is_reported_not_swallowed(
        self, s0a_search_engine, tmp_path
    ):
        # A catastrophically-backtracking pattern does not hang the driver:
        # PCRE2's own match limit trips first and ripgrep exits non-zero. That
        # must surface as phase1_failed, never as "no matches found".
        filler = "a" * PCRE2_MATCH_LIMIT_TRIGGER_LENGTH
        (tmp_path / "A.java").write_text(
            f"class A {{ void t() {{ /* {filler} */ }} }}\n"
        )
        result = run_engine(
            s0a_search_engine, tmp_path, driver_regex=r"(a+)+$", pcre2=True
        )
        assert result["phase1_failed"] is True
        assert result["files_total"] == 0
        assert "match limit" in result["phase1_error"]


class TestRegexDriverPcre2:
    def test_lookahead_with_pcre2_enabled_matches_only_the_intended_file(
        self, s0a_search_engine, tmp_path, s0a_requires_xray_cli
    ):
        # Same pattern as the pcre2=False case above: enabling pcre2 turns a
        # driver failure into a correct, narrow match.
        write_java_files(tmp_path, 3)
        result = run_engine(
            s0a_search_engine, tmp_path, driver_regex="target(?=0)", pcre2=True
        )
        assert "phase1_failed" not in result
        assert result["files_total"] == 1
        assert result["files_processed"] == 1
