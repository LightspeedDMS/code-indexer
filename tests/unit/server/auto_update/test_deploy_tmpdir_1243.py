"""
Tests for Bug #1243: Auto-updater pip install dead-loops under py3.12 + systemd PrivateTmp.

Root cause: Under PrivateTmp=yes, sudo'd pip's tempfile.gettempdir() finds no usable
temp dir (/tmp isolated by PrivateTmp, /var/tmp also isolated, CWD / not writable,
no TMPDIR env var) -> FileNotFoundError -> hnswlib build fails every retry ->
self-perpetuating deadlock (identical class to Bug #1182).

Fix:
1. _deploy_tmpdir() returns a writable dir under _cidx_data_dir (NOT /tmp).
2. Every `sudo pip install` command becomes:
       ["sudo", "env", f"TMPDIR={deploy_tmpdir}", python, "-m", "pip", "install", ...]
   `env` passes TMPDIR to the pip process through sudo's env_reset (which would strip
   inherited env vars). Validated fix: sudo env TMPDIR=/home/.../.tmp python3 -m pip
   install --break-system-packages pybind11 -> rc=0 on the affected py3.12 staging node.

Applied to:
- pybind11 install in build_custom_hnswlib()
- hnswlib --force-reinstall install in build_custom_hnswlib()
- pip install -e . in pip_install()
- All --break-system-packages belt-and-suspenders retry variants of the above.

The pip --version PROBE (in _pip_supports_break_system_packages) does NOT need TMPDIR
(no temp file usage) and is left unchanged.

Mocking strategy: subprocess.run is the only external boundary mocked.
Module-level _cidx_data_dir is patched via patch.object to keep filesystem writes
inside pytest tmp_path. No SUT methods are patched.

Regression: existing tests in test_pip_break_system_packages_1234.py must continue
to pass (their dispatch functions are TMPDIR-agnostic — they check for pybind11,
--force-reinstall, -e, --break-system-packages tokens which remain unchanged).
"""

import sys as _sys
from pathlib import Path
from typing import Optional
from unittest.mock import Mock, patch

import pytest

import code_indexer.server.auto_update.deployment_executor as _de_mod
from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def executor(tmp_path: Path) -> DeploymentExecutor:
    """DeploymentExecutor with a temp repo path."""
    return DeploymentExecutor(
        repo_path=tmp_path,
        branch="master",
        service_name="cidx-server",
    )


@pytest.fixture()
def patched_data_dir(tmp_path: Path) -> Path:  # type: ignore[misc]
    """Patch _cidx_data_dir to tmp_path/.cidx-server so _deploy_tmpdir() writes inside tmp_path."""
    data_dir = tmp_path / ".cidx-server"
    with patch.object(_de_mod, "_cidx_data_dir", data_dir):
        yield data_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_subprocess_dispatch(pip_version: str = "23.1"):  # type: ignore[return]
    """Standard subprocess.run side_effect for pip-install tests.

    Handles:
    - sudo cat <service_file>     -> minimal ExecStart with sys.executable
    - which g++                   -> found (skip dnf/yum)
    - pip --version probe         -> configurable pip version string
    - all other calls             -> returncode=0 (success)
    """

    def dispatch(cmd: list, **_kw: object) -> Mock:
        # _get_server_python: sudo cat /etc/systemd/system/cidx-server.service
        if cmd[0] == "sudo" and "cat" in cmd:
            return Mock(
                returncode=0,
                stdout=f"[Service]\nExecStart={_sys.executable} -m code_indexer\n",
                stderr="",
            )
        # _ensure_build_dependencies: which g++
        if cmd == ["which", "g++"]:
            return Mock(returncode=0, stdout="/usr/bin/g++\n", stderr="")
        # pip --version probe (probe does NOT carry env TMPDIR — unchanged by fix)
        if "-m" in cmd and "pip" in cmd and "--version" in cmd:
            return Mock(
                returncode=0,
                stdout=f"pip {pip_version} from /path (python 3.9)\n",
                stderr="",
            )
        # everything else succeeds
        return Mock(returncode=0, stderr="", stdout="")

    return dispatch


