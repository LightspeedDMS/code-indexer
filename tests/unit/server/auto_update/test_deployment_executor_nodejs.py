"""BUG #1318: Node.js/npm provisioning in DeploymentExecutor.

Node.js/npm is not installed on any server node, so ensure_scip_python()
(v11.26.0) logs "npm not available on PATH" and SCIP builds fail with
"[Errno 2] No such file or directory: 'scip-python'". Same root cause makes
Codex-CLI provisioning inert.

Fix: provision a pinned Node.js LTS toolchain to a system-wide directory
(mirrors RUST_SYSTEM_DIR = /opt/rust) BEFORE ensure_scip_python() and the
Codex CLI install run, and wire /opt/node/bin onto both (a) the systemd
unit PATH (for the running server + its child index subprocesses) and
(b) this process's own os.environ["PATH"] (so the SAME execute() run's
subsequent npm-dependent steps can find it).

Mirrors the existing ensure_ripgrep()/_ensure_rust_toolchain()/
ensure_scip_python() test conventions: subprocess.run is the mocked
external boundary; pure Python logic (tar extraction, PATH string
manipulation) executes for real wherever practical.
"""

import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.auto_update.deployment_executor import (
    NODEJS_DIST_URL,
    NODEJS_VERSION,
    DeploymentExecutor,
)

_MODULE = "code_indexer.server.auto_update.deployment_executor"
SERVICE_NAME = "cidx-server"


def _make_executor() -> DeploymentExecutor:
    """Build a minimal DeploymentExecutor without real side effects."""
    executor = DeploymentExecutor.__new__(DeploymentExecutor)
    executor.service_name = SERVICE_NAME
    return executor


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


# ---------------------------------------------------------------------------
# Slice A: _check_node_installed idempotency probe
# ---------------------------------------------------------------------------


class TestCheckNodeInstalled:
    """_check_node_installed() must detect an existing Node.js install
    either at NODEJS_INSTALL_DIR/bin/node or elsewhere on PATH."""

    def test_check_node_installed_true_when_opt_node_binary_runs(
        self, tmp_path: Path
    ) -> None:
        fake_install_dir = tmp_path / "opt_node"
        (fake_install_dir / "bin").mkdir(parents=True)
        node_bin = fake_install_dir / "bin" / "node"
        node_bin.write_text("#!/bin/sh\necho fake\n")

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch("subprocess.run", return_value=_proc(0, "v22.11.0", "")) as mock_run,
            patch(f"{_MODULE}.shutil.which", return_value=None),
        ):
            executor = _make_executor()
            result = executor._check_node_installed()

        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == str(node_bin)
        assert args[1] == "--version"

    def test_check_node_installed_true_when_shutil_which_finds_node_elsewhere(
        self, tmp_path: Path
    ) -> None:
        fake_install_dir = tmp_path / "opt_node"  # does not exist on disk

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch("subprocess.run") as mock_run,
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/node"),
        ):
            executor = _make_executor()
            result = executor._check_node_installed()

        assert result is True
        mock_run.assert_not_called()

    def test_check_node_installed_false_when_neither_present(
        self, tmp_path: Path
    ) -> None:
        fake_install_dir = tmp_path / "opt_node"  # does not exist on disk

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch("subprocess.run") as mock_run,
            patch(f"{_MODULE}.shutil.which", return_value=None),
        ):
            executor = _make_executor()
            result = executor._check_node_installed()

        assert result is False
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Slice B: _download_nodejs_tarball via curl (no shell=True, mirrors rustup)
# ---------------------------------------------------------------------------


class TestDownloadNodejsTarball:
    """_download_nodejs_tarball() must curl-download the pinned tarball."""

    def test_download_success_returns_true_and_uses_curl(self, tmp_path: Path) -> None:
        dest = tmp_path / "node.tar.xz"

        with patch("subprocess.run", return_value=_proc(0, "", "")) as mock_run:
            executor = _make_executor()
            result = executor._download_nodejs_tarball(dest)

        assert result is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "curl"
        assert str(dest) in args
        assert NODEJS_DIST_URL in args

    def test_download_nonzero_exit_returns_false(self, tmp_path: Path) -> None:
        dest = tmp_path / "node.tar.xz"

        with patch("subprocess.run", return_value=_proc(22, "", "HTTP 404 Not Found")):
            executor = _make_executor()
            result = executor._download_nodejs_tarball(dest)

        assert result is False

    def test_download_timeout_returns_false_no_raise(self, tmp_path: Path) -> None:
        dest = tmp_path / "node.tar.xz"
        timeout_error = subprocess.TimeoutExpired(cmd=["curl"], timeout=300)

        with patch("subprocess.run", side_effect=timeout_error):
            executor = _make_executor()
            result = executor._download_nodejs_tarball(dest)

        assert result is False


