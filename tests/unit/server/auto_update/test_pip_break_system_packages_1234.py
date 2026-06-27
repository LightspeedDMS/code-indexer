"""
Tests for Bug #1234: Auto-update hnswlib build assumes pip>=23 (--break-system-packages).

Root cause (original fix): DeploymentExecutor.build_custom_hnswlib() and pip_install() pass
--break-system-packages to pip unconditionally. On stock Rocky 9 the system pip
is 21.3.1 which does not support that flag, causing:
  "no such option: --break-system-packages"
  [DEPLOY-GENERAL-047] pybind11 installation failed
  [DEPLOY-GENERAL-044] Deployment failed at custom hnswlib build step

Root cause (live-VM fix): The original probe ran WITHOUT sudo; the actual installs
run WITH sudo.  On Rocky 9, non-sudo resolves the user pip (~26.x, probe True) while
sudo resolves the system pip (~21.3.1, should be False).  Probe must use the SAME
privilege context as the install.

Fix:
1. _pip_supports_break_system_packages(python_path, use_sudo=False) runs the pip
   --version probe with sudo when use_sudo=True.
2. build_custom_hnswlib and pip_install call the probe with use_sudo=True.
3. Belt-and-suspenders: if a pip install fails with "no such option" in stderr,
   retry once without --break-system-packages.

Mocking strategy: subprocess.run is the only external boundary mocked.
Calls are keyed on whether the command list starts with "sudo" to model the sudo/user
pip split.  No internal SUT methods are patched.
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

    def test_use_sudo_false_omits_sudo_prefix(
        self, executor: DeploymentExecutor
    ) -> None:
        """use_sudo=False (default) must NOT prepend sudo to the probe command."""
        captured: list = []

        def capture_run(cmd, **_kwargs):
            captured.append(list(cmd))
            return _pip_version_result("23.1")

        with patch("subprocess.run", side_effect=capture_run):
            executor._pip_supports_break_system_packages(
                "/usr/bin/python3", use_sudo=False
            )

        assert len(captured) == 1
        assert captured[0] == ["/usr/bin/python3", "-m", "pip", "--version"], (
            "use_sudo=False must run [python, -m, pip, --version] with no sudo prefix"
        )

    def test_use_sudo_true_prepends_sudo_prefix(
        self, executor: DeploymentExecutor
    ) -> None:
        """use_sudo=True must prepend sudo to the probe command."""
        captured: list = []

        def capture_run(cmd, **_kwargs):
            captured.append(list(cmd))
            return _pip_version_result("23.1")

        with patch("subprocess.run", side_effect=capture_run):
            executor._pip_supports_break_system_packages(
                "/usr/bin/python3", use_sudo=True
            )

        assert len(captured) == 1
        assert captured[0] == ["sudo", "/usr/bin/python3", "-m", "pip", "--version"], (
            "use_sudo=True must run [sudo, python, -m, pip, --version]"
        )


def _sudo_aware_pip_version(cmd, **_kwargs):
    """subprocess.run side_effect that returns different pip versions for sudo vs non-sudo.

    sudo    -> pip 21.3.1 (Rocky 9 system pip — rejects --break-system-packages)
    no sudo -> pip 26.0.1 (user/local pip — supports --break-system-packages)
    """
    if cmd[0] == "sudo":
        return Mock(
            returncode=0,
            stdout="pip 21.3.1 from /usr/lib/python3.9/site-packages/pip (python 3.9)\n",
            stderr="",
        )
    return Mock(
        returncode=0,
        stdout="pip 26.0.1 from /home/svc/.local/lib/python3.9/site-packages/pip (python 3.9)\n",
        stderr="",
    )


class TestSudoContextProbe:
    """Tests that model the Rocky 9 sudo/user pip split.

    On Rocky 9:
      - non-sudo `python3 -m pip --version` -> pip 26.0.1 (user/local)
      - sudo    `python3 -m pip --version` -> pip 21.3.1 (system)

    The probe MUST use the same privilege context as the install.
    Both build_custom_hnswlib and pip_install use sudo, so they must
    call _pip_supports_break_system_packages(..., use_sudo=True).
    """

    @pytest.fixture()
    def executor(self, tmp_path: Path) -> DeploymentExecutor:
        return DeploymentExecutor(
            repo_path=tmp_path,
            branch="master",
            service_name="cidx-server",
        )

    def test_sudo_probe_returns_false_when_system_pip_is_old(
        self, executor: DeploymentExecutor
    ) -> None:
        """With use_sudo=True and system pip 21.3.1, probe must return False.

        This is the exact Rocky 9 failure: system pip (sudo context) is 21.3.1
        which rejects --break-system-packages.
        """
        with patch("subprocess.run", side_effect=_sudo_aware_pip_version):
            result = executor._pip_supports_break_system_packages(
                "/usr/bin/python3", use_sudo=True
            )
        assert result is False, (
            "sudo probe must return False when system pip is 21.3.1 "
            "(the Rocky 9 scenario that caused production deploys to fail)"
        )

    def test_non_sudo_probe_returns_true_when_user_pip_is_new(
        self, executor: DeploymentExecutor
    ) -> None:
        """With use_sudo=False and user pip 26.0.1, probe must return True.

        Demonstrates the original bug: non-sudo probe returned True but the
        actual install used sudo (system pip = False).
        """
        with patch("subprocess.run", side_effect=_sudo_aware_pip_version):
            result = executor._pip_supports_break_system_packages(
                "/usr/bin/python3", use_sudo=False
            )
        assert result is True

    def test_sudo_and_non_sudo_disagree_on_rocky9(
        self, executor: DeploymentExecutor
    ) -> None:
        """sudo and non-sudo probes return different results on Rocky 9.

        This mismatch is the root cause: the old code probed non-sudo (True)
        but installed with sudo (system pip = should be False).
        """
        with patch("subprocess.run", side_effect=_sudo_aware_pip_version):
            sudo_result = executor._pip_supports_break_system_packages(
                "/usr/bin/python3", use_sudo=True
            )
            non_sudo_result = executor._pip_supports_break_system_packages(
                "/usr/bin/python3", use_sudo=False
            )
        assert sudo_result is False
        assert non_sudo_result is True


# ---------------------------------------------------------------------------
# Helpers shared by TestBreakSystemPackagesFallback
# ---------------------------------------------------------------------------

import sys as _sys  # noqa: E402 — needed for service-file mock


def _make_dispatch(*, fail_pattern: str, tmp_path):
    """Return a subprocess.run side_effect that:

    - Handles `sudo cat <service_file>` -> fake service file containing sys.executable
    - Handles `which g++`               -> 0 (g++ found; skip dnf/yum)
    - Handles pip --version probe       -> pip 21.3.1 (system; flag NOT supported)
    - Fails the first pip install whose command contains *fail_pattern* with
      'no such option: --break-system-packages'
    - Succeeds all other calls.
    """
    _first_fail_done: list = []

    def dispatch(cmd, **_kwargs):
        cmd_str = " ".join(cmd)

        # _get_server_python: `sudo cat /etc/systemd/system/cidx-server.service`
        if cmd[0] == "sudo" and "cat" in cmd:
            python_exe = _sys.executable
            return Mock(
                returncode=0,
                stdout=f"[Service]\nExecStart={python_exe} -m code_indexer\n",
                stderr="",
            )

        # _ensure_build_dependencies: `which g++`
        if cmd == ["which", "g++"]:
            return Mock(returncode=0, stdout="/usr/bin/g++\n", stderr="")

        # pip --version probe (with or without sudo)
        if "-m" in cmd and "pip" in cmd and "--version" in cmd:
            return Mock(
                returncode=0,
                stdout="pip 21.3.1 from /usr/lib/python3.9/site-packages/pip (python 3.9)\n",
                stderr="",
            )

        # First install matching fail_pattern WITH the flag -> fail
        if (
            fail_pattern in cmd_str
            and "--break-system-packages" in cmd
            and not _first_fail_done
        ):
            _first_fail_done.append(True)
            return Mock(
                returncode=1,
                stderr="no such option: --break-system-packages",
                stdout="",
            )

        # All other calls succeed
        return Mock(returncode=0, stderr="", stdout="")

    return dispatch


class TestBreakSystemPackagesFallback:
    """Belt-and-suspenders retry when --break-system-packages is still rejected.

    Even with the sudo-context fix a mismatch is theoretically possible.
    If pip exits non-zero with 'no such option' in stderr, retry without the flag.
    All mocking is via subprocess.run only — no SUT methods are patched.
    """

    @pytest.fixture()
    def executor(self, tmp_path: Path) -> DeploymentExecutor:
        return DeploymentExecutor(
            repo_path=tmp_path,
            branch="master",
            service_name="cidx-server",
        )

    def test_build_custom_hnswlib_retries_pybind11_without_flag_on_no_such_option(
        self, executor: DeploymentExecutor, tmp_path: Path
    ) -> None:
        """pybind11 install fails 'no such option' -> retry without flag -> success."""
        hnswlib_path = tmp_path / "third_party" / "hnswlib"
        hnswlib_path.mkdir(parents=True)
        (hnswlib_path / "setup.py").write_text("# setup")

        calls: list = []

        def recording_dispatch(cmd, **_kwargs):
            calls.append(list(cmd))
            return _make_dispatch(fail_pattern="pybind11", tmp_path=tmp_path)(
                cmd, **_kwargs
            )

        with patch("subprocess.run", side_effect=recording_dispatch):
            result = executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        assert result is True, (
            "build_custom_hnswlib must succeed by retrying pybind11 without --break-system-packages"
        )
        retry_calls = [
            c
            for c in calls
            if "pybind11" in " ".join(c) and "--break-system-packages" not in c
        ]
        assert len(retry_calls) >= 1, (
            "Expected pybind11 retry without --break-system-packages"
        )

    def test_build_custom_hnswlib_retries_hnswlib_install_without_flag_on_no_such_option(
        self, executor: DeploymentExecutor, tmp_path: Path
    ) -> None:
        """hnswlib install fails 'no such option' -> retry without flag -> success."""
        hnswlib_path = tmp_path / "third_party" / "hnswlib"
        hnswlib_path.mkdir(parents=True)
        (hnswlib_path / "setup.py").write_text("# setup")

        calls: list = []

        def recording_dispatch(cmd, **_kwargs):
            calls.append(list(cmd))
            return _make_dispatch(fail_pattern="--force-reinstall", tmp_path=tmp_path)(
                cmd, **_kwargs
            )

        with patch("subprocess.run", side_effect=recording_dispatch):
            result = executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        assert result is True, (
            "build_custom_hnswlib must succeed by retrying hnswlib install without --break-system-packages"
        )
        retry_calls = [
            c
            for c in calls
            if "--force-reinstall" in c and "--break-system-packages" not in c
        ]
        assert len(retry_calls) >= 1, (
            "Expected hnswlib retry without --break-system-packages"
        )

    def test_pip_install_retries_without_flag_on_no_such_option(
        self, executor: DeploymentExecutor, tmp_path: Path
    ) -> None:
        """pip_install: first attempt WITH flag fails 'no such option' -> retry without -> success."""
        calls: list = []

        def dispatch(cmd, **_kwargs):
            calls.append(list(cmd))
            # Service file for _get_server_python
            if cmd[0] == "sudo" and "cat" in cmd:
                python_exe = _sys.executable
                return Mock(
                    returncode=0,
                    stdout=f"[Service]\nExecStart={python_exe} -m code_indexer\n",
                    stderr="",
                )
            # pip --version probe
            if "-m" in cmd and "pip" in cmd and "--version" in cmd:
                return Mock(
                    returncode=0,
                    stdout="pip 21.3.1 from /usr/lib/python3.9/site-packages/pip (python 3.9)\n",
                    stderr="",
                )
            # First install WITH flag -> fail
            if "--break-system-packages" in cmd and "-e" in cmd:
                return Mock(
                    returncode=1,
                    stderr="no such option: --break-system-packages",
                    stdout="",
                )
            # Retry without flag -> succeed
            return Mock(returncode=0, stderr="", stdout="")

        with patch("subprocess.run", side_effect=dispatch):
            result = executor.pip_install()

        assert result is True, (
            "pip_install must succeed by retrying without --break-system-packages"
        )
        retry_calls = [
            c for c in calls if "-e" in c and "--break-system-packages" not in c
        ]
        assert len(retry_calls) >= 1, (
            "Expected pip_install retry without --break-system-packages"
        )

    def test_pip_install_does_not_retry_on_unrelated_failure(
        self, executor: DeploymentExecutor, tmp_path: Path
    ) -> None:
        """pip_install must NOT retry when failure is unrelated to --break-system-packages."""
        calls: list = []

        def dispatch(cmd, **_kwargs):
            calls.append(list(cmd))
            if cmd[0] == "sudo" and "cat" in cmd:
                python_exe = _sys.executable
                return Mock(
                    returncode=0,
                    stdout=f"[Service]\nExecStart={python_exe} -m code_indexer\n",
                    stderr="",
                )
            if "-m" in cmd and "pip" in cmd and "--version" in cmd:
                return Mock(
                    returncode=0,
                    stdout="pip 23.1 from /path (python 3.9)\n",
                    stderr="",
                )
            if "-e" in cmd:
                return Mock(
                    returncode=1, stderr="ERROR: Could not find a version", stdout=""
                )
            return Mock(returncode=0, stderr="", stdout="")

        with patch("subprocess.run", side_effect=dispatch):
            result = executor.pip_install()

        assert result is False
        install_calls = [c for c in calls if "-e" in c]
        assert len(install_calls) == 1, "Must not retry on unrelated failure"

    def test_pip_install_pip_ge23_no_retry_needed(
        self, executor: DeploymentExecutor, tmp_path: Path
    ) -> None:
        """pip>=23 in sudo context: flag kept, install succeeds first try, no retry."""
        calls: list = []

        def dispatch(cmd, **_kwargs):
            calls.append(list(cmd))
            if cmd[0] == "sudo" and "cat" in cmd:
                python_exe = _sys.executable
                return Mock(
                    returncode=0,
                    stdout=f"[Service]\nExecStart={python_exe} -m code_indexer\n",
                    stderr="",
                )
            if "-m" in cmd and "pip" in cmd and "--version" in cmd:
                return Mock(
                    returncode=0,
                    stdout="pip 23.1 from /path (python 3.9)\n",
                    stderr="",
                )
            return Mock(returncode=0, stderr="", stdout="")

        with patch("subprocess.run", side_effect=dispatch):
            result = executor.pip_install()

        assert result is True
        install_calls = [c for c in calls if "-e" in c]
        assert len(install_calls) == 1, (
            "No retry needed when pip>=23 and install succeeds"
        )
        assert "--break-system-packages" in install_calls[0], (
            "Flag must be present when pip>=23 in the sudo context"
        )