def _assert_sudo_env_tmpdir(
    cmd: list,
    *,
    label: str,
    expected_data_dir: Optional[Path] = None,
) -> None:
    """Assert cmd has the mandatory sudo env TMPDIR=<path> prefix (Bug #1243).

    Expected shape:
        ["sudo", "env", "TMPDIR=<path-under-_cidx_data_dir>", <python>, "-m", "pip", "install", ...]

    Args:
        cmd: The command list to inspect.
        label: Human-readable label for assertion messages.
        expected_data_dir: When provided, asserts that the TMPDIR value starts with this
            path (verifying it is derived from _cidx_data_dir, not hardcoded to /tmp).
            In production _cidx_data_dir is ~/.cidx-server which is never under /tmp.
    """
    assert len(cmd) >= 4, f"[{label}] Command too short to carry TMPDIR prefix: {cmd}"
    assert cmd[0] == "sudo", f"[{label}] cmd[0] must be 'sudo', got: {cmd}"
    assert cmd[1] == "env", (
        f"[{label}] cmd[1] must be 'env' (TMPDIR passthrough via sudo); got: {cmd}"
    )
    tmpdir_tokens = [t for t in cmd if t.startswith("TMPDIR=")]
    assert tmpdir_tokens, (
        f"[{label}] Command must contain a 'TMPDIR=<path>' token; got: {cmd}"
    )
    # Verify the TMPDIR token is immediately at position 2 (right after "env")
    assert cmd[2].startswith("TMPDIR="), (
        f"[{label}] 'TMPDIR=...' must be at cmd[2] (immediately after 'env'); got: {cmd}"
    )
    tmpdir_value = cmd[2][len("TMPDIR=") :]
    assert ".deploy-tmp" in tmpdir_value, (
        f"[{label}] TMPDIR must contain '.deploy-tmp' (deploy-specific subdir); got: {tmpdir_value}"
    )
    if expected_data_dir is not None:
        assert tmpdir_value.startswith(str(expected_data_dir)), (
            f"[{label}] TMPDIR must be derived from _cidx_data_dir ({expected_data_dir}), "
            f"not hardcoded to system /tmp; got: {tmpdir_value}"
        )


# ---------------------------------------------------------------------------
# _deploy_tmpdir() helper method
# ---------------------------------------------------------------------------


