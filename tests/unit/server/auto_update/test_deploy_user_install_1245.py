"""
Tests for Bug #1245: Auto-updater pip uses sudo unconditionally, breaking user-install layouts.

Root cause: build_custom_hnswlib() and pip_install() unconditionally run pip via sudo,
targeting /root/.local on a user-install layout (code-indexer in ~/.local/...).
On an immutable host /root/.local is read-only -> OSError -> deploy dead-loops.

Fix:
1. _is_user_install(python_path): probe code_indexer.__file__; returns True if path
   contains /.local/. Conservative: False on any failure.
2. _hnswlib_importable(python_path): probe `import hnswlib`; returns True if rc==0.
3. _get_hnswlib_submodule_commit(): git ls-files -s third_party/hnswlib; parses second token.
4. _get_last_built_hnswlib_commit(): reads _cidx_data_dir/hnswlib-last-built-commit.
5. _save_last_built_hnswlib_commit(commit): writes commit to _cidx_data_dir/hnswlib-last-built-commit.
6. build_custom_hnswlib(): skip when importable + commit unchanged; use use_sudo to pick
   command shape; non-fatal when rebuild fails but hnswlib still importable.
7. pip_install(): use use_sudo to pick command shape.

Mocking strategy:
- For probe unit tests (TestIsUserInstall, TestHnswlibImportable, etc.): patch subprocess.run.
- For integration tests (command shape, skip, non-fatal): patch.object the new helper methods
  plus _get_server_python and _ensure_build_dependencies; capture subprocess.run calls.

Invariants verified:
- Existing Bug #1243 (sudo env TMPDIR=) preserved for system install (use_sudo=True).
- Existing Bug #1234 (_pip_supports_break_system_packages probe) preserved with use_sudo.
- All new tests must FAIL before implementation and PASS after.
- MUST NOT modify any existing test files.
"""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import code_indexer.server.auto_update.deployment_executor as _de_mod
from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def executor(tmp_path: Path) -> DeploymentExecutor:
    """DeploymentExecutor with temp repo path."""
    return DeploymentExecutor(
        repo_path=tmp_path,
        branch="master",
        service_name="cidx-server",
    )


@pytest.fixture()
def patched_data_dir(tmp_path: Path) -> Path:  # type: ignore[misc]
    """Patch _cidx_data_dir to tmp_path/.cidx-server so filesystem ops stay in tmp_path."""
    data_dir = tmp_path / ".cidx-server"
    with patch.object(_de_mod, "_cidx_data_dir", data_dir):
        yield data_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_capturing_dispatch(calls: list, pip_version: str = "23.1"):
    """Return a subprocess.run side_effect that captures all calls and handles pip --version."""

    def dispatch(cmd: list, **kw: object) -> Mock:
        calls.append(list(cmd))
        if "-m" in cmd and "pip" in cmd and "--version" in cmd:
            return Mock(
                returncode=0,
                stdout=f"pip {pip_version} from /path (python 3.9)\n",
                stderr="",
            )
        return Mock(returncode=0, stdout="", stderr="")

    return dispatch


def _make_hnswlib_path(tmp_path: Path) -> Path:
    """Create a minimal third_party/hnswlib directory with setup.py."""
    hnswlib_path = tmp_path / "third_party" / "hnswlib"
    hnswlib_path.mkdir(parents=True)
    (hnswlib_path / "setup.py").write_text("# setup")
    return hnswlib_path


# ---------------------------------------------------------------------------
# TestIsUserInstall
# ---------------------------------------------------------------------------


