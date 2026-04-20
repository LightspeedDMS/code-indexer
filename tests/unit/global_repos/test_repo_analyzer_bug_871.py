"""
Bug #871: bare CSI tails (no ESC prefix) break YAML parse in _clean_claude_output.

Under ``script -q -c claude -p ...`` in production, the pseudo-TTY strips the ESC
byte from terminal control sequences, emitting bare CSI tails like ``[>4m``,
``[?25h``, ``[?1004h``, ``[0m``. These pass through all four regex passes in
the current (pre-fix) implementation and reach ``yaml.safe_load()``, causing:

    ScannerError: found character '>' that cannot start any token
    ParserError: flow sequences not allowed

182+ identical production failures since Epic #725 deploy.

Scenarios confirmed against pre-fix code:
  S1  ESC-prefixed ESC[>4m           -> PARSES  (current code handles this)
  S2  bare [>4m                      -> FAILS: ScannerError '>' (PRODUCTION CASE)
  S3  bare [?1004h                   -> FAILS: ParserError flow sequence
  S4  bare [?25h                     -> FAILS: ParserError flow sequence
  S5  bare [0m                       -> FAILS: ParserError flow sequence
  S6  wrapped ---\\n[>4m[?25h\\n...  -> FAILS: ScannerError '>'

Test classes:
  TestBareCSIScenarios           — S1-S6 each parse as valid YAML after cleaning
  TestYamlFlowSequencePreserved  — [1, 2, 3] YAML array passes through unchanged
  TestAnsiColorIntermixed        — real ANSI SGR codes intermixed with bare CSI
  TestFixtureBasedProduction     — real Claude CLI stdout fixture if file present
"""

import os

import pytest
import yaml

from code_indexer.global_repos.repo_analyzer import _clean_claude_output

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LIFECYCLE_YAML = "lifecycle:\n  status: active\n"

# Fixture captured with:
#   script -q -c "claude -p 'print exactly: [>4m\nlifecycle:\n  status: active'" /dev/null
_FIXTURE_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "fixtures",
        "claude_cli_production_output.txt",
    )
)


def _parse_cleaned(raw: str) -> dict:
    """Clean *raw* and parse the resulting YAML; raises AssertionError on failure."""
    cleaned = _clean_claude_output(raw)
    try:
        parsed = yaml.safe_load(cleaned)
    except yaml.YAMLError as exc:
        raise AssertionError(
            f"YAML parse failed after cleaning.\n"
            f"  raw={raw!r}\n"
            f"  cleaned={cleaned!r}\n"
            f"  error: {exc}"
        ) from exc
    assert isinstance(parsed, dict), (
        f"Expected dict from YAML parse, got {type(parsed).__name__}.\n"
        f"  cleaned={cleaned!r}"
    )
    return parsed


# ---------------------------------------------------------------------------
# S1-S6: All 6 production scenarios
# ---------------------------------------------------------------------------


