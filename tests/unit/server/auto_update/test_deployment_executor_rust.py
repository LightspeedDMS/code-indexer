"""Bug fix: Rust toolchain must install to /opt/rust (system-wide), not /root/.cargo.

The auto-updater runs as root (Path.home() = /root), but cidx-server runs as the
code-indexer OS user which cannot read /root (0550 permissions). Installing to
/root/.cargo means rustc is unreachable by the service. Fix: use RUST_SYSTEM_DIR
= Path("/opt/rust") as the canonical installation target.

Tests:

1. test_ensure_rust_toolchain_uses_opt_rust
   - RUSTUP_HOME, CARGO_HOME set to /opt/rust in env
   - PATH includes /opt/rust/bin

2. test_install_rust_toolchain_passes_env
   - Both curl and sh subprocess calls receive the env with RUSTUP_HOME/CARGO_HOME

3. test_ensure_systemd_rust_path_uses_system_dir
   - _ensure_systemd_rust_path writes /opt/rust/bin to the systemd PATH, not
     /root/.cargo/bin

4. test_remove_stale_cargo_path
   - Old /root/.cargo/bin entry is stripped from systemd PATH after upgrade

5. test_build_updated_service_content_unchanged_when_rust_bin_present
   - _build_updated_service_content is idempotent when /opt/rust/bin already in PATH

6. test_remove_path_segment_strips_segment
   - _remove_path_segment correctly removes the given segment from
     Environment="PATH=..." line

Only true external dependencies are mocked:
  - subprocess.run (for rustc, cargo, curl/sh commands)
  - SYSTEMD_UNIT_DIR (service file location)
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.auto_update.deployment_executor import (
    RUST_SYSTEM_DIR,
    DeploymentExecutor,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODULE = "code_indexer.server.auto_update.deployment_executor"
SERVICE_NAME = "cidx-server"
RUST_BIN = str(RUST_SYSTEM_DIR / "bin")
STALE_CARGO_BIN = "/root/.cargo/bin"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_executor() -> DeploymentExecutor:
    """Build a minimal DeploymentExecutor without real side effects."""
    executor = DeploymentExecutor.__new__(DeploymentExecutor)
    executor.service_name = SERVICE_NAME
    return executor


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Return a MagicMock mimicking subprocess.CompletedProcess."""
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


# ---------------------------------------------------------------------------
# Test 1: _ensure_rust_toolchain passes RUSTUP_HOME/CARGO_HOME=/opt/rust and
#         PATH includes /opt/rust/bin in the env forwarded to subprocesses.
# ---------------------------------------------------------------------------