class TestIsUserInstall:
    """Unit tests for _is_user_install() probe."""

    def test_user_install_detected_when_in_local_dir(
        self, executor: DeploymentExecutor
    ) -> None:
        """Path containing /.local/ -> user install -> True."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="/home/user/.local/lib/python3.9/site-packages/code_indexer/__init__.py\n",
                stderr="",
            )
            result = executor._is_user_install("/usr/bin/python3")  # type: ignore[attr-defined]
        assert result is True

    def test_system_install_detected_when_in_usr(
        self, executor: DeploymentExecutor
    ) -> None:
        """Path under /usr/local -> system install -> False."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="/usr/local/lib/python3.9/site-packages/code_indexer/__init__.py\n",
                stderr="",
            )
            result = executor._is_user_install("/usr/bin/python3")  # type: ignore[attr-defined]
        assert result is False

    def test_system_install_detected_when_in_opt_pipx(
        self, executor: DeploymentExecutor
    ) -> None:
        """Path under /opt/pipx -> system install -> False."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="/opt/pipx/venvs/code-indexer/lib/python3.9/site-packages/code_indexer/__init__.py\n",
                stderr="",
            )
            result = executor._is_user_install("/usr/bin/python3")  # type: ignore[attr-defined]
        assert result is False

    def test_user_install_returns_false_when_probe_rc_nonzero(
        self, executor: DeploymentExecutor
    ) -> None:
        """Probe rc=1 -> conservative False (do not omit sudo based on failed probe)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stdout="", stderr="error")
            result = executor._is_user_install("/usr/bin/python3")  # type: ignore[attr-defined]
        assert result is False

    def test_user_install_returns_false_on_empty_stdout(
        self, executor: DeploymentExecutor
    ) -> None:
        """rc=0 but empty stdout -> False (no path to inspect)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            result = executor._is_user_install("/usr/bin/python3")  # type: ignore[attr-defined]
        assert result is False

    def test_user_install_returns_false_on_exception(
        self, executor: DeploymentExecutor
    ) -> None:
        """OSError -> conservative False."""
        with patch("subprocess.run", side_effect=OSError("permission denied")):
            result = executor._is_user_install("/usr/bin/python3")  # type: ignore[attr-defined]
        assert result is False


# ---------------------------------------------------------------------------
# TestHnswlibImportable
# ---------------------------------------------------------------------------


class TestHnswlibImportable:
    """Unit tests for _hnswlib_importable() probe."""

    def test_hnswlib_importable_true_when_rc_zero(
        self, executor: DeploymentExecutor
    ) -> None:
        """rc=0 -> hnswlib can be imported -> True."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            result = executor._hnswlib_importable("/usr/bin/python3")  # type: ignore[attr-defined]
        assert result is True

    def test_hnswlib_importable_false_when_rc_nonzero(
        self, executor: DeploymentExecutor
    ) -> None:
        """rc!=0 -> hnswlib cannot be imported -> False."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=1, stdout="", stderr="No module named hnswlib"
            )
            result = executor._hnswlib_importable("/usr/bin/python3")  # type: ignore[attr-defined]
        assert result is False

    def test_hnswlib_importable_false_on_exception(
        self, executor: DeploymentExecutor
    ) -> None:
        """Exception -> False."""
        with patch("subprocess.run", side_effect=OSError("permission denied")):
            result = executor._hnswlib_importable("/usr/bin/python3")  # type: ignore[attr-defined]
        assert result is False


# ---------------------------------------------------------------------------
# TestGetHnswlibSubmoduleCommit
# ---------------------------------------------------------------------------


class TestGetHnswlibSubmoduleCommit:
    """Unit tests for _get_hnswlib_submodule_commit() parsing."""

    def test_get_submodule_commit_parses_hash(
        self, executor: DeploymentExecutor
    ) -> None:
        """Standard git ls-files -s output -> second token (commit hash) returned."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="100644 abc123def456 0\tthird_party/hnswlib\n",
                stderr="",
            )
            result = executor._get_hnswlib_submodule_commit()  # type: ignore[attr-defined]
        assert result == "abc123def456"

    def test_get_submodule_commit_returns_none_on_empty_stdout(
        self, executor: DeploymentExecutor
    ) -> None:
        """Empty stdout (submodule not initialized) -> None."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            result = executor._get_hnswlib_submodule_commit()  # type: ignore[attr-defined]
        assert result is None

    def test_get_submodule_commit_returns_none_on_rc_nonzero(
        self, executor: DeploymentExecutor
    ) -> None:
        """rc!=0 -> None."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stdout="", stderr="fatal")
            result = executor._get_hnswlib_submodule_commit()  # type: ignore[attr-defined]
        assert result is None


# ---------------------------------------------------------------------------
# TestRebuildSkip
# ---------------------------------------------------------------------------