# ---------------------------------------------------------------------------
# Slice C: _extract_nodejs_tarball — REAL tar extraction (no mocking of
# tarfile itself, per anti-mock policy: only subprocess/filesystem
# boundaries that require real infra are approximated).
# ---------------------------------------------------------------------------


def _build_real_nodejs_tarball(dest_tar: Path, top_dir_name: str) -> None:
    """Build a real .tar.xz containing top_dir_name/bin/node (a tiny fake
    executable) plus a nested file, mirroring the real Node.js dist layout."""
    with tempfile.TemporaryDirectory() as staging:
        root = Path(staging) / top_dir_name
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "node").write_text("#!/bin/sh\necho fake-node\n")
        (root / "bin" / "npm").write_text("#!/bin/sh\necho fake-npm\n")
        (root / "lib").mkdir()
        (root / "lib" / "README.md").write_text("fake lib file\n")

        with tarfile.open(dest_tar, "w:xz") as tar:
            tar.add(root, arcname=top_dir_name)


class TestExtractNodejsTarball:
    """_extract_nodejs_tarball() strips the top-level node-vX.Y.Z-linux-x64/
    directory (tarfile has no --strip-components) and moves contents into
    the install dir."""

    def test_extracts_and_strips_top_level_dir(self, tmp_path: Path) -> None:
        tar_path = tmp_path / "node.tar.xz"
        install_dir = tmp_path / "opt_node"
        install_dir.mkdir()
        top_dir_name = "node-v22.11.0-linux-x64"

        with patch(f"{_MODULE}.NODEJS_VERSION", "22.11.0"):
            _build_real_nodejs_tarball(tar_path, top_dir_name)

            executor = _make_executor()
            result = executor._extract_nodejs_tarball(tar_path, install_dir)

        assert result is True
        assert (install_dir / "bin" / "node").is_file()
        assert (install_dir / "bin" / "npm").is_file()
        assert (install_dir / "lib" / "README.md").is_file()
        # Top-level dir name itself must NOT appear inside install_dir
        assert not (install_dir / top_dir_name).exists()

    def test_missing_expected_top_dir_returns_false(self, tmp_path: Path) -> None:
        tar_path = tmp_path / "node.tar.xz"
        install_dir = tmp_path / "opt_node"
        install_dir.mkdir()

        with tempfile.TemporaryDirectory() as staging:
            wrong_root = Path(staging) / "unexpected-dir-name"
            wrong_root.mkdir()
            (wrong_root / "file.txt").write_text("x")
            with tarfile.open(tar_path, "w:xz") as tar:
                tar.add(wrong_root, arcname="unexpected-dir-name")

        with patch(f"{_MODULE}.NODEJS_VERSION", "22.11.0"):
            executor = _make_executor()
            result = executor._extract_nodejs_tarball(tar_path, install_dir)

        assert result is False

    def test_corrupt_tarball_returns_false_no_raise(self, tmp_path: Path) -> None:
        tar_path = tmp_path / "node.tar.xz"
        tar_path.write_bytes(b"not a real tarball")
        install_dir = tmp_path / "opt_node"
        install_dir.mkdir()

        executor = _make_executor()
        result = executor._extract_nodejs_tarball(tar_path, install_dir)

        assert result is False


# ---------------------------------------------------------------------------
# Slice D: _add_nodejs_bin_to_process_path — same-run PATH wiring so
# ensure_scip_python()/_ensure_codex_cli_installed() (which call
# shutil.which("npm")/subprocess.run(["npm", ...]) without an explicit
# env= kwarg, relying on the inherited process environment) can find npm
# immediately after ensure_nodejs() installs it, within the SAME execute()
# run.
# ---------------------------------------------------------------------------


class TestAddNodejsBinToProcessPath:
    """_add_nodejs_bin_to_process_path() must prepend NODEJS_INSTALL_DIR/bin
    to os.environ['PATH'] exactly once (idempotent)."""

    def test_prepends_node_bin_to_process_path(self, tmp_path: Path) -> None:
        fake_install_dir = tmp_path / "opt_node"

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch.dict("os.environ", {"PATH": "/usr/bin:/bin"}, clear=False),
        ):
            executor = _make_executor()
            executor._add_nodejs_bin_to_process_path()
            import os as _os

            path_segments = _os.environ["PATH"].split(":")

        assert str(fake_install_dir / "bin") == path_segments[0]
        assert "/usr/bin" in path_segments
        assert "/bin" in path_segments

    def test_idempotent_does_not_duplicate_segment(self, tmp_path: Path) -> None:
        fake_install_dir = tmp_path / "opt_node"
        node_bin = str(fake_install_dir / "bin")

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch.dict("os.environ", {"PATH": f"{node_bin}:/usr/bin"}, clear=False),
        ):
            executor = _make_executor()
            executor._add_nodejs_bin_to_process_path()
            import os as _os

            final_path = _os.environ["PATH"]

        assert final_path.count(node_bin) == 1