class TestEnsureRustToolchainUsesOptRust:
    """_ensure_rust_toolchain must use /opt/rust, not /root/.cargo."""

    def test_env_has_rustup_home_set_to_opt_rust(self, tmp_path: Path) -> None:
        """RUSTUP_HOME must be /opt/rust in the env passed to subprocess calls."""
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        fake_file = str(
            tmp_path
            / "src"
            / "code_indexer"
            / "server"
            / "auto_update"
            / "deployment_executor.py"
        )
        observed_envs: list[dict] = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            env = kwargs.get("env") or {}
            observed_envs.append(dict(env))
            p = MagicMock()
            p.returncode = 0
            p.stdout = "rustc 1.78.0"
            p.stderr = ""
            return p

        with (
            patch(f"{_MODULE}.__file__", fake_file, create=True),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"),
            patch("subprocess.run", side_effect=recording_run),
            patch("pathlib.Path.mkdir"),
        ):
            executor._ensure_rust_toolchain()

        assert observed_envs, "Expected at least one subprocess call with env"
        for env in observed_envs:
            assert env.get("RUSTUP_HOME") == str(RUST_SYSTEM_DIR), (
                f"Expected RUSTUP_HOME={RUST_SYSTEM_DIR}, got: {env.get('RUSTUP_HOME')!r}"
            )

    def test_env_has_cargo_home_set_to_opt_rust(self, tmp_path: Path) -> None:
        """CARGO_HOME must be /opt/rust in the env passed to subprocess calls."""
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        fake_file = str(
            tmp_path
            / "src"
            / "code_indexer"
            / "server"
            / "auto_update"
            / "deployment_executor.py"
        )
        observed_envs: list[dict] = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            env = kwargs.get("env") or {}
            observed_envs.append(dict(env))
            p = MagicMock()
            p.returncode = 0
            p.stdout = "rustc 1.78.0"
            p.stderr = ""
            return p

        with (
            patch(f"{_MODULE}.__file__", fake_file, create=True),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"),
            patch("subprocess.run", side_effect=recording_run),
            patch("pathlib.Path.mkdir"),
        ):
            executor._ensure_rust_toolchain()

        assert observed_envs, "Expected at least one subprocess call with env"
        for env in observed_envs:
            assert env.get("CARGO_HOME") == str(RUST_SYSTEM_DIR), (
                f"Expected CARGO_HOME={RUST_SYSTEM_DIR}, got: {env.get('CARGO_HOME')!r}"
            )

    def test_env_path_includes_opt_rust_bin(self, tmp_path: Path) -> None:
        """PATH in env must include /opt/rust/bin."""
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        fake_file = str(
            tmp_path
            / "src"
            / "code_indexer"
            / "server"
            / "auto_update"
            / "deployment_executor.py"
        )
        observed_envs: list[dict] = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            env = kwargs.get("env") or {}
            observed_envs.append(dict(env))
            p = MagicMock()
            p.returncode = 0
            p.stdout = "rustc 1.78.0"
            p.stderr = ""
            return p

        with (
            patch(f"{_MODULE}.__file__", fake_file, create=True),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"),
            patch("subprocess.run", side_effect=recording_run),
            patch("pathlib.Path.mkdir"),
        ):
            executor._ensure_rust_toolchain()

        assert observed_envs, "Expected at least one subprocess call with env"
        for env in observed_envs:
            path_val = env.get("PATH", "")
            assert RUST_BIN in path_val.split(":"), (
                f"Expected {RUST_BIN!r} in PATH segments, got PATH={path_val!r}"
            )

    def test_env_path_does_not_contain_root_cargo(self, tmp_path: Path) -> None:
        """PATH must NOT contain /root/.cargo/bin."""
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        fake_file = str(
            tmp_path
            / "src"
            / "code_indexer"
            / "server"
            / "auto_update"
            / "deployment_executor.py"
        )
        observed_envs: list[dict] = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            env = kwargs.get("env") or {}
            observed_envs.append(dict(env))
            p = MagicMock()
            p.returncode = 0
            p.stdout = "rustc 1.78.0"
            p.stderr = ""
            return p

        with (
            patch(f"{_MODULE}.__file__", fake_file, create=True),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"),
            patch("subprocess.run", side_effect=recording_run),
            patch("pathlib.Path.mkdir"),
        ):
            executor._ensure_rust_toolchain()

        for env in observed_envs:
            path_val = env.get("PATH", "")
            assert STALE_CARGO_BIN not in path_val.split(":"), (
                f"PATH must not contain {STALE_CARGO_BIN!r}, got PATH={path_val!r}"
            )


# ---------------------------------------------------------------------------
# Test 2: _install_rust_toolchain passes env (with RUSTUP_HOME/CARGO_HOME) to
#         both the curl subprocess and the sh subprocess calls.
# ---------------------------------------------------------------------------


