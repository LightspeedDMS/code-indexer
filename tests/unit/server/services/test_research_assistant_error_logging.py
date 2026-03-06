"""Tests for Bug #370: research_assistant_service logs empty error message when stderr is empty.

When Claude CLI writes errors to stdout instead of stderr, line 1104 produces the
message "Claude CLI failed: " with no diagnostic info. The fix adds a fallback
chain: stderr -> stdout -> exit code.

These tests verify the fallback chain logic in isolation (the same expression used
in the production fix), ensuring all three branches produce meaningful messages.
"""


def _build_error_message(stderr: str, stdout: str, returncode: int) -> str:
    """Mirror of the fixed error extraction logic from research_assistant_service.py line 1104.

    This is the exact expression that replaces:
        error = result.stderr.strip()
    with:
        error = result.stderr.strip() or result.stdout.strip() or f"Exit code {result.returncode}"

    Defined here so the tests are explicit about what they are validating.
    """
    return stderr.strip() or stdout.strip() or f"Exit code {returncode}"


class TestErrorMessageFallbackChain:
    """Validate the three-tier fallback chain: stderr -> stdout -> exit code."""

    def test_prefers_stderr_when_available(self):
        """Bug #370: stderr is the first priority when non-empty."""
        error = _build_error_message(
            stderr="Permission denied",
            stdout="Some other output",
            returncode=1,
        )
        assert error == "Permission denied"

    def test_falls_back_to_stdout_when_stderr_empty(self):
        """Bug #370: stdout is used when stderr is empty - the primary bug scenario."""
        error = _build_error_message(
            stderr="",
            stdout="Error: authentication failed",
            returncode=1,
        )
        assert error == "Error: authentication failed"

    def test_falls_back_to_exit_code_when_both_empty(self):
        """Bug #370: exit code is used when both stderr and stdout are empty."""
        error = _build_error_message(
            stderr="",
            stdout="",
            returncode=42,
        )
        assert error == "Exit code 42"

    def test_stderr_whitespace_only_treated_as_empty(self):
        """Bug #370: whitespace-only stderr should fall through to stdout."""
        error = _build_error_message(
            stderr="   \n\t  ",
            stdout="Claude error: rate limit exceeded",
            returncode=1,
        )
        assert error == "Claude error: rate limit exceeded"

    def test_stdout_whitespace_only_falls_through_to_exit_code(self):
        """Bug #370: whitespace-only stdout should fall through to exit code."""
        error = _build_error_message(
            stderr="",
            stdout="\n\n",
            returncode=127,
        )
        assert error == "Exit code 127"

    def test_error_message_never_empty(self):
        """Bug #370: the fallback chain must always produce a non-empty string."""
        # Worst case: everything empty, returncode 0 (unusual but possible)
        error = _build_error_message(stderr="", stdout="", returncode=0)
        assert error  # Truthy - never empty
        assert error == "Exit code 0"

    def test_stderr_with_leading_trailing_whitespace_stripped(self):
        """stderr content is stripped before use."""
        error = _build_error_message(
            stderr="  connection refused  \n",
            stdout="ignored",
            returncode=1,
        )
        assert error == "connection refused"

    def test_stdout_with_leading_trailing_whitespace_stripped(self):
        """stdout content is stripped before use."""
        error = _build_error_message(
            stderr="",
            stdout="  timeout after 30s  \n",
            returncode=1,
        )
        assert error == "timeout after 30s"

    def test_exit_code_zero_when_no_other_info(self):
        """Exit code 0 in the fallback means the process exited cleanly but produced no output."""
        error = _build_error_message(stderr="", stdout="", returncode=0)
        assert error == "Exit code 0"

    def test_negative_exit_code_in_fallback(self):
        """Negative exit codes (signal kills) are represented correctly."""
        error = _build_error_message(stderr="", stdout="", returncode=-9)
        assert error == "Exit code -9"