# ---------------------------------------------------------------------------
# Slice E: _ensure_systemd_node_path — mirrors _ensure_systemd_rust_path()
# exactly (writes /opt/node/bin into the systemd unit PATH so the RUNNING
# server + its child index subprocesses find node/npm/scip-python).
# ---------------------------------------------------------------------------


class TestEnsureSystemdNodePath:
    """_ensure_systemd_node_path() must add NODEJS_INSTALL_DIR/bin to the
    systemd unit PATH, idempotently."""

    def test_returns_true_and_writes_node_bin(self, tmp_path: Path) -> None:
        fake_install_dir = tmp_path / "opt_node"
        node_bin = str(fake_install_dir / "bin")
        original = (
            '[Service]\nEnvironment="PATH=/usr/local/bin:/usr/bin"\n'
            "ExecStart=/bin/app\n"
        )

        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir()
        service_file = unit_dir / f"{SERVICE_NAME}.service"
        service_file.write_text(original)

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", unit_dir),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0),  # sudo tee
                MagicMock(returncode=0),  # daemon-reload
            ]
            executor = _make_executor()
            result = executor._ensure_systemd_node_path()

        assert result is True
        tee_payload = str(mock_run.call_args_list[0].kwargs.get("input", ""))
        assert node_bin in tee_payload

    def test_idempotent_when_node_bin_already_present(self, tmp_path: Path) -> None:
        fake_install_dir = tmp_path / "opt_node"
        node_bin = str(fake_install_dir / "bin")
        original = (
            f'[Service]\nEnvironment="PATH={node_bin}:/usr/local/bin:/usr/bin"\n'
            "ExecStart=/bin/app\n"
        )

        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir()
        service_file = unit_dir / f"{SERVICE_NAME}.service"
        service_file.write_text(original)

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", unit_dir),
            patch("subprocess.run") as mock_run,
        ):
            executor = _make_executor()
            result = executor._ensure_systemd_node_path()

        assert result is True
        mock_run.assert_not_called()

    def test_returns_false_when_service_file_missing(self, tmp_path: Path) -> None:
        fake_install_dir = tmp_path / "opt_node"
        unit_dir = tmp_path / "systemd"
        unit_dir.mkdir()  # no service file written

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", unit_dir),
        ):
            executor = _make_executor()
            result = executor._ensure_systemd_node_path()

        assert result is False


# ---------------------------------------------------------------------------
# Slice F: ensure_nodejs() — the main orchestrator, mirroring
# _ensure_rust_toolchain()'s idempotent-check -> sudo mkdir/chown ->
# download -> extract -> verify -> PATH-wiring structure.
# ---------------------------------------------------------------------------


def _fake_run_happy_path(args, **kwargs):  # type: ignore[no-untyped-def]
    """subprocess.run side_effect simulating a successful full provision:
    sudo mkdir actually creates the dir (mimicking real sudo mkdir -p),
    sudo chown is a no-op success, curl writes a REAL tarball to -o dest,
    and the post-extraction `node --version` probe succeeds."""
    if args[:2] == ["sudo", "mkdir"]:
        Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return _proc(0)
    if args[:2] == ["sudo", "chown"]:
        return _proc(0)
    if args[0] == "curl":
        dest = Path(args[args.index("-o") + 1])
        _build_real_nodejs_tarball(dest, f"node-v{NODEJS_VERSION}-linux-x64")
        return _proc(0)
    return _proc(0, f"v{NODEJS_VERSION}", "")  # node --version probe