class TestDeployTmpdir:
    """Unit tests for the _deploy_tmpdir() helper method added by Bug #1243."""

    def test_method_exists(self, executor: DeploymentExecutor) -> None:
        """_deploy_tmpdir() must exist on DeploymentExecutor."""
        assert hasattr(executor, "_deploy_tmpdir"), (
            "_deploy_tmpdir() is missing from DeploymentExecutor — required by Bug #1243"
        )

    def test_returns_str(
        self, executor: DeploymentExecutor, patched_data_dir: Path
    ) -> None:
        """_deploy_tmpdir() must return a str (for use in f-string command tokens)."""
        result = executor._deploy_tmpdir()  # type: ignore[attr-defined]
        assert isinstance(result, str), f"Expected str, got {type(result)}: {result!r}"

    def test_not_under_system_tmp(
        self, executor: DeploymentExecutor, patched_data_dir: Path
    ) -> None:
        """Deploy tmpdir must be derived from _cidx_data_dir, not hardcoded to /tmp.

        In production _cidx_data_dir is ~/.cidx-server (never /tmp or /var/tmp).
        We verify derivation by checking that the returned path starts with the
        patched _cidx_data_dir value. This is the invariant that matters: the tmpdir
        follows _cidx_data_dir, not a hardcoded system-temp location.
        """
        tmpdir = executor._deploy_tmpdir()  # type: ignore[attr-defined]
        assert tmpdir.startswith(str(patched_data_dir)), (
            f"Deploy tmpdir must be derived from _cidx_data_dir ({patched_data_dir}), "
            f"not from a hardcoded system temp path; got: {tmpdir}"
        )

    def test_uses_cidx_data_dir(
        self, executor: DeploymentExecutor, patched_data_dir: Path
    ) -> None:
        """Deploy tmpdir must be rooted under _cidx_data_dir."""
        tmpdir = executor._deploy_tmpdir()  # type: ignore[attr-defined]
        assert str(patched_data_dir) in tmpdir, (
            f"Deploy tmpdir must be under _cidx_data_dir ({patched_data_dir}); got: {tmpdir}"
        )

    def test_named_deploy_tmp(
        self, executor: DeploymentExecutor, patched_data_dir: Path
    ) -> None:
        """Deploy tmpdir path must contain '.deploy-tmp' for traceability."""
        tmpdir = executor._deploy_tmpdir()  # type: ignore[attr-defined]
        assert ".deploy-tmp" in tmpdir, f"Expected '.deploy-tmp' in path; got: {tmpdir}"

    def test_creates_directory(
        self, executor: DeploymentExecutor, patched_data_dir: Path
    ) -> None:
        """_deploy_tmpdir() must mkdir the directory before returning."""
        tmpdir = executor._deploy_tmpdir()  # type: ignore[attr-defined]
        p = Path(tmpdir)
        assert p.exists(), (
            f"_deploy_tmpdir() must create the directory (mkdir parents exist_ok); "
            f"{tmpdir} does not exist"
        )
        assert p.is_dir(), f"Expected a directory at {tmpdir}"

    def test_idempotent_if_already_exists(
        self, executor: DeploymentExecutor, patched_data_dir: Path
    ) -> None:
        """Calling _deploy_tmpdir() twice must not raise (exist_ok=True required)."""
        tmpdir1 = executor._deploy_tmpdir()  # type: ignore[attr-defined]
        tmpdir2 = executor._deploy_tmpdir()  # type: ignore[attr-defined]
        assert tmpdir1 == tmpdir2, "Must return same path on repeated calls"


# ---------------------------------------------------------------------------
# pybind11 install carries sudo env TMPDIR
# ---------------------------------------------------------------------------


class TestPybind11InstallCarriesTmpdir:
    """pybind11 pip install in build_custom_hnswlib() must carry TMPDIR prefix."""

    def test_pybind11_command_shape(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
    ) -> None:
        """pybind11 install must be: sudo env TMPDIR=<dir> python -m pip install ..."""
        hnswlib_path = tmp_path / "third_party" / "hnswlib"
        hnswlib_path.mkdir(parents=True)
        (hnswlib_path / "setup.py").write_text("# setup")

        calls: list = []
        base = _make_subprocess_dispatch(pip_version="23.1")

        def dispatch(cmd: list, **kw: object) -> Mock:
            calls.append(list(cmd))
            return base(cmd, **kw)  # type: ignore[no-any-return]

        with patch("subprocess.run", side_effect=dispatch):
            executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        pybind11_calls = [c for c in calls if "pybind11" in c]
        assert pybind11_calls, (
            f"Expected at least one pybind11 install call; all calls: {calls}"
        )
        for call in pybind11_calls:
            _assert_sudo_env_tmpdir(call, label="pybind11 install")

    def test_pybind11_retry_without_flag_also_carries_tmpdir(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
    ) -> None:
        """pybind11 retry (--break-system-packages stripped) must still carry TMPDIR."""
        hnswlib_path = tmp_path / "third_party" / "hnswlib"
        hnswlib_path.mkdir(parents=True)
        (hnswlib_path / "setup.py").write_text("# setup")

        calls: list = []
        first_pybind_fail: list = []

        def dispatch(cmd: list, **_kw: object) -> Mock:
            calls.append(list(cmd))
            if cmd[0] == "sudo" and "cat" in cmd:
                return Mock(
                    returncode=0,
                    stdout=f"[Service]\nExecStart={_sys.executable} -m code_indexer\n",
                    stderr="",
                )
            if cmd == ["which", "g++"]:
                return Mock(returncode=0, stdout="/usr/bin/g++\n", stderr="")
            if "-m" in cmd and "pip" in cmd and "--version" in cmd:
                return Mock(
                    returncode=0, stdout="pip 23.1 from /path (python 3.9)\n", stderr=""
                )
            # Fail first pybind11 install with --break-system-packages flag
            if (
                "pybind11" in cmd
                and "--break-system-packages" in cmd
                and not first_pybind_fail
            ):
                first_pybind_fail.append(True)
                return Mock(
                    returncode=1,
                    stderr="no such option: --break-system-packages",
                    stdout="",
                )
            return Mock(returncode=0, stderr="", stdout="")

        with patch("subprocess.run", side_effect=dispatch):
            result = executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        assert result is True, "build_custom_hnswlib must succeed after pybind11 retry"
        retry_calls = [
            c for c in calls if "pybind11" in c and "--break-system-packages" not in c
        ]
        assert retry_calls, (
            "Expected a pybind11 retry call without --break-system-packages"
        )
        for call in retry_calls:
            _assert_sudo_env_tmpdir(call, label="pybind11 retry (no flag)")


