"""Test helpers shared by X-Ray's own suite and the core's X-Ray integration tests.

Two test trees exercise X-Ray: the distribution's engine tests
(``packages/features/cidx-xray/tests``) and the core's server-integration tests
(``tests/unit/xray``). Neither may import the other's test modules, so the
helpers both need live here, in the package both import. Nothing in the
runtime imports this module, and ``pytest`` is imported only inside the
helper that reports through it.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from contextlib import contextmanager
from typing import Any, Iterator

# Upper bound on the fallback `cargo build --release` run when the xray-cli
# binary is missing (Bug #1827, L-5) -- generous enough for a cold build of the
# small xray-core/xray-cli crates on a slower CI host.
_XRAY_CLI_BUILD_TIMEOUT_SECONDS = 600


def require_xray_cli_binary() -> None:
    """Ensure the real xray-cli release binary exists, building it if
    needed, so real-binary acceptance tests never silently skip.

    Bug #1827 remediation (L-5): a skip when the binary was missing let a
    fresh checkout's suite read green while contributing nothing. The
    workflow requires ./rust-automation.sh (which needs cargo) whenever rust/
    is touched, so cargo is expected on PATH in the normal development flow:
    attempt a real build when the binary is missing, and skip ONLY when cargo
    itself is unavailable (a Rust-less environment). A build that IS attempted
    but FAILS fails the test loudly, never a silent skip.
    """
    import pytest

    from cidx_xray.rust_backend import (
        _PROJECT_ROOT,
        _XRAY_CLI_PATH_ENV_VAR,
        _XRAY_CLI_SOURCE_TREE_PATH,
        resolve_xray_cli_path,
    )

    # Resolve the same way production does, so an override pointing at a
    # prebuilt binary satisfies the requirement instead of triggering a build.
    resolved = resolve_xray_cli_path()
    if resolved.exists():
        return

    # An override that is set but broken must NOT be papered over by building
    # into the source tree: cargo writes to rust/target/release, which is a
    # different path, so the build would "succeed" and the test would still
    # find nothing. Fail on the real problem instead.
    if resolved != _XRAY_CLI_SOURCE_TREE_PATH:
        pytest.fail(
            f"{_XRAY_CLI_PATH_ENV_VAR} points at {resolved}, which does not "
            "exist. Fix the override or unset it to use the source-tree build."
        )

    if shutil.which("cargo") is None:
        pytest.skip(
            "cargo not found on PATH -- install Rust (https://rustup.rs) to "
            "build xray-cli and enable this test."
        )

    result = subprocess.run(
        ["cargo", "build", "--release", "--bin", "xray-cli"],
        cwd=str(_PROJECT_ROOT / "rust"),
        capture_output=True,
        text=True,
        timeout=_XRAY_CLI_BUILD_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 or not _XRAY_CLI_SOURCE_TREE_PATH.exists():
        pytest.fail(
            "xray-cli release binary is required for this test and the "
            f"automatic 'cargo build --release' failed (exit "
            f"{result.returncode}):\n{result.stdout}\n{result.stderr}"
        )


def reset_main_thread_event_loop() -> None:
    """Leave the calling thread with a fresh, open event loop (Bug #1817).

    ``XRaySearchEngine``'s sync-over-async helper falls back to
    ``asyncio.run()``, which clears the calling thread's event loop when it
    returns. Left as is, a later caller of ``asyncio.get_event_loop()`` in the
    same process raises ``RuntimeError: There is no current event loop``.
    Each X-Ray test tree calls this after every test (an autouse fixture in
    its ``conftest.py``).
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.stop()
        if not loop.is_closed():
            loop.close()
    except RuntimeError:
        # No event loop exists (asyncio.run() already cleared it) -- exactly
        # the state this repairs.
        pass
    asyncio.set_event_loop(asyncio.new_event_loop())


@contextmanager
def placement_host_installed() -> Iterator[None]:
    """Install the feature's X-Ray placement host for the block, as the
    started feature does (``on_startup``), and remove it after.

    For tests that drive ``RustNativeBackend`` through the server's wiring
    without starting the feature: the configured backend is
    ``cidx_xray.server.backend_wiring.server_xray_execution_backend()``
    (patch it to park one), the per-request decision
    ``xray_execution_backend_for``.
    """
    from cidx_xray._platform import capabilities
    from cidx_xray.server.backend_wiring import FeaturePlacementHost

    previous = capabilities.XRAY_PLACEMENT_HOST.get()
    capabilities.XRAY_PLACEMENT_HOST.install(FeaturePlacementHost())
    try:
        yield
    finally:
        if previous is None:
            capabilities.XRAY_PLACEMENT_HOST.uninstall()
        else:
            capabilities.XRAY_PLACEMENT_HOST.install(previous)


def sqlite_k_calibration_backend(db_path: str) -> Any:
    """A ``SqliteKCalibrationBackend`` over the SQLite file *db_path*, with
    the feature's migration 1 applied (as the core applies it to the server
    database before ``on_startup`` builds the store)."""
    import sqlite3

    from cidx_xray.server.graph_governor.k_calibration_store import (
        SqliteKCalibrationBackend,
    )
    from cidx_xray.server.migrations import XRAY_MIGRATIONS

    setup = sqlite3.connect(db_path)
    try:
        for migration in XRAY_MIGRATIONS:
            migration.apply(setup)
        setup.commit()
    finally:
        setup.close()

    @contextmanager
    def connect() -> Iterator[Any]:
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    return SqliteKCalibrationBackend(connect)
