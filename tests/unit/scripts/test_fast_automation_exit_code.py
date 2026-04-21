"""Tests for pipeline exit-code capture in fast-automation.sh.

The bug: lines 319-321 of fast-automation.sh pipe pytest output through tee,
then capture $? — which reflects tee's exit status (almost always 0), NOT
pytest's exit status. When pytest fails, PYTEST_EXIT_CODE is set to 0 and
the script prints a green SUCCESS banner, masking real failures.

The fix: replace $? with ${PIPESTATUS[0]}, which is the bash array holding
the exit code of every command in the most recent pipeline. [0] is pytest.

These tests verify:
1. The buggy pattern ($?) captures tee's exit (0) even when upstream fails.
2. The correct pattern (${PIPESTATUS[0]}) captures upstream's exit code.
3. The fixed pattern preserves 0 when upstream succeeds (no regression).
4. The SPECIFIC fix is present in fast-automation.sh source.
5. The buggy pattern is absent from fast-automation.sh source.
"""

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).parents[3]  # tests/unit/scripts -> project root
FAST_AUTOMATION = REPO_ROOT / "fast-automation.sh"


class TestPipelineExitCodeCapture:
    """Verify shell pipeline exit-code capture behaviour."""

    def test_question_mark_captures_tee_not_pytest(self) -> None:
        """$? after a pipe returns tee's exit code (0), NOT the upstream failure.

        This is the root cause of the bug: when pytest fails in a pipeline
        ending with `| tee file`, $? == 0 because tee succeeded.
        """
        result = subprocess.run(
            ["bash", "-c", "false | tee /dev/null; echo $?"],
            capture_output=True,
            text=True,
        )
        # tee exits 0, so $? is "0" — the buggy behaviour
        assert result.stdout.strip() == "0", (
            f"Expected tee's exit code (0), got: {result.stdout.strip()!r}"
        )

    def test_pipestatus_captures_upstream_failure(self) -> None:
        """${PIPESTATUS[0]} returns the upstream command's exit code, not tee's.

        This is the correct pattern: when pytest (position 0) fails with exit
        code 1, ${PIPESTATUS[0]} == 1 even though tee succeeded.
        """
        result = subprocess.run(
            ["bash", "-c", "false | tee /dev/null; echo ${PIPESTATUS[0]}"],
            capture_output=True,
            text=True,
        )
        # PIPESTATUS[0] is false's exit code == 1
        assert result.stdout.strip() == "1", (
            f"Expected upstream (false) exit code 1, got: {result.stdout.strip()!r}"
        )

    def test_pipestatus_preserves_success(self) -> None:
        """${PIPESTATUS[0]} returns 0 when upstream succeeds — no regression."""
        result = subprocess.run(
            ["bash", "-c", "true | tee /dev/null; echo ${PIPESTATUS[0]}"],
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "0", (
            f"Expected upstream (true) exit code 0, got: {result.stdout.strip()!r}"
        )


class TestFastAutomationScriptFix:
    """Verify the PIPESTATUS fix is present in fast-automation.sh."""

    def test_script_uses_pipestatus_not_plain_question_mark(self) -> None:
        """fast-automation.sh must capture PYTEST_EXIT_CODE via PIPESTATUS[0].

        The buggy line is:
            PYTEST_EXIT_CODE=$?

        The fixed line must be:
            PYTEST_EXIT_CODE=${PIPESTATUS[0]}

        We verify the fix is present by reading the script and asserting the
        correct assignment exists immediately after the tee pipeline.
        """
        assert FAST_AUTOMATION.exists(), (
            f"fast-automation.sh not found at {FAST_AUTOMATION}"
        )
        content = FAST_AUTOMATION.read_text()

        # The fixed pattern must be present
        assert "PYTEST_EXIT_CODE=${PIPESTATUS[0]}" in content, (
            "fast-automation.sh must use PYTEST_EXIT_CODE=${PIPESTATUS[0]} "
            "to capture pytest's exit code after the tee pipeline. "
            "Currently it uses $? which returns tee's exit code (always 0)."
        )

    def test_script_does_not_use_plain_question_mark_for_pytest_exit(self) -> None:
        """The buggy PYTEST_EXIT_CODE=$? pattern must NOT exist in the script.

        After the fix, the line 'PYTEST_EXIT_CODE=$?' must be gone.
        This test guards against regressions where the old pattern is
        accidentally restored.
        """
        assert FAST_AUTOMATION.exists(), (
            f"fast-automation.sh not found at {FAST_AUTOMATION}"
        )
        content = FAST_AUTOMATION.read_text()

        assert "PYTEST_EXIT_CODE=$?" not in content, (
            "fast-automation.sh still contains 'PYTEST_EXIT_CODE=$?' which "
            "captures tee's exit code (always 0) instead of pytest's. "
            "Replace it with 'PYTEST_EXIT_CODE=${PIPESTATUS[0]}'."
        )