# ---------------------------------------------------------------------------
# hnswlib install carries sudo env TMPDIR
# ---------------------------------------------------------------------------


class TestHnswlibInstallCarriesTmpdir:
    """hnswlib pip install in build_custom_hnswlib() must carry TMPDIR prefix."""

    def test_hnswlib_command_shape(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
    ) -> None:
        """hnswlib install must be: sudo env TMPDIR=<dir> python -m pip install ..."""
        hnswlib_path = tmp_path / "third_party" / "hnswlib"
        hnswlib_path.mkdir(parents=True)
        (hnswlib_path / "setup.py").write_text("# setup")

        calls: list = []
        base = _make_subprocess_dispatch(pip_version="23.1")

        def dispatch(cmd: list, **kw: object) -> Mock:
            calls.append(list(cmd))
            return base(cmd, **kw)  # type: ignore[no-any-return]

        with patch("subprocess.run", side_effect=dispatch):
            executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        hnswlib_calls = [c for c in calls if "--force-reinstall" in c]
        assert hnswlib_calls, (
            f"Expected at least one hnswlib install call (--force-reinstall); all calls: {calls}"
        )
        for call in hnswlib_calls:
            _assert_sudo_env_tmpdir(call, label="hnswlib install")

    def test_hnswlib_retry_without_flag_also_carries_tmpdir(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
    ) -> None:
        """hnswlib retry (--break-system-packages stripped) must still carry TMPDIR."""
        hnswlib_path = tmp_path / "third_party" / "hnswlib"
        hnswlib_path.mkdir(parents=True)
        (hnswlib_path / "setup.py").write_text("# setup")

        calls: list = []
        first_hnswlib_fail: list = []

        def dispatch(cmd: list, **_kw: object) -> Mock:
            calls.append(list(cmd))
            if cmd[0] == "sudo" and "cat" in cmd:
                return Mock(
                    returncode=0,
                    stdout=f"[Service]\nExecStart={_sys.executable} -m code_indexer\n",
                    stderr="",
                )
            if cmd == ["which", "g++"]:
                return Mock(returncode=0, stdout="/usr/bin/g++\n", stderr="")
            if "-m" in cmd and "pip" in cmd and "--version" in cmd:
                return Mock(
                    returncode=0, stdout="pip 23.1 from /path (python 3.9)\n", stderr=""
                )
            # Fail first hnswlib install (--force-reinstall) with flag
            if (
                "--force-reinstall" in cmd
                and "--break-system-packages" in cmd
                and not first_hnswlib_fail
            ):
                first_hnswlib_fail.append(True)
                return Mock(
                    returncode=1,
                    stderr="no such option: --break-system-packages",
                    stdout="",
                )
            return Mock(returncode=0, stderr="", stdout="")

        with patch("subprocess.run", side_effect=dispatch):
            result = executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        assert result is True, "build_custom_hnswlib must succeed after hnswlib retry"
        retry_calls = [
            c
            for c in calls
            if "--force-reinstall" in c and "--break-system-packages" not in c
        ]
        assert retry_calls, (
            "Expected hnswlib retry call without --break-system-packages"
        )
        for call in retry_calls:
            _assert_sudo_env_tmpdir(call, label="hnswlib retry (no flag)")