class TestEnsureNodejs:
    """ensure_nodejs() orchestrates the idempotent Node.js provisioning."""

    def test_already_installed_skips_provisioning_and_wires_path(
        self, tmp_path: Path
    ) -> None:
        fake_install_dir = tmp_path / "opt_node"

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch("subprocess.run") as mock_run,
            patch.object(
                DeploymentExecutor, "_check_node_installed", return_value=True
            ),
            patch.object(
                DeploymentExecutor, "_add_nodejs_bin_to_process_path"
            ) as mock_path,
            patch.object(
                DeploymentExecutor, "_ensure_systemd_node_path", return_value=True
            ) as mock_systemd,
        ):
            executor = _make_executor()
            result = executor.ensure_nodejs()

        assert result is True
        mock_run.assert_not_called()
        mock_path.assert_called_once()
        mock_systemd.assert_called_once()

    def test_not_installed_downloads_extracts_and_returns_true(
        self, tmp_path: Path
    ) -> None:
        fake_install_dir = tmp_path / "opt_node"  # does not exist yet

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch(f"{_MODULE}.platform.machine", return_value="x86_64"),
            # Bug-in-test-fixture guard: the dev/CI machine may have a REAL
            # node on PATH (e.g. /bin/node for Claude Code itself), which
            # would falsely satisfy _check_node_installed()'s shutil.which
            # fallback before extraction ever runs. Force it absent so the
            # "not installed" branch is genuinely exercised.
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch("subprocess.run", side_effect=_fake_run_happy_path) as mock_run,
            patch.object(
                DeploymentExecutor, "_ensure_systemd_node_path", return_value=True
            ) as mock_systemd,
        ):
            executor = _make_executor()
            result = executor.ensure_nodejs()

        assert result is True
        assert (fake_install_dir / "bin" / "node").is_file()
        mock_systemd.assert_called_once()
        called_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any(cmd[:2] == ["sudo", "mkdir"] for cmd in called_cmds)
        assert any(cmd[:2] == ["sudo", "chown"] for cmd in called_cmds)
        assert any(cmd[0] == "curl" for cmd in called_cmds)

    def test_unsupported_architecture_returns_false_no_subprocess(
        self, tmp_path: Path
    ) -> None:
        fake_install_dir = tmp_path / "opt_node"  # does not exist

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch(f"{_MODULE}.platform.machine", return_value="aarch64"),
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            executor = _make_executor()
            result = executor.ensure_nodejs()

        assert result is False
        mock_run.assert_not_called()


class TestEnsureNodejsFailurePaths:
    """ensure_nodejs() must fail non-fatally (WARNING + False) at each
    provisioning step and correctly short-circuit subsequent steps."""

    def test_sudo_mkdir_failure_returns_false_no_chown_or_curl(
        self, tmp_path: Path
    ) -> None:
        fake_install_dir = tmp_path / "opt_node"  # does not exist

        def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[:2] == ["sudo", "mkdir"]:
                return _proc(1, "", "Permission denied")
            return _proc(0)

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch(f"{_MODULE}.platform.machine", return_value="x86_64"),
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch("subprocess.run", side_effect=fake_run) as mock_run,
        ):
            executor = _make_executor()
            result = executor.ensure_nodejs()

        assert result is False
        called_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert not any(cmd[:2] == ["sudo", "chown"] for cmd in called_cmds)
        assert not any(cmd[0] == "curl" for cmd in called_cmds)

    def test_sudo_chown_failure_returns_false_no_curl(self, tmp_path: Path) -> None:
        fake_install_dir = tmp_path / "opt_node"  # does not exist

        def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[:2] == ["sudo", "mkdir"]:
                Path(args[-1]).mkdir(parents=True, exist_ok=True)
                return _proc(0)
            if args[:2] == ["sudo", "chown"]:
                return _proc(1, "", "chown: not permitted")
            return _proc(0)

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch(f"{_MODULE}.platform.machine", return_value="x86_64"),
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch("subprocess.run", side_effect=fake_run) as mock_run,
        ):
            executor = _make_executor()
            result = executor.ensure_nodejs()

        assert result is False
        called_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert not any(cmd[0] == "curl" for cmd in called_cmds)

    def test_download_failure_returns_false(self, tmp_path: Path) -> None:
        fake_install_dir = tmp_path / "opt_node"  # does not exist

        def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[:2] == ["sudo", "mkdir"]:
                Path(args[-1]).mkdir(parents=True, exist_ok=True)
                return _proc(0)
            if args[:2] == ["sudo", "chown"]:
                return _proc(0)
            if args[0] == "curl":
                return _proc(22, "", "HTTP 404 Not Found")
            return _proc(0)

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch(f"{_MODULE}.platform.machine", return_value="x86_64"),
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch("subprocess.run", side_effect=fake_run),
        ):
            executor = _make_executor()
            result = executor.ensure_nodejs()

        assert result is False

    def test_extraction_failure_returns_false(self, tmp_path: Path) -> None:
        """Curl 'succeeds' but writes a corrupt (non-tar) file -- extraction
        must fail and ensure_nodejs() must return False."""
        fake_install_dir = tmp_path / "opt_node"  # does not exist

        def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[:2] == ["sudo", "mkdir"]:
                Path(args[-1]).mkdir(parents=True, exist_ok=True)
                return _proc(0)
            if args[:2] == ["sudo", "chown"]:
                return _proc(0)
            if args[0] == "curl":
                dest = Path(args[args.index("-o") + 1])
                dest.write_bytes(b"not a real tarball")
                return _proc(0)
            return _proc(0)

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch(f"{_MODULE}.platform.machine", return_value="x86_64"),
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch("subprocess.run", side_effect=fake_run),
        ):
            executor = _make_executor()
            result = executor.ensure_nodejs()

        assert result is False

    def test_post_extraction_verification_failure_returns_false(
        self, tmp_path: Path
    ) -> None:
        """Extraction reports success, but the resulting tarball did not
        actually contain bin/node -- the final _check_node_installed()
        verification must catch this and ensure_nodejs() must return
        False (not silently claim success)."""
        fake_install_dir = tmp_path / "opt_node"  # does not exist

        def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[:2] == ["sudo", "mkdir"]:
                Path(args[-1]).mkdir(parents=True, exist_ok=True)
                return _proc(0)
            if args[:2] == ["sudo", "chown"]:
                return _proc(0)
            if args[0] == "curl":
                dest = Path(args[args.index("-o") + 1])
                top_dir_name = f"node-v{NODEJS_VERSION}-linux-x64"
                with tempfile.TemporaryDirectory() as staging:
                    root = Path(staging) / top_dir_name
                    (root / "lib").mkdir(parents=True)
                    (root / "lib" / "README.md").write_text("no bin/node here\n")
                    with tarfile.open(dest, "w:xz") as tar:
                        tar.add(root, arcname=top_dir_name)
                return _proc(0)
            return _proc(1, "", "no such file or directory")  # node --version probe

        with (
            patch(f"{_MODULE}.NODEJS_INSTALL_DIR", fake_install_dir),
            patch(f"{_MODULE}.platform.machine", return_value="x86_64"),
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch("subprocess.run", side_effect=fake_run),
        ):
            executor = _make_executor()
            result = executor.ensure_nodejs()

        assert result is False