class TestInstallRustToolchainPassesEnv:
    """Both curl and sh subprocess calls inside _install_rust_toolchain must
    receive the env dict containing RUSTUP_HOME and CARGO_HOME."""

    def test_curl_call_receives_env_with_rustup_home(self) -> None:
        """curl subprocess call must receive env with RUSTUP_HOME set."""
        executor = _make_executor()
        test_env = {
            "RUSTUP_HOME": str(RUST_SYSTEM_DIR),
            "CARGO_HOME": str(RUST_SYSTEM_DIR),
            "PATH": RUST_BIN,
        }

        calls: list[tuple] = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((args, kwargs))
            p = MagicMock()
            p.returncode = 0
            p.stdout = b"fake installer"
            p.stderr = b""
            return p

        with patch("subprocess.run", side_effect=recording_run):
            executor._install_rust_toolchain(env=test_env)

        assert calls, "Expected at least one subprocess call"
        curl_call_kwargs = calls[0][1]
        actual_env = curl_call_kwargs.get("env")
        assert actual_env is not None, "curl call must pass env="
        assert actual_env.get("RUSTUP_HOME") == str(RUST_SYSTEM_DIR), (
            f"curl env must have RUSTUP_HOME={RUST_SYSTEM_DIR}, "
            f"got: {actual_env.get('RUSTUP_HOME')!r}"
        )

    def test_sh_call_receives_env_with_cargo_home(self) -> None:
        """sh subprocess call (rustup installer) must receive env with CARGO_HOME set."""
        executor = _make_executor()
        test_env = {
            "RUSTUP_HOME": str(RUST_SYSTEM_DIR),
            "CARGO_HOME": str(RUST_SYSTEM_DIR),
            "PATH": RUST_BIN,
        }

        calls: list[tuple] = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((args, kwargs))
            p = MagicMock()
            p.returncode = 0
            p.stdout = b"fake installer"
            p.stderr = b""
            return p

        with patch("subprocess.run", side_effect=recording_run):
            executor._install_rust_toolchain(env=test_env)

        assert len(calls) >= 2, "Expected at least two subprocess calls (curl + sh)"
        sh_call_kwargs = calls[1][1]
        actual_env = sh_call_kwargs.get("env")
        assert actual_env is not None, "sh call must pass env="
        assert actual_env.get("CARGO_HOME") == str(RUST_SYSTEM_DIR), (
            f"sh env must have CARGO_HOME={RUST_SYSTEM_DIR}, "
            f"got: {actual_env.get('CARGO_HOME')!r}"
        )

    def test_sh_call_receives_env_with_rustup_home(self) -> None:
        """sh subprocess call must also have RUSTUP_HOME so rustup installs to /opt/rust."""
        executor = _make_executor()
        test_env = {
            "RUSTUP_HOME": str(RUST_SYSTEM_DIR),
            "CARGO_HOME": str(RUST_SYSTEM_DIR),
            "PATH": RUST_BIN,
        }

        calls: list[tuple] = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((args, kwargs))
            p = MagicMock()
            p.returncode = 0
            p.stdout = b"fake installer"
            p.stderr = b""
            return p

        with patch("subprocess.run", side_effect=recording_run):
            executor._install_rust_toolchain(env=test_env)

        assert len(calls) >= 2
        sh_call_kwargs = calls[1][1]
        actual_env = sh_call_kwargs.get("env")
        assert actual_env is not None, "sh call must pass env="
        assert actual_env.get("RUSTUP_HOME") == str(RUST_SYSTEM_DIR), (
            f"sh env must have RUSTUP_HOME={RUST_SYSTEM_DIR}, "
            f"got: {actual_env.get('RUSTUP_HOME')!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: _ensure_systemd_rust_path writes /opt/rust/bin to systemd PATH.
# ---------------------------------------------------------------------------


class TestEnsureSystemdRustPathUsesSystemDir:
    """_ensure_systemd_rust_path must add /opt/rust/bin to the systemd unit PATH."""

    def test_returns_true_and_writes_opt_rust_bin(self) -> None:
        """When PATH line is missing /opt/rust/bin, it must be prepended."""
        executor = _make_executor()
        original = '[Service]\nEnvironment="PATH=/usr/local/bin:/usr/bin"\nExecStart=/bin/app\n'

        with tempfile.TemporaryDirectory() as tmpdir:
            unit_dir = Path(tmpdir)
            service_file = unit_dir / f"{SERVICE_NAME}.service"
            service_file.write_text(original)

            with (
                patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", unit_dir),
                patch("subprocess.run") as mock_run,
            ):
                mock_run.side_effect = [
                    MagicMock(returncode=0),  # sudo tee
                    MagicMock(returncode=0),  # daemon-reload
                ]
                result = executor._ensure_systemd_rust_path()

        assert result is True
        tee_payload = str(mock_run.call_args_list[0].kwargs.get("input", ""))
        assert RUST_BIN in tee_payload, (
            f"Expected {RUST_BIN!r} in written content, got: {tee_payload!r}"
        )

    def test_does_not_write_root_cargo_bin(self) -> None:
        """The written systemd PATH must NOT contain /root/.cargo/bin."""
        executor = _make_executor()
        original = '[Service]\nEnvironment="PATH=/usr/local/bin:/usr/bin"\nExecStart=/bin/app\n'

        with tempfile.TemporaryDirectory() as tmpdir:
            unit_dir = Path(tmpdir)
            service_file = unit_dir / f"{SERVICE_NAME}.service"
            service_file.write_text(original)

            with (
                patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", unit_dir),
                patch("subprocess.run") as mock_run,
            ):
                mock_run.side_effect = [
                    MagicMock(returncode=0),
                    MagicMock(returncode=0),
                ]
                executor._ensure_systemd_rust_path()

        tee_payload = str(mock_run.call_args_list[0].kwargs.get("input", ""))
        assert STALE_CARGO_BIN not in tee_payload, (
            f"Systemd PATH must not contain {STALE_CARGO_BIN!r}, got: {tee_payload!r}"
        )

    def test_idempotent_when_rust_bin_already_present(self) -> None:
        """If /opt/rust/bin is already in the PATH line, no subprocess call is made."""
        executor = _make_executor()
        original = (
            f'[Service]\nEnvironment="PATH={RUST_BIN}:/usr/local/bin:/usr/bin"\n'
            "ExecStart=/bin/app\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            unit_dir = Path(tmpdir)
            service_file = unit_dir / f"{SERVICE_NAME}.service"
            service_file.write_text(original)

            with (
                patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", unit_dir),
                patch("subprocess.run") as mock_run,
            ):
                result = executor._ensure_systemd_rust_path()

        assert result is True
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: _ensure_systemd_rust_path removes stale /root/.cargo/bin from PATH.
# ---------------------------------------------------------------------------


class TestRemoveStaleCargoPath:
    """After upgrading, old /root/.cargo/bin entries must be removed from
    the systemd service PATH line."""

    def test_stale_cargo_bin_is_removed_when_present(self) -> None:
        """If /root/.cargo/bin is in PATH, it must be removed from the written file."""
        executor = _make_executor()
        # Service file has the old /root/.cargo/bin entry already
        original = (
            f'[Service]\nEnvironment="PATH={STALE_CARGO_BIN}:/usr/local/bin:/usr/bin"\n'
            "ExecStart=/bin/app\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            unit_dir = Path(tmpdir)
            service_file = unit_dir / f"{SERVICE_NAME}.service"
            service_file.write_text(original)

            with (
                patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", unit_dir),
                patch("subprocess.run") as mock_run,
            ):
                mock_run.side_effect = [
                    MagicMock(returncode=0),
                    MagicMock(returncode=0),
                ]
                result = executor._ensure_systemd_rust_path()

        assert result is True
        tee_payload = str(mock_run.call_args_list[0].kwargs.get("input", ""))
        assert STALE_CARGO_BIN not in tee_payload, (
            f"Stale {STALE_CARGO_BIN!r} must be removed from written content, "
            f"got: {tee_payload!r}"
        )

    def test_opt_rust_bin_is_added_when_only_stale_was_present(self) -> None:
        """After removing stale entry, /opt/rust/bin must appear in PATH."""
        executor = _make_executor()
        original = (
            f'[Service]\nEnvironment="PATH={STALE_CARGO_BIN}:/usr/local/bin:/usr/bin"\n'
            "ExecStart=/bin/app\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            unit_dir = Path(tmpdir)
            service_file = unit_dir / f"{SERVICE_NAME}.service"
            service_file.write_text(original)

            with (
                patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", unit_dir),
                patch("subprocess.run") as mock_run,
            ):
                mock_run.side_effect = [
                    MagicMock(returncode=0),
                    MagicMock(returncode=0),
                ]
                executor._ensure_systemd_rust_path()

        tee_payload = str(mock_run.call_args_list[0].kwargs.get("input", ""))
        assert RUST_BIN in tee_payload, (
            f"Expected {RUST_BIN!r} in written content after stale removal, "
            f"got: {tee_payload!r}"
        )


# ---------------------------------------------------------------------------
# Test 5: _build_updated_service_content is idempotent when /opt/rust/bin
#         is already present.
# ---------------------------------------------------------------------------


class TestBuildUpdatedServiceContentUnchangedWhenRustBinPresent:
    """_build_updated_service_content must return content unchanged when
    /opt/rust/bin is already an exact segment in the PATH line."""

    def test_returns_content_unchanged_when_rust_bin_already_present(self) -> None:
        original = (
            f'[Service]\nEnvironment="PATH={RUST_BIN}:/usr/local/bin:/usr/bin"\n'
            "ExecStart=/bin/app\n"
        )
        result = DeploymentExecutor._build_updated_service_content(original, RUST_BIN)
        assert result == original, (
            f"Expected content unchanged, but got different output:\n{result!r}"
        )

    def test_prepends_rust_bin_when_absent(self) -> None:
        original = (
            '[Service]\nEnvironment="PATH=/usr/local/bin:/usr/bin"\n'
            "ExecStart=/bin/app\n"
        )
        result = DeploymentExecutor._build_updated_service_content(original, RUST_BIN)
        assert RUST_BIN in result
        # Must appear before other PATH segments
        path_line = [
            line for line in result.splitlines() if 'Environment="PATH=' in line
        ][0]
        segments = path_line.split('"')[1][len("PATH=") :].split(":")
        assert segments[0] == RUST_BIN, (
            f"Expected {RUST_BIN!r} as first PATH segment, got: {segments[0]!r}"
        )


# ---------------------------------------------------------------------------
# Test 6: _remove_path_segment correctly strips a path segment.
# ---------------------------------------------------------------------------


class TestRemovePathSegment:
    """_remove_path_segment(content, segment) must remove the given segment
    from the Environment="PATH=..." line without affecting other lines."""

    def test_removes_segment_from_path_line(self) -> None:
        """Target segment must be removed from the PATH line."""
        content = (
            f'[Service]\nEnvironment="PATH={STALE_CARGO_BIN}:/usr/local/bin:/usr/bin"\n'
            "ExecStart=/bin/app\n"
        )
        result = DeploymentExecutor._remove_path_segment(content, STALE_CARGO_BIN)
        assert STALE_CARGO_BIN not in result, (
            f"Expected {STALE_CARGO_BIN!r} to be removed, got: {result!r}"
        )

    def test_other_path_segments_preserved(self) -> None:
        """Other PATH segments must be preserved after removal."""
        content = (
            f'[Service]\nEnvironment="PATH={STALE_CARGO_BIN}:/usr/local/bin:/usr/bin"\n'
            "ExecStart=/bin/app\n"
        )
        result = DeploymentExecutor._remove_path_segment(content, STALE_CARGO_BIN)
        assert "/usr/local/bin" in result
        assert "/usr/bin" in result

    def test_no_double_colon_after_removal(self) -> None:
        """Removing a segment must not leave double colons (empty path segment)."""
        content = (
            f'[Service]\nEnvironment="PATH=/usr/local/bin:{STALE_CARGO_BIN}:/usr/bin"\n'
            "ExecStart=/bin/app\n"
        )
        result = DeploymentExecutor._remove_path_segment(content, STALE_CARGO_BIN)
        assert "::" not in result, (
            f"Expected no '::' after segment removal, got: {result!r}"
        )

    def test_returns_content_unchanged_when_segment_absent(self) -> None:
        """If the segment is not present, content must be returned unchanged."""
        content = (
            '[Service]\nEnvironment="PATH=/usr/local/bin:/usr/bin"\n'
            "ExecStart=/bin/app\n"
        )
        result = DeploymentExecutor._remove_path_segment(content, STALE_CARGO_BIN)
        assert result == content

    def test_non_path_lines_are_untouched(self) -> None:
        """Lines that are not Environment PATH lines must not be modified."""
        content = (
            f'[Service]\nEnvironment="PATH={STALE_CARGO_BIN}:/usr/bin"\n'
            "ExecStart=/bin/app\n"
            "Description=cidx-server\n"
        )
        result = DeploymentExecutor._remove_path_segment(content, STALE_CARGO_BIN)
        assert "ExecStart=/bin/app" in result
        assert "Description=cidx-server" in result