# ---------------------------------------------------------------------------
# pip install -e . carries sudo env TMPDIR
# ---------------------------------------------------------------------------


class TestPipInstallCarriesTmpdir:
    """pip_install() must carry sudo env TMPDIR=<deploy-tmp> prefix."""

    def test_pip_install_command_shape(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
    ) -> None:
        """pip_install() command must be: sudo env TMPDIR=<dir> python -m pip install -e ."""
        calls: list = []
        base = _make_subprocess_dispatch(pip_version="23.1")

        def dispatch(cmd: list, **kw: object) -> Mock:
            calls.append(list(cmd))
            return base(cmd, **kw)  # type: ignore[no-any-return]

        with patch("subprocess.run", side_effect=dispatch):
            executor.pip_install()

        install_calls = [c for c in calls if "-e" in c]
        assert install_calls, f"Expected pip install -e . call; all calls: {calls}"
        for call in install_calls:
            _assert_sudo_env_tmpdir(call, label="pip install -e .")

    def test_pip_install_retry_without_flag_also_carries_tmpdir(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
    ) -> None:
        """pip_install() retry (--break-system-packages stripped) must still carry TMPDIR."""
        calls: list = []
        first_fail: list = []

        def dispatch(cmd: list, **_kw: object) -> Mock:
            calls.append(list(cmd))
            if cmd[0] == "sudo" and "cat" in cmd:
                return Mock(
                    returncode=0,
                    stdout=f"[Service]\nExecStart={_sys.executable} -m code_indexer\n",
                    stderr="",
                )
            if "-m" in cmd and "pip" in cmd and "--version" in cmd:
                return Mock(
                    returncode=0, stdout="pip 23.1 from /path (python 3.9)\n", stderr=""
                )
            # Fail first pip install -e with flag
            if "-e" in cmd and "--break-system-packages" in cmd and not first_fail:
                first_fail.append(True)
                return Mock(
                    returncode=1,
                    stderr="no such option: --break-system-packages",
                    stdout="",
                )
            return Mock(returncode=0, stderr="", stdout="")

        with patch("subprocess.run", side_effect=dispatch):
            result = executor.pip_install()

        assert result is True, "pip_install must succeed after retry"
        retry_calls = [
            c for c in calls if "-e" in c and "--break-system-packages" not in c
        ]
        assert retry_calls, (
            "Expected pip install retry call without --break-system-packages"
        )
        for call in retry_calls:
            _assert_sudo_env_tmpdir(call, label="pip install -e . retry (no flag)")


# ---------------------------------------------------------------------------
# --break-system-packages preserved alongside TMPDIR (Bug #1234 regression)
# ---------------------------------------------------------------------------