class TestBareCSIScenarios:
    """
    All six production scenarios must parse as valid YAML after _clean_claude_output.

    S1 already passes before the fix (ESC-prefixed).
    S2-S6 are the production failures targeted by Bug #871.
    """

    def test_s1_esc_prefixed_csi_parses(self):
        """S1: ESC[>4m prefix around lifecycle YAML — already handled, must still pass."""
        raw = f"\x1b[>4m{_LIFECYCLE_YAML}\x1b[>4m"
        parsed = _parse_cleaned(raw)
        assert parsed["lifecycle"]["status"] == "active"

    def test_s2_bare_gt4m_csi_parses(self):
        """S2 (PRODUCTION CASE): bare [>4m wraps lifecycle YAML — must not cause ScannerError '>'."""
        raw = f"[>4m{_LIFECYCLE_YAML}[>4m"
        parsed = _parse_cleaned(raw)
        assert parsed["lifecycle"]["status"] == "active"

    def test_s3_bare_question_1004h_parses(self):
        """S3: bare [?1004h around lifecycle YAML — must not cause ParserError."""
        raw = f"[?1004h{_LIFECYCLE_YAML}[?1004h"
        parsed = _parse_cleaned(raw)
        assert parsed["lifecycle"]["status"] == "active"

    def test_s4_bare_question_25h_parses(self):
        """S4: bare [?25h around lifecycle YAML — must not cause ParserError."""
        raw = f"[?25h{_LIFECYCLE_YAML}[?25h"
        parsed = _parse_cleaned(raw)
        assert parsed["lifecycle"]["status"] == "active"

    def test_s5_bare_0m_parses(self):
        """S5: bare [0m around lifecycle YAML — must not cause ParserError."""
        raw = f"[0m{_LIFECYCLE_YAML}[0m"
        parsed = _parse_cleaned(raw)
        assert parsed["lifecycle"]["status"] == "active"

    def test_s6_wrapped_mixed_bare_csi_parses(self):
        """S6: YAML with frontmatter marker, bare [>4m and [?25h inline — must parse correctly."""
        raw = f"---\n[>4m[?25h\n{_LIFECYCLE_YAML}"
        parsed = _parse_cleaned(raw)
        assert parsed["lifecycle"]["status"] == "active"


# ---------------------------------------------------------------------------
# YAML flow sequence must NOT be eaten
# ---------------------------------------------------------------------------


class TestYamlFlowSequencePreserved:
    """
    The bare-CSI regex pattern must NOT over-strip legitimate YAML content.

    YAML flow sequences (``[1, 2, 3]``) start with ``[`` followed by digits and
    commas.  The CSI final-byte range is ``[@-~]`` (0x40-0x7e); digits (0x30-0x39)
    are NOT in that range, so the bare-CSI pattern cannot match ``[1, 2, 3]``.
    Tests below prove this.
    """

    def test_yaml_flow_sequence_passes_through_unchanged(self):
        """[1, 2, 3] YAML flow sequence must not be stripped by the bare-CSI regex."""
        raw = "[1, 2, 3]\n"
        cleaned = _clean_claude_output(raw)
        parsed = yaml.safe_load(cleaned)
        assert parsed == [1, 2, 3], (
            f"YAML flow sequence was incorrectly modified.\n"
            f"  raw={raw!r}\n"
            f"  cleaned={cleaned!r}\n"
            f"  parsed={parsed!r}"
        )

    def test_yaml_mapping_with_list_value_preserved(self):
        """YAML mapping whose value is a flow sequence must parse correctly after cleaning."""
        raw = "repos: [repo-a, repo-b]\nstatus: active\n"
        cleaned = _clean_claude_output(raw)
        parsed = yaml.safe_load(cleaned)
        assert parsed["repos"] == ["repo-a", "repo-b"]
        assert parsed["status"] == "active"

    def test_lifecycle_yaml_with_bare_csi_and_flow_sequence_coexist(self):
        """Bare [?25h around YAML that contains a flow sequence — both handled correctly."""
        raw = "[?25hrepos: [repo-a, repo-b]\nlifecycle:\n  status: active\n[?25h"
        cleaned = _clean_claude_output(raw)
        parsed = yaml.safe_load(cleaned)
        assert parsed["repos"] == ["repo-a", "repo-b"]
        assert parsed["lifecycle"]["status"] == "active"


# ---------------------------------------------------------------------------
# Real ANSI SGR codes intermixed with bare CSI
# ---------------------------------------------------------------------------


