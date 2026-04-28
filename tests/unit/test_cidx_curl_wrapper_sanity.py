"""Sanity checks for the cidx-curl.sh wrapper script (Story #929 Item #2a).

Verifies that the wrapper file exists, is executable, passes bash syntax
validation, and that all required runtime binaries are on PATH.
"""

import os
import shutil
import subprocess

from tests.unit.test_cidx_curl_wrapper_helpers import WRAPPER

BASH_SYNTAX_CHECK_TIMEOUT = 5  # seconds — bash -n is fast; fail hard if it hangs


class TestWrapperFilePresence:
    """Verify the wrapper file itself is present and well-formed."""

    def test_wrapper_file_exists(self):
        assert WRAPPER.is_file(), f"Wrapper script missing: {WRAPPER}"

    def test_wrapper_is_executable(self):
        assert os.access(WRAPPER, os.X_OK), f"Wrapper not executable: {WRAPPER}"

    def test_wrapper_passes_bash_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(WRAPPER)],
            capture_output=True,
            text=True,
            timeout=BASH_SYNTAX_CHECK_TIMEOUT,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"


class TestWrapperRuntimeDependencies:
    """Verify that binaries the wrapper delegates to are available on PATH."""

    def test_curl_binary_available(self):
        assert shutil.which("curl") is not None, "curl not on PATH"

    def test_python3_binary_available(self):
        assert shutil.which("python3") is not None, "python3 not on PATH"
