"""
Tests for Bug #1234: Auto-update hnswlib build assumes pip>=23 (--break-system-packages).

Root cause: DeploymentExecutor.build_custom_hnswlib() and pip_install() pass
--break-system-packages to pip unconditionally. On stock Rocky 9 the system pip
is 21.3.1 which does not support that flag, causing:
  "no such option: --break-system-packages"
  [DEPLOY-GENERAL-047] pybind11 installation failed
  [DEPLOY-GENERAL-044] Deployment failed at custom hnswlib build step

Fix: Extract a helper `_pip_supports_break_system_packages(python_path)` that
probes pip version and returns True only when pip >= 23.0.1.  All pip install
invocations in deployment_executor.py that currently pass --break-system-packages
must use this conditional.

Mocking strategy: subprocess.run is the only external boundary mocked.
No internal SUT methods are patched. Tests focus on the helper's decision logic
since it is the pure behaviour being verified for Bug #1234.
"""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


@pytest.fixture()
def executor(tmp_path: Path) -> DeploymentExecutor:
    """Create a DeploymentExecutor with a temp repo path."""
    return DeploymentExecutor(
        repo_path=tmp_path,
        branch="master",
        service_name="cidx-server",
    )


def _pip_version_result(version_str: str) -> Mock:
    """Build a subprocess.run return value that looks like `pip --version` output."""
    return Mock(
        returncode=0,
        stdout=(
            f"pip {version_str} from /usr/local/lib/python3/dist-packages/pip"
            " (python 3.9)\n"
        ),
        stderr="",
    )


class TestPipSupportsBreakSystemPackages:
    """Unit tests for the pip-capability probe helper.

    The helper is the single source of truth for whether --break-system-packages
    should be included.  Its logic only requires subprocess.run, so that is the
    only external boundary mocked.
    """

    def test_helper_exists(self, executor: DeploymentExecutor) -> None:
        """Helper must be present on DeploymentExecutor."""
        assert hasattr(executor, "_pip_supports_break_system_packages"), (
            "_pip_supports_break_system_packages() is missing — needed for Bug #1234"
        )

    @pytest.mark.parametrize(
        "pip_version, expected",
        [
            # Below threshold — flag not supported
            ("21.3.1", False),  # Stock Rocky 9 system pip
            ("22.0", False),
            ("22.3.1", False),
            ("23.0.0", False),  # One patch below exact threshold
            # At or above threshold — flag supported
            ("23.0.1", True),  # Exact minimum
            ("23.1", True),
            ("23.2.1", True),
            ("24.0", True),
            ("25.0.0", True),
        ],
    )
    def test_version_threshold(
        self,
        executor: DeploymentExecutor,
        pip_version: str,
        expected: bool,
    ) -> None:
        """Version threshold: <23.0.1 -> False, >=23.0.1 -> True."""
        with patch("subprocess.run", return_value=_pip_version_result(pip_version)):
            result = executor._pip_supports_break_system_packages("/usr/bin/python3")
        assert result is expected, (
            f"pip {pip_version}: expected {expected}, got {result}"
        )

    def test_subprocess_failure_returns_false(
        self, executor: DeploymentExecutor
    ) -> None:
        """Non-zero returncode from pip --version -> conservatively return False."""
        with patch(
            "subprocess.run",
            return_value=Mock(returncode=1, stdout="", stderr="command not found"),
        ):
            result = executor._pip_supports_break_system_packages("/usr/bin/python3")
        assert result is False

    def test_subprocess_exception_returns_false(
        self, executor: DeploymentExecutor
    ) -> None:
        """subprocess.run raises -> conservatively return False."""
        with patch("subprocess.run", side_effect=OSError("no such file")):
            result = executor._pip_supports_break_system_packages("/usr/bin/python3")
        assert result is False

    def test_malformed_output_returns_false(self, executor: DeploymentExecutor) -> None:
        """Unparseable pip --version output -> return False."""
        with patch(
            "subprocess.run",
            return_value=Mock(returncode=0, stdout="unexpected output\n", stderr=""),
        ):
            result = executor._pip_supports_break_system_packages("/usr/bin/python3")
        assert result is False

    def test_result_is_bool_not_truthy(self, executor: DeploymentExecutor) -> None:
        """Helper must return exactly True or False, not a truthy/falsy value."""
        with patch("subprocess.run", return_value=_pip_version_result("23.1")):
            result = executor._pip_supports_break_system_packages("/usr/bin/python3")
        assert result is True  # `is True` not just `== True`

        with patch("subprocess.run", return_value=_pip_version_result("21.3.1")):
            result = executor._pip_supports_break_system_packages("/usr/bin/python3")
        assert result is False  # `is False` not just `== False`
