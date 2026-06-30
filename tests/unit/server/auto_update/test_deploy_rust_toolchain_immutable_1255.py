"""Bug #1255 (P1): auto-updater dead-loops on immutable hosts where /opt/rust
is already provisioned (rustc/cargo present and usable) but the root
filesystem is read-only, so `sudo chown -R <uid:gid> /opt/rust` fails and was
treated as FATAL ([DEPLOY-GENERAL-172] "Deployment cannot continue").

Fix:
1. `_ensure_rust_toolchain` probes whether the toolchain is ALREADY usable
   (rustc + cargo present and runnable via the resolved absolute binary
   paths under RUST_SYSTEM_DIR/bin) BEFORE attempting any mkdir/chown.  If
   usable, provisioning (mkdir + chown + install) is skipped entirely.
2. If provisioning IS attempted (toolchain not yet proven usable) and the
   mkdir or chown subprocess fails, a second usability probe runs; if the
   toolchain turns out to be usable despite the failure, the failure is
   logged at WARNING and deployment continues (non-fatal).  Only when the
   toolchain is genuinely unusable does the failure remain FATAL.
3. The xray-cli `cargo build` step redirects CARGO_HOME to a writable
   fallback (~/.cargo) when RUST_SYSTEM_DIR exists but is not writable, so
   the build doesn't need write access under a read-only /opt/rust.  A
   residual cargo-build failure (or missing C compiler) is now WARNING +
   non-fatal -- xray's native backend is an optional accelerator with a
   Python fallback, so deployment (pip install + restart) must still
   complete.
4. Genuinely-missing-and-uninstallable toolchain remains FATAL (regression
   pin) -- this bug is about hosts where the toolchain IS present and
   usable, not about hosts that truly lack Rust.

Only true external dependencies are mocked: subprocess.run, os.access,
Path.home, shutil.which, __file__ (repo-root resolution).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.auto_update.deployment_executor import (
    RUST_SYSTEM_DIR,
    DeploymentExecutor,
)

_MODULE = "code_indexer.server.auto_update.deployment_executor"
RUST_BIN = str(RUST_SYSTEM_DIR / "bin")


def _make_executor() -> DeploymentExecutor:
    """Build a minimal DeploymentExecutor without real side effects."""
    executor = DeploymentExecutor.__new__(DeploymentExecutor)
    executor.service_name = "cidx-server"
    return executor


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


def _fake_file_for(tmp_path: Path) -> str:
    return str(
        tmp_path
        / "src"
        / "code_indexer"
        / "server"
        / "auto_update"
        / "deployment_executor.py"
    )


# ---------------------------------------------------------------------------
# Requirement 1: toolchain already usable -> skip mkdir/chown entirely
# ---------------------------------------------------------------------------


class TestSkipProvisioningWhenAlreadyUsable:
    """If rustc/cargo already work at RUST_SYSTEM_DIR/bin, _ensure_rust_toolchain
    must never call sudo mkdir / sudo chown -- ownership is not required to
    execute an already-present, already-executable toolchain."""

    def test_no_chown_or_mkdir_called_when_toolchain_already_usable(
        self, tmp_path: Path
    ) -> None:
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()
        fake_file = _fake_file_for(tmp_path)

        calls: list[list] = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(list(args))
            return _proc(0, stdout="rustc 1.78.0 / cargo 1.78.0")

        with (
            patch(f"{_MODULE}.__file__", fake_file, create=True),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"),
            patch("subprocess.run", side_effect=recording_run),
        ):
            result = executor._ensure_rust_toolchain()

        assert result is True
        mkdir_calls = [c for c in calls if "mkdir" in c]
        chown_calls = [c for c in calls if "chown" in c]
        assert not mkdir_calls, f"Expected no mkdir calls, got: {mkdir_calls}"
        assert not chown_calls, f"Expected no chown calls, got: {chown_calls}"

    def test_info_logged_when_toolchain_already_usable(
        self, tmp_path: Path, caplog
    ) -> None:
        import logging

        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()
        fake_file = _fake_file_for(tmp_path)

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            return _proc(0, stdout="rustc 1.78.0")

        with caplog.at_level(logging.INFO):
            with (
                patch(f"{_MODULE}.__file__", fake_file, create=True),
                patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
                patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"),
                patch("subprocess.run", side_effect=recording_run),
            ):
                executor._ensure_rust_toolchain()

        assert any(
            "already" in r.message.lower() and "usable" in r.message.lower()
            for r in caplog.records
        ), (
            f"Expected an INFO log about toolchain already usable, got: {[r.message for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# Requirement 2: chown fails but toolchain usable -> non-fatal
# ---------------------------------------------------------------------------


class TestChownFailureNonFatalWhenToolchainUsable:
    """If the upfront probe says 'not usable yet' (e.g. cold check before
    provisioning), mkdir succeeds, but chown fails with a read-only-fs style
    error, and a SECOND usability probe afterwards proves the toolchain is
    in fact usable -- the chown failure must be logged at WARNING and
    deployment must continue (return True), not abort."""

    def test_returns_true_and_warns_when_chown_fails_but_usable(
        self, tmp_path: Path, caplog
    ) -> None:
        import logging

        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()
        fake_file = _fake_file_for(tmp_path)

        # Usability probe call-count: 1st pair (rustc, cargo) = upfront check
        # (not usable yet) -> 2nd pair (post-chown-failure recheck) = usable.
        probe_call_count = {"n": 0}

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            if "mkdir" in args:
                return _proc(0)
            if "chown" in args:
                return _proc(1, stderr="chown: Read-only file system")
            # Resolved absolute-path rustc/cargo probe calls.
            if args and str(args[0]).startswith(RUST_BIN):
                probe_call_count["n"] += 1
                # Upfront probe short-circuits on the first binary (rustc)
                # failing, consuming exactly 1 call -> not usable yet.
                if probe_call_count["n"] <= 1:
                    return _proc(1, stderr="not found")
                # Subsequent calls (post-chown-failure recheck) -> usable.
                return _proc(0, stdout="rustc 1.78.0")
            # _check_rustc_installed (plain "rustc" via PATH)
            return _proc(0, stdout="rustc 1.78.0")

        with caplog.at_level(logging.WARNING):
            with (
                patch(f"{_MODULE}.__file__", fake_file, create=True),
                patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
                patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"),
                patch("subprocess.run", side_effect=recording_run),
            ):
                result = executor._ensure_rust_toolchain()

        assert result is True, (
            "chown failure must be non-fatal when toolchain is usable"
        )
        assert any(
            r.levelno == logging.WARNING and "chown" in r.message.lower()
            for r in caplog.records
        ), (
            f"Expected a WARNING about chown failure, got: {[r.message for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# Requirement 4 (regression pin): genuinely missing toolchain + install
# failure remains FATAL.
# ---------------------------------------------------------------------------


class TestGenuinelyMissingToolchainStillFatal:
    """When the toolchain is NOT present anywhere (probe always fails) and
    the rustup install itself fails, _ensure_rust_toolchain must still
    return False -- this bug is about hosts where Rust IS present and
    usable, not about hosts that truly lack it."""

    def test_returns_false_when_toolchain_missing_and_install_fails(
        self, tmp_path: Path
    ) -> None:
        executor = _make_executor()
        fake_file = _fake_file_for(tmp_path)

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            if "mkdir" in args:
                return _proc(0)
            if "chown" in args:
                return _proc(0)
            if args and str(args[0]).startswith(RUST_BIN):
                # Resolved-path usability probe: never usable.
                return _proc(1, stderr="not found")
            if args and args[0] == "rustc":
                # _check_rustc_installed: rustc not on PATH either.
                raise FileNotFoundError("rustc not found")
            # curl / sh install steps both fail.
            return _proc(1, stderr="install failed: network unreachable")

        with (
            patch(f"{_MODULE}.__file__", fake_file, create=True),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
            patch("subprocess.run", side_effect=recording_run),
        ):
            result = executor._ensure_rust_toolchain()

        assert result is False, (
            "A genuinely missing toolchain that cannot be installed must remain FATAL"
        )


# ---------------------------------------------------------------------------
# Requirement 3: xray-cli build uses a writable CARGO_HOME when
# RUST_SYSTEM_DIR exists but is read-only.
# ---------------------------------------------------------------------------


class TestCargoHomeRedirectedWhenReadOnly:
    """When RUST_SYSTEM_DIR exists but os.access(..., os.W_OK) is False, the
    cargo build step for xray-cli must use a writable CARGO_HOME (the
    user's home ~/.cargo) instead of RUST_SYSTEM_DIR, since cargo build may
    need to write to its registry cache."""

    def test_cargo_build_env_uses_writable_cargo_home_when_read_only(
        self, tmp_path: Path
    ) -> None:
        executor = _make_executor()
        fake_rust_system_dir = tmp_path / "opt_rust"
        fake_rust_system_dir.mkdir()
        (fake_rust_system_dir / "bin").mkdir()
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()
        fake_file = _fake_file_for(tmp_path)

        build_envs: list[dict] = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            if args and "cargo" in args and "build" in args:
                build_envs.append(dict(kwargs.get("env") or {}))
            return _proc(0, stdout="rustc 1.78.0")

        with (
            patch(f"{_MODULE}.__file__", fake_file, create=True),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
            patch(f"{_MODULE}.RUST_SYSTEM_DIR", fake_rust_system_dir),
            patch(f"{_MODULE}.Path.home", return_value=fake_home),
            patch(f"{_MODULE}.os.access", return_value=False),
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"),
            patch("subprocess.run", side_effect=recording_run),
        ):
            result = executor._ensure_rust_toolchain()

        assert result is True
        assert build_envs, "Expected the cargo build call to be made"
        expected_cargo_home = str(fake_home / ".cargo")
        assert build_envs[0].get("CARGO_HOME") == expected_cargo_home, (
            f"Expected CARGO_HOME={expected_cargo_home!r} for the build step "
            f"when RUST_SYSTEM_DIR is read-only, got: "
            f"{build_envs[0].get('CARGO_HOME')!r}"
        )

    def test_cargo_build_env_keeps_rust_system_dir_when_writable(
        self, tmp_path: Path
    ) -> None:
        """When RUST_SYSTEM_DIR IS writable, CARGO_HOME for the build step
        must remain RUST_SYSTEM_DIR (no behavior change for normal hosts)."""
        executor = _make_executor()
        fake_rust_system_dir = tmp_path / "opt_rust"
        fake_rust_system_dir.mkdir()
        (fake_rust_system_dir / "bin").mkdir()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()
        fake_file = _fake_file_for(tmp_path)

        build_envs: list[dict] = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            if args and "cargo" in args and "build" in args:
                build_envs.append(dict(kwargs.get("env") or {}))
            return _proc(0, stdout="rustc 1.78.0")

        with (
            patch(f"{_MODULE}.__file__", fake_file, create=True),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
            patch(f"{_MODULE}.RUST_SYSTEM_DIR", fake_rust_system_dir),
            patch(f"{_MODULE}.os.access", return_value=True),
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"),
            patch("subprocess.run", side_effect=recording_run),
        ):
            result = executor._ensure_rust_toolchain()

        assert result is True
        assert build_envs, "Expected the cargo build call to be made"
        assert build_envs[0].get("CARGO_HOME") == str(fake_rust_system_dir)


# ---------------------------------------------------------------------------
# Requirement 3 (continued): optional xray-cli build failure degrades
# gracefully instead of failing the whole deployment.
# ---------------------------------------------------------------------------


class TestXrayCliBuildFailureIsNonFatal:
    """A cargo build failure (or missing C compiler) for the OPTIONAL
    xray-cli native backend must no longer abort _ensure_rust_toolchain --
    the core deploy (pip install + restart) must still complete. The Python
    xray engine remains the working fallback."""

    def test_returns_true_when_cargo_build_fails(self, tmp_path: Path) -> None:
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()
        fake_file = _fake_file_for(tmp_path)

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            if args and "cargo" in args and "build" in args:
                return _proc(1, stderr="error[E0001]: compile error")
            return _proc(0, stdout="rustc 1.78.0")

        with (
            patch(f"{_MODULE}.__file__", fake_file, create=True),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"),
            patch("subprocess.run", side_effect=recording_run),
        ):
            result = executor._ensure_rust_toolchain()

        assert result is True, (
            "cargo build failure for the optional xray native backend must be non-fatal"
        )

    def test_returns_true_when_no_c_compiler(self, tmp_path: Path) -> None:
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()
        fake_file = _fake_file_for(tmp_path)

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            return _proc(0, stdout="rustc 1.78.0")

        with (
            patch(f"{_MODULE}.__file__", fake_file, create=True),
            patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
            patch(f"{_MODULE}.shutil.which", return_value=None),
            patch("subprocess.run", side_effect=recording_run),
        ):
            result = executor._ensure_rust_toolchain()

        assert result is True, (
            "missing C compiler for the optional xray native backend must be non-fatal"
        )