class TestAnsiColorIntermixed:
    """
    Existing ANSI color hardening (ESC-prefixed CSI from Bug #850) must still
    work when bare CSI sequences are intermixed in the same output.
    """

    def test_esc_prefixed_sgr_and_bare_csi_both_stripped(self):
        """ESC[0m (SGR reset) and bare [?25h in same string — both stripped, text preserved."""
        raw = f"\x1b[0m[?25h{_LIFECYCLE_YAML}\x1b[1;32m[>4m"
        result = _clean_claude_output(raw)
        assert "\x1b" not in result, f"ESC byte survived cleaning: {result!r}"
        assert "[?25h" not in result, f"Bare [?25h survived: {result!r}"
        assert "[>4m" not in result, f"Bare [>4m survived: {result!r}"
        assert "[0m" not in result, f"Bare [0m survived: {result!r}"
        assert "lifecycle" in result

    def test_esc_prefixed_sgr_and_bare_csi_yaml_parses(self):
        """Full parse test: ESC-prefixed + bare CSI in same output -> valid YAML."""
        raw = (
            "\x1b[>4m"         # ESC-prefixed (Bug #850, already fixed)
            "[>4m"             # bare (Bug #871, new fix)
            "\x1b[?25h"       # ESC-prefixed cursor hide
            "[?25h"            # bare cursor hide
            + _LIFECYCLE_YAML
            + "\x1b[0m"        # ESC-prefixed SGR reset
            + "[0m"            # bare SGR reset
        )
        parsed = _parse_cleaned(raw)
        assert parsed["lifecycle"]["status"] == "active"

    def test_esc_prefixed_color_text_preserved(self):
        """ANSI color around text — text preserved, escapes stripped, bare CSI also stripped."""
        raw = "\x1b[1;32mhello[?25h world\x1b[0m"
        result = _clean_claude_output(raw)
        assert "\x1b" not in result
        assert "[?25h" not in result
        assert "hello" in result
        assert "world" in result


# ---------------------------------------------------------------------------
# Production fixture test (skipped when fixture file absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.path.exists(_FIXTURE_PATH),
    reason=(
        "Skipping production fixture test: fixture file not found at "
        f"{_FIXTURE_PATH}. "
        "To capture it, run: "
        "script -q -c \"claude -p 'print exactly: [>4m\\nlifecycle:\\n  status: active'\" /dev/null "
        "> tests/fixtures/claude_cli_production_output.txt"
    ),
)
class TestFixtureBasedProduction:
    """
    Real Claude CLI stdout fixture proves the fix handles actual production output.

    The fixture is captured with:
        script -q -c "claude -p 'print exactly: [>4m\\nlifecycle:\\n  status: active'" /dev/null
        > tests/fixtures/claude_cli_production_output.txt

    The test is skipped when the fixture file is absent.
    """

    def test_production_output_cleans_without_error(self):
        """
        Real Claude CLI stdout (via pty) must clean without exception.

        Verified properties after cleaning:
        1. No ESC bytes (\\x1b) remain.
        2. No bare CSI tails remain — patterns matching
           ``[`` + optional private prefix (``?<>=!``) or leading digit +
           digit/semicolon params + optional intermediate + final byte in
           ``[@-~]`` are all removed.  This covers all forms observed in the
           captured fixture: ``[?1006l``, ``[>4m``, ``[<u``, ``[?25h``.
        3. The word "lifecycle" is still present (the literal text Claude echoed).
        """
        import re as _re

        with open(_FIXTURE_PATH, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()

        assert raw, "Fixture file must not be empty"

        # Must not raise
        cleaned = _clean_claude_output(raw)

        # No ESC bytes must remain
        assert "\x1b" not in cleaned, (
            f"ESC byte survived cleaning.\n  cleaned={cleaned!r}"
        )

        # No bare CSI tails must remain.
        # Pattern mirrors the bare-CSI regex in _clean_claude_output:
        # private prefix OR leading digit, followed by param bytes, optional
        # intermediate bytes, and a final byte in [@-~].
        remaining_bare_csi = _re.findall(
            r"\[(?:[?<>=!][0-9;]*|[0-9][0-9;]*)[ -/]*[@-~]",
            cleaned,
        )
        assert not remaining_bare_csi, (
            f"Bare CSI tails survived cleaning: {remaining_bare_csi}\n"
            f"  cleaned={cleaned!r}"
        )

        # The word "lifecycle" must still be present (it was in the raw content)
        assert "lifecycle" in cleaned, (
            f"'lifecycle' text lost during cleaning.\n"
            f"  raw (first 300 chars)={raw[:300]!r}\n"
            f"  cleaned={cleaned!r}"
        )
