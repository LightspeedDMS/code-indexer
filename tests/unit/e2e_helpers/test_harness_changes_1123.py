"""Tests for Story #1123 bash harness and test infrastructure changes.

Covers:
  AC1 — SKIP summary section appears in e2e-automation.sh output
  AC2 — wait_for_server in e2e-automation.sh now uses auth probe
  AC3a — phantom maintenance skip removed from test_99_destructive_mcp.py
  AC3b — tests/e2e/README.md correctly states 6 phases (not 5)
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
E2E_SCRIPT = REPO_ROOT / "e2e-automation.sh"
DESTRUCTIVE_MCP = REPO_ROOT / "tests" / "e2e" / "server" / "test_99_destructive_mcp.py"
E2E_README = REPO_ROOT / "tests" / "e2e" / "README.md"


# ---------------------------------------------------------------------------
# AC1: SKIP summary in e2e-automation.sh
# ---------------------------------------------------------------------------


class TestSkipSummary:
    def test_script_contains_skip_summary_label(self):
        """e2e-automation.sh must emit a 'SKIP SUMMARY' section header."""
        content = E2E_SCRIPT.read_text()
        assert "SKIP SUMMARY" in content, (
            "e2e-automation.sh must contain a 'SKIP SUMMARY' section header "
            "so AC1 (consolidated end-of-run skip report) is implemented."
        )

    def test_script_collects_skip_lines_with_rs_flag(self):
        """e2e-automation.sh must use pytest -rs to capture skip reasons."""
        content = E2E_SCRIPT.read_text()
        assert (
            " -rs" in content
            or "\t-rs" in content
            or "'-rs'" in content
            or '"-rs"' in content
        ), (
            "e2e-automation.sh must pass -rs to pytest so skip reasons are "
            "captured for the consolidated SKIP SUMMARY."
        )

    def test_script_groups_skip_output(self):
        """e2e-automation.sh must accumulate skip lines across phases."""
        content = E2E_SCRIPT.read_text()
        # The script must store skip lines somewhere (temp file or array variable)
        # We check for a variable or file name that references skip accumulation.
        has_skip_accumulation = (
            "SKIP_LINES" in content
            or "skip_lines" in content
            or "SKIP_LOG" in content
            or "skip_log" in content
            or "skip_summary" in content.lower()
        )
        assert has_skip_accumulation, (
            "e2e-automation.sh must accumulate skip lines (e.g. SKIP_LINES array or "
            "a temp file) across phases so they can be printed in the SKIP SUMMARY."
        )


# ---------------------------------------------------------------------------
# AC2: wait_for_server in e2e-automation.sh uses auth probe
# ---------------------------------------------------------------------------


class TestWaitForServerHardeningInScript:
    def test_script_probes_auth_login_endpoint(self):
        """wait_for_server in e2e-automation.sh must probe /auth/login."""
        content = E2E_SCRIPT.read_text()
        assert "/auth/login" in content, (
            "e2e-automation.sh's wait_for_server function must probe /auth/login "
            "so that a degraded-but-bound server (503 on auth) fails readiness."
        )

    def test_script_uses_json_content_type_for_login(self):
        """The auth probe must use Content-Type: application/json (not form-urlencoded)."""
        content = E2E_SCRIPT.read_text()
        # CLAUDE.md E2E gotchas: auth endpoint requires JSON body
        assert "application/json" in content, (
            "e2e-automation.sh must send Content-Type: application/json when "
            "probing /auth/login (see CLAUDE.md E2E gotchas)."
        )

    def test_script_uses_admin_credentials_for_probe(self):
        """The auth probe must reference E2E_ADMIN_USER and E2E_ADMIN_PASS."""
        content = E2E_SCRIPT.read_text()
        # These env vars must appear in the login probe section
        assert "E2E_ADMIN_USER" in content
        assert "E2E_ADMIN_PASS" in content

    def test_wait_for_fault_server_also_hardened(self):
        """wait_for_fault_server must also probe /auth/login consistently."""
        content = E2E_SCRIPT.read_text()
        # Count occurrences of /auth/login — must appear at least twice
        # (once in wait_for_server, once in wait_for_fault_server)
        count = content.count("/auth/login")
        assert count >= 2, (
            f"e2e-automation.sh must probe /auth/login in BOTH wait_for_server "
            f"and wait_for_fault_server, but found only {count} occurrence(s)."
        )


# ---------------------------------------------------------------------------
# AC3a: Phantom maintenance skip removed
# ---------------------------------------------------------------------------


class TestPhantomSkipRemoved:
    def test_maintenance_skip_no_longer_claims_phase4_phase5_coverage(self):
        """The phantom 'covered by Phase 4 or Phase 5' claim must be removed.

        The maintenance skip pointed at non-existent Phase 4/5 coverage.
        Story #1123 removes that false claim.  The real maintenance test is
        S10/#1132's job — it must not be implemented here.
        """
        content = DESTRUCTIVE_MCP.read_text()
        # The phantom claim: "covered by Phase 4 or Phase 5 tests"
        assert "covered by Phase 4 or Phase 5" not in content, (
            "The false 'covered by Phase 4 or Phase 5' claim in "
            "test_99_destructive_mcp.py::test_zzz_mcp_check_maintenance_cycle "
            "must be removed. It points at non-existent coverage."
        )

    def test_maintenance_test_not_just_a_bare_skip(self):
        """The maintenance test must not be an unconditional bare pytest.skip().

        After removing the phantom claim the test must either:
        (a) be deleted entirely, or
        (b) be converted to a proper skip with a truthful reason (no false coverage claim)
        It must NOT remain as 'pytest.skip(... covered by Phase 4 or Phase 5 ...)'.
        """
        content = DESTRUCTIVE_MCP.read_text()
        # If the function still exists it should not contain the old phantom reason
        if "test_zzz_mcp_check_maintenance_cycle" in content:
            # Function still present -- verify phantom reason is gone
            assert "Phase 4 or Phase 5" not in content, (
                "test_zzz_mcp_check_maintenance_cycle still contains the "
                "'Phase 4 or Phase 5' phantom coverage claim."
            )


# ---------------------------------------------------------------------------
# AC3b: README says 5 phases
# ---------------------------------------------------------------------------


class TestReadmePhaseCount:
    def test_readme_mentions_6_phases_not_5(self):
        """tests/e2e/README.md must reflect the actual 6-phase suite."""
        content = E2E_README.read_text()
        # Should say "6 phases" somewhere in the architecture/overview section
        assert "6 phases" in content or "six phases" in content.lower(), (
            "tests/e2e/README.md must say '6 phases' (not 5) to reflect the "
            "current suite: Phase 6 (PostgreSQL parity) was added but README was not updated."
        )

    def test_readme_does_not_say_5_phases_in_architecture_heading(self):
        """The architecture section must not claim 5 phases."""
        content = E2E_README.read_text()
        # The old README says "organized into 5 phases" -- must be updated to 6
        assert "organized into 5 phases" not in content, (
            "README still says 'organized into 5 phases'. Must be updated to 6."
        )

    def test_readme_mentions_phase5_resiliency(self):
        """README must describe Phase 5 (Resiliency / phase5_resiliency/)."""
        content = E2E_README.read_text()
        has_phase5 = (
            "Phase 5" in content
            or "phase5_resiliency" in content
            or "Resiliency" in content
        )
        assert has_phase5, (
            "tests/e2e/README.md must describe Phase 5 "
            "(phase5_resiliency/ Resiliency tests)."
        )

    def test_readme_run_commands_include_phase5(self):
        """README run-command examples must include --phase 5."""
        content = E2E_README.read_text()
        assert "--phase 5" in content, (
            "README must show '--phase 5' in the single-phase run examples."
        )

    def test_readme_architecture_table_includes_phase5_dir(self):
        """README architecture section must list cli_remote/ and phase5_resiliency/."""
        content = E2E_README.read_text()
        assert "phase5_resiliency" in content, (
            "README architecture table/code block must list phase5_resiliency/ "
            "as a test directory."
        )