class TestBreakSystemPackagesPreservedWithTmpdir:
    """Bug #1234 flag-handling must be preserved after adding the TMPDIR prefix."""

    def test_flag_present_when_pip_ge23_pip_install(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
    ) -> None:
        """With pip>=23, --break-system-packages must still appear in pip_install() command."""
        calls: list = []
        base = _make_subprocess_dispatch(pip_version="23.1")

        def dispatch(cmd: list, **kw: object) -> Mock:
            calls.append(list(cmd))
            return base(cmd, **kw)  # type: ignore[no-any-return]

        with patch("subprocess.run", side_effect=dispatch):
            result = executor.pip_install()

        assert result is True
        install_calls = [c for c in calls if "-e" in c]
        assert install_calls, "Expected pip install -e . call"
        assert "--break-system-packages" in install_calls[0], (
            "--break-system-packages must be present when pip>=23 (Bug #1234 regression)"
        )
        # AND the TMPDIR fix must also be applied
        _assert_sudo_env_tmpdir(install_calls[0], label="pip>=23 install with TMPDIR")

    def test_flag_absent_when_pip_lt23_pip_install(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
    ) -> None:
        """With pip<23, --break-system-packages must NOT appear, but TMPDIR must still be set."""
        calls: list = []
        base = _make_subprocess_dispatch(pip_version="21.3.1")

        def dispatch(cmd: list, **kw: object) -> Mock:
            calls.append(list(cmd))
            return base(cmd, **kw)  # type: ignore[no-any-return]

        with patch("subprocess.run", side_effect=dispatch):
            result = executor.pip_install()

        assert result is True
        install_calls = [c for c in calls if "-e" in c]
        assert install_calls, "Expected pip install -e . call"
        assert "--break-system-packages" not in install_calls[0], (
            "--break-system-packages must NOT be present when pip<23 (Bug #1234)"
        )
        # TMPDIR fix must still apply even without the flag
        _assert_sudo_env_tmpdir(install_calls[0], label="pip<23 install with TMPDIR")

    def test_flag_present_in_build_hnswlib_when_pip_ge23(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
    ) -> None:
        """build_custom_hnswlib: pip>=23 -> flag in both pybind11 and hnswlib installs."""
        hnswlib_path = tmp_path / "third_party" / "hnswlib"
        hnswlib_path.mkdir(parents=True)
        (hnswlib_path / "setup.py").write_text("# setup")

        calls: list = []
        base = _make_subprocess_dispatch(pip_version="23.1")

        def dispatch(cmd: list, **kw: object) -> Mock:
            calls.append(list(cmd))
            return base(cmd, **kw)  # type: ignore[no-any-return]

        with patch("subprocess.run", side_effect=dispatch):
            result = executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        assert result is True
        pybind11_calls = [c for c in calls if "pybind11" in c]
        assert pybind11_calls, "Expected pybind11 install call"
        assert "--break-system-packages" in pybind11_calls[0], (
            "--break-system-packages must be in pybind11 install when pip>=23"
        )
        _assert_sudo_env_tmpdir(pybind11_calls[0], label="pybind11 pip>=23")

        hnswlib_calls = [c for c in calls if "--force-reinstall" in c]
        assert hnswlib_calls, "Expected hnswlib install call"
        assert "--break-system-packages" in hnswlib_calls[0], (
            "--break-system-packages must be in hnswlib install when pip>=23"
        )
        _assert_sudo_env_tmpdir(hnswlib_calls[0], label="hnswlib pip>=23")

    def test_flag_absent_in_build_hnswlib_when_pip_lt23(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
    ) -> None:
        """build_custom_hnswlib: pip<23 -> no flag, but TMPDIR must still be set."""
        hnswlib_path = tmp_path / "third_party" / "hnswlib"
        hnswlib_path.mkdir(parents=True)
        (hnswlib_path / "setup.py").write_text("# setup")

        calls: list = []
        base = _make_subprocess_dispatch(pip_version="21.3.1")

        def dispatch(cmd: list, **kw: object) -> Mock:
            calls.append(list(cmd))
            return base(cmd, **kw)  # type: ignore[no-any-return]

        with patch("subprocess.run", side_effect=dispatch):
            result = executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        assert result is True
        pybind11_calls = [c for c in calls if "pybind11" in c]
        assert pybind11_calls, "Expected pybind11 install call"
        assert "--break-system-packages" not in pybind11_calls[0], (
            "--break-system-packages must NOT be in pybind11 install when pip<23"
        )
        _assert_sudo_env_tmpdir(pybind11_calls[0], label="pybind11 pip<23 with TMPDIR")

        hnswlib_calls = [c for c in calls if "--force-reinstall" in c]
        assert hnswlib_calls, "Expected hnswlib install call"
        assert "--break-system-packages" not in hnswlib_calls[0], (
            "--break-system-packages must NOT be in hnswlib install when pip<23"
        )
        _assert_sudo_env_tmpdir(hnswlib_calls[0], label="hnswlib pip<23 with TMPDIR")