class TestRebuildSkip:
    """Test skip logic: importable + commit unchanged -> skip rebuild."""

    def test_rebuild_skipped_when_importable_and_commit_unchanged(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """Importable + current commit == last built -> skip, return True, no pip invoked."""
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []

        def capturing_dispatch(cmd: list, **kw: object) -> Mock:
            calls.append(list(cmd))
            return Mock(returncode=0, stdout="", stderr="")

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            patch.object(executor, "_hnswlib_importable", return_value=True),
            patch.object(
                executor, "_get_hnswlib_submodule_commit", return_value="abc123"
            ),
            patch.object(
                executor, "_get_last_built_hnswlib_commit", return_value="abc123"
            ),
            patch("subprocess.run", side_effect=capturing_dispatch),
        ):
            result = executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        assert result is True
        pip_install_calls = [c for c in calls if "install" in c and "pip" in c]
        assert not pip_install_calls, (
            f"Expected no pip install calls when skip condition met; got: {pip_install_calls}"
        )

    def test_rebuild_attempted_when_submodule_commit_changed(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """Importable but commit changed -> rebuild must be attempted."""
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            patch.object(executor, "_hnswlib_importable", return_value=True),
            patch.object(
                executor, "_get_hnswlib_submodule_commit", return_value="new123"
            ),
            patch.object(
                executor, "_get_last_built_hnswlib_commit", return_value="abc123"
            ),
            patch.object(executor, "_is_user_install", return_value=False),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            result = executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        assert result is True
        pip_calls = [c for c in calls if "pybind11" in c or "--force-reinstall" in c]
        assert pip_calls, "Expected pip install calls when commit changed; none found"

    def test_rebuild_attempted_when_no_prior_build_record(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """Importable but no prior build record (last_built=None) -> rebuild must be attempted."""
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            patch.object(executor, "_hnswlib_importable", return_value=True),
            patch.object(
                executor, "_get_hnswlib_submodule_commit", return_value="abc123"
            ),
            patch.object(executor, "_get_last_built_hnswlib_commit", return_value=None),
            patch.object(executor, "_is_user_install", return_value=False),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            result = executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        assert result is True
        pip_calls = [c for c in calls if "pybind11" in c or "--force-reinstall" in c]
        assert pip_calls, (
            "Expected pip install calls when no prior build record; none found"
        )

    def test_rebuild_attempted_when_commit_indeterminate(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """Importable but current commit unknown (None) -> rebuild (cannot confirm unchanged)."""
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            patch.object(executor, "_hnswlib_importable", return_value=True),
            patch.object(executor, "_get_hnswlib_submodule_commit", return_value=None),
            patch.object(
                executor, "_get_last_built_hnswlib_commit", return_value="abc123"
            ),
            patch.object(executor, "_is_user_install", return_value=False),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            result = executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        assert result is True
        pip_calls = [c for c in calls if "pybind11" in c or "--force-reinstall" in c]
        assert pip_calls, (
            "Expected pip install calls when current commit is None; none found"
        )


# ---------------------------------------------------------------------------
# TestBuildCommandShape
# ---------------------------------------------------------------------------


class TestBuildCommandShape:
    """Test pybind11 and hnswlib install command shapes under user vs system install."""

    def test_pybind11_cmd_no_sudo_for_user_install(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """User install -> pybind11 cmd must NOT start with 'sudo'."""
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            patch.object(executor, "_hnswlib_importable", return_value=False),
            patch.object(executor, "_get_hnswlib_submodule_commit", return_value=None),
            patch.object(executor, "_get_last_built_hnswlib_commit", return_value=None),
            patch.object(executor, "_is_user_install", return_value=True),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        pybind11_calls = [c for c in calls if "pybind11" in c]
        assert pybind11_calls, f"Expected pybind11 install call; all calls: {calls}"
        for cmd in pybind11_calls:
            assert cmd[0] != "sudo", (
                f"User install: pybind11 cmd must NOT start with 'sudo'; got: {cmd}"
            )

    def test_hnswlib_cmd_no_sudo_for_user_install(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """User install -> hnswlib cmd must NOT start with 'sudo'."""
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            patch.object(executor, "_hnswlib_importable", return_value=False),
            patch.object(executor, "_get_hnswlib_submodule_commit", return_value=None),
            patch.object(executor, "_get_last_built_hnswlib_commit", return_value=None),
            patch.object(executor, "_is_user_install", return_value=True),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        hnswlib_calls = [c for c in calls if "--force-reinstall" in c]
        assert hnswlib_calls, f"Expected hnswlib install call; all calls: {calls}"
        for cmd in hnswlib_calls:
            assert cmd[0] != "sudo", (
                f"User install: hnswlib cmd must NOT start with 'sudo'; got: {cmd}"
            )

    def test_pybind11_cmd_has_sudo_for_system_install(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """System install -> pybind11 cmd[0] must be 'sudo'."""
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            patch.object(executor, "_hnswlib_importable", return_value=False),
            patch.object(executor, "_get_hnswlib_submodule_commit", return_value=None),
            patch.object(executor, "_get_last_built_hnswlib_commit", return_value=None),
            patch.object(executor, "_is_user_install", return_value=False),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        pybind11_calls = [c for c in calls if "pybind11" in c]
        assert pybind11_calls, f"Expected pybind11 install call; all calls: {calls}"
        assert pybind11_calls[0][0] == "sudo", (
            f"System install: pybind11 cmd[0] must be 'sudo'; got: {pybind11_calls[0]}"
        )

    def test_hnswlib_cmd_has_sudo_for_system_install(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """System install -> hnswlib cmd[0] must be 'sudo'."""
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            patch.object(executor, "_hnswlib_importable", return_value=False),
            patch.object(executor, "_get_hnswlib_submodule_commit", return_value=None),
            patch.object(executor, "_get_last_built_hnswlib_commit", return_value=None),
            patch.object(executor, "_is_user_install", return_value=False),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        hnswlib_calls = [c for c in calls if "--force-reinstall" in c]
        assert hnswlib_calls, f"Expected hnswlib install call; all calls: {calls}"
        assert hnswlib_calls[0][0] == "sudo", (
            f"System install: hnswlib cmd[0] must be 'sudo'; got: {hnswlib_calls[0]}"
        )

    def test_system_install_cmd_has_tmpdir_token(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """System install -> TMPDIR= token must be present in hnswlib cmd (Bug #1243)."""
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            patch.object(executor, "_hnswlib_importable", return_value=False),
            patch.object(executor, "_get_hnswlib_submodule_commit", return_value=None),
            patch.object(executor, "_get_last_built_hnswlib_commit", return_value=None),
            patch.object(executor, "_is_user_install", return_value=False),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        hnswlib_calls = [c for c in calls if "--force-reinstall" in c]
        assert hnswlib_calls, "Expected hnswlib install call"
        tmpdir_tokens = [t for t in hnswlib_calls[0] if t.startswith("TMPDIR=")]
        assert tmpdir_tokens, (
            f"System install: hnswlib cmd must have TMPDIR= token (Bug #1243); "
            f"got: {hnswlib_calls[0]}"
        )

    def test_user_install_cmd_has_no_tmpdir_token(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """User install -> no TMPDIR= token in pybind11 cmd (no sudo, no env passthrough needed)."""
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            patch.object(executor, "_hnswlib_importable", return_value=False),
            patch.object(executor, "_get_hnswlib_submodule_commit", return_value=None),
            patch.object(executor, "_get_last_built_hnswlib_commit", return_value=None),
            patch.object(executor, "_is_user_install", return_value=True),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        pybind11_calls = [c for c in calls if "pybind11" in c]
        assert pybind11_calls, "Expected pybind11 install call"
        tmpdir_tokens = [t for t in pybind11_calls[0] if t.startswith("TMPDIR=")]
        assert not tmpdir_tokens, (
            f"User install: pybind11 cmd must NOT have TMPDIR= token; got: {pybind11_calls[0]}"
        )

    def test_break_system_packages_present_in_user_install_cmd_pip_ge23(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """User install + pip>=23 -> --break-system-packages must be in pybind11 cmd."""
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            patch.object(executor, "_hnswlib_importable", return_value=False),
            patch.object(executor, "_get_hnswlib_submodule_commit", return_value=None),
            patch.object(executor, "_get_last_built_hnswlib_commit", return_value=None),
            patch.object(executor, "_is_user_install", return_value=True),
            patch(
                "subprocess.run",
                side_effect=_make_capturing_dispatch(calls, pip_version="23.1"),
            ),
        ):
            executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        pybind11_calls = [c for c in calls if "pybind11" in c]
        assert pybind11_calls, "Expected pybind11 install call"
        assert "--break-system-packages" in pybind11_calls[0], (
            f"User install + pip>=23: --break-system-packages must be in pybind11 cmd; "
            f"got: {pybind11_calls[0]}"
        )

    def test_break_system_packages_absent_in_user_install_cmd_pip_lt23(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """User install + pip<23 -> --break-system-packages must NOT be in pybind11 cmd."""
        hnswlib_path = _make_hnswlib_path(tmp_path)
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            patch.object(executor, "_hnswlib_importable", return_value=False),
            patch.object(executor, "_get_hnswlib_submodule_commit", return_value=None),
            patch.object(executor, "_get_last_built_hnswlib_commit", return_value=None),
            patch.object(executor, "_is_user_install", return_value=True),
            patch(
                "subprocess.run",
                side_effect=_make_capturing_dispatch(calls, pip_version="21.3.1"),
            ),
        ):
            executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        pybind11_calls = [c for c in calls if "pybind11" in c]
        assert pybind11_calls, "Expected pybind11 install call"
        assert "--break-system-packages" not in pybind11_calls[0], (
            f"User install + pip<23: --break-system-packages must NOT be in pybind11 cmd; "
            f"got: {pybind11_calls[0]}"
        )


# ---------------------------------------------------------------------------
# TestNonFatalRebuildFailure
# ---------------------------------------------------------------------------


class TestNonFatalRebuildFailure:
    """Test that a failed rebuild is non-fatal when hnswlib is still importable."""

    def test_nonfatal_when_rebuild_fails_but_hnswlib_importable(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """hnswlib install rc=1 + hnswlib still importable (2nd probe) -> return True."""
        hnswlib_path = _make_hnswlib_path(tmp_path)

        def dispatch(cmd: list, **kw: object) -> Mock:
            if "--force-reinstall" in cmd:
                # Generic compiler failure; NOT "no such option" (no belt-and-suspenders retry)
                return Mock(
                    returncode=1, stdout="", stderr="Build failed: compiler error"
                )
            if "-m" in cmd and "pip" in cmd and "--version" in cmd:
                return Mock(
                    returncode=0, stdout="pip 23.1 from /path (python 3.9)\n", stderr=""
                )
            return Mock(returncode=0, stdout="", stderr="")

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            # First call (skip check) -> False; second call (failure branch) -> True (non-fatal)
            patch.object(executor, "_hnswlib_importable", side_effect=[False, True]),
            patch.object(executor, "_get_hnswlib_submodule_commit", return_value=None),
            patch.object(executor, "_get_last_built_hnswlib_commit", return_value=None),
            patch.object(executor, "_is_user_install", return_value=False),
            patch("subprocess.run", side_effect=dispatch),
        ):
            result = executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        assert result is True, (
            "Non-fatal: hnswlib still importable after failed rebuild -> must return True"
        )

    def test_fatal_when_rebuild_fails_and_hnswlib_not_importable(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """hnswlib install rc=1 + hnswlib NOT importable (2nd probe) -> return False."""
        hnswlib_path = _make_hnswlib_path(tmp_path)

        def dispatch(cmd: list, **kw: object) -> Mock:
            if "--force-reinstall" in cmd:
                return Mock(
                    returncode=1, stdout="", stderr="Build failed: compiler error"
                )
            if "-m" in cmd and "pip" in cmd and "--version" in cmd:
                return Mock(
                    returncode=0, stdout="pip 23.1 from /path (python 3.9)\n", stderr=""
                )
            return Mock(returncode=0, stdout="", stderr="")

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_ensure_build_dependencies", return_value=True),
            # Both calls return False: skip check fails, failure branch confirms not importable
            patch.object(executor, "_hnswlib_importable", side_effect=[False, False]),
            patch.object(executor, "_get_hnswlib_submodule_commit", return_value=None),
            patch.object(executor, "_get_last_built_hnswlib_commit", return_value=None),
            patch.object(executor, "_is_user_install", return_value=False),
            patch("subprocess.run", side_effect=dispatch),
        ):
            result = executor.build_custom_hnswlib(hnswlib_path=hnswlib_path)

        assert result is False, (
            "Fatal: hnswlib not importable after failed rebuild -> must return False"
        )


# ---------------------------------------------------------------------------
# TestPipInstallCommandShape
# ---------------------------------------------------------------------------


class TestPipInstallCommandShape:
    """Test pip_install() command shape under user vs system install."""

    def test_pip_install_no_sudo_for_user_install(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """User install -> pip install cmd must NOT start with 'sudo'."""
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_is_user_install", return_value=True),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            executor.pip_install()

        install_calls = [c for c in calls if "-e" in c]
        assert install_calls, f"Expected pip install -e . call; all calls: {calls}"
        assert install_calls[0][0] != "sudo", (
            f"User install: cmd must NOT start with 'sudo'; got: {install_calls[0]}"
        )

    def test_pip_install_has_sudo_for_system_install(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """System install -> pip install cmd[0] must be 'sudo'."""
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_is_user_install", return_value=False),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            executor.pip_install()

        install_calls = [c for c in calls if "-e" in c]
        assert install_calls, "Expected pip install -e . call"
        assert install_calls[0][0] == "sudo", (
            f"System install: cmd[0] must be 'sudo'; got: {install_calls[0]}"
        )

    def test_pip_install_system_has_tmpdir_token(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """System install -> TMPDIR= token must be in pip install cmd (Bug #1243)."""
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_is_user_install", return_value=False),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            executor.pip_install()

        install_calls = [c for c in calls if "-e" in c]
        assert install_calls, "Expected pip install -e . call"
        tmpdir_tokens = [t for t in install_calls[0] if t.startswith("TMPDIR=")]
        assert tmpdir_tokens, (
            f"System install: TMPDIR= must be in pip install cmd (Bug #1243); "
            f"got: {install_calls[0]}"
        )

    def test_pip_install_user_has_no_tmpdir_token(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """User install -> no TMPDIR= token in pip install cmd."""
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_is_user_install", return_value=True),
            patch("subprocess.run", side_effect=_make_capturing_dispatch(calls)),
        ):
            executor.pip_install()

        install_calls = [c for c in calls if "-e" in c]
        assert install_calls, "Expected pip install -e . call"
        tmpdir_tokens = [t for t in install_calls[0] if t.startswith("TMPDIR=")]
        assert not tmpdir_tokens, (
            f"User install: TMPDIR= must NOT be in pip install cmd; got: {install_calls[0]}"
        )

    def test_pip_install_break_system_packages_user_install_pip_ge23(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """User install + pip>=23 -> --break-system-packages must be in pip install cmd."""
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_is_user_install", return_value=True),
            patch(
                "subprocess.run",
                side_effect=_make_capturing_dispatch(calls, pip_version="23.1"),
            ),
        ):
            executor.pip_install()

        install_calls = [c for c in calls if "-e" in c]
        assert install_calls, "Expected pip install -e . call"
        assert "--break-system-packages" in install_calls[0], (
            f"User install + pip>=23: --break-system-packages must be in cmd; "
            f"got: {install_calls[0]}"
        )

    def test_pip_install_break_system_packages_system_install_pip_ge23(
        self, executor: DeploymentExecutor, tmp_path: Path, patched_data_dir: Path
    ) -> None:
        """System install + pip>=23 -> --break-system-packages must be in pip install cmd."""
        calls: list = []

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_is_user_install", return_value=False),
            patch(
                "subprocess.run",
                side_effect=_make_capturing_dispatch(calls, pip_version="23.1"),
            ),
        ):
            executor.pip_install()

        install_calls = [c for c in calls if "-e" in c]
        assert install_calls, "Expected pip install -e . call"
        assert "--break-system-packages" in install_calls[0], (
            f"System install + pip>=23: --break-system-packages must be in cmd; "
            f"got: {install_calls[0]}"
        )