# ---------------------------------------------------------------------------
# Slice G: execute() must call ensure_nodejs() BEFORE ensure_scip_python()
# and _ensure_codex_cli_installed(), since both need npm on PATH.
# ---------------------------------------------------------------------------


class TestExecuteWiresEnsureNodejsBeforeNpmConsumers:
    """execute() must provision Node.js before the npm-dependent steps.

    Mirrors test_deployment_executor_scip_python.py::
    test_execute_wires_ensure_scip_python: sibling provisioning steps are
    patched (the established project pattern for isolating execute()'s
    orchestration/ordering from the heavy real work each step performs,
    which is unit-tested independently elsewhere).
    """

    def test_ensure_nodejs_called_before_scip_python_and_codex(self) -> None:
        executor = DeploymentExecutor(
            repo_path=Path("/test/repo"), service_name=SERVICE_NAME
        )
        call_order: list = []

        def record(name):  # type: ignore[no-untyped-def]
            def _fn(*args, **kwargs):  # type: ignore[no-untyped-def]
                call_order.append(name)
                return True

            return _fn

        with (
            patch.object(
                executor, "_calculate_auto_update_hash", return_value="abc123"
            ),
            patch.object(executor, "git_pull", return_value=True),
            patch.object(executor, "git_submodule_update", return_value=True),
            patch.object(executor, "_build_hnswlib_with_fallback", return_value=True),
            patch.object(executor, "pip_install", return_value=True),
            patch.object(executor, "ensure_ripgrep", return_value=True),
            patch.object(executor, "_ensure_rust_toolchain", return_value=True),
            patch.object(
                executor, "ensure_nodejs", side_effect=record("ensure_nodejs")
            ),
            patch.object(
                executor,
                "ensure_scip_python",
                side_effect=record("ensure_scip_python"),
            ),
            patch.object(
                executor,
                "_ensure_codex_cli_installed",
                side_effect=record("_ensure_codex_cli_installed"),
            ),
        ):
            executor.execute()

        assert "ensure_nodejs" in call_order
        assert "ensure_scip_python" in call_order
        assert "_ensure_codex_cli_installed" in call_order
        assert call_order.index("ensure_nodejs") < call_order.index(
            "ensure_scip_python"
        ), f"ensure_nodejs must run before ensure_scip_python, got order: {call_order}"
        assert call_order.index("ensure_nodejs") < call_order.index(
            "_ensure_codex_cli_installed"
        ), (
            "ensure_nodejs must run before _ensure_codex_cli_installed, "
            f"got order: {call_order}"
        )
