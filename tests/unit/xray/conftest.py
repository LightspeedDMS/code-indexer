"""Pytest configuration for tests/unit/xray/ -- event loop cleanup.

Bug #1817: several xray tests drive an async call through
``XRaySearchEngine``'s ``_run_async_in_sync`` helper
(``src/code_indexer/xray/search_engine.py``). When no event loop is already
running on the calling (pytest main) thread, that helper falls back to
``asyncio.run(coro)``. CPython's ``asyncio.run()`` unconditionally clears the
calling thread's event loop in its own ``finally`` block once the coroutine
completes -- regardless of what the loop looked like before it was called.

Left uncleaned, that state survives past the end of ``tests/unit/xray/``'s
own test run and poisons whatever runs next in the same pytest process: the
deprecated ``asyncio.get_event_loop()`` API only auto-creates a fresh loop
the very first time it is ever called on a thread, so once any earlier
``asyncio.run()`` call has cleared the loop, later callers of
``asyncio.get_event_loop()`` raise ``RuntimeError: There is no current event
loop in thread 'MainThread'.`` -- exactly what happened to 16 tests under
``tests/unit/server/mcp/`` in the combined selection
``pytest tests/unit/xray/ tests/unit/server/mcp/``.

This mirrors the project's existing precedent for this exact class of bug:
``tests/unit/remote/conftest.py``'s ``cleanup_event_loop`` fixture.
"""

import asyncio
import shutil
import subprocess

import pytest

# Upper bound on the fallback `cargo build --release` this module runs when
# the xray-cli binary is missing (Bug #1827, L-5) -- generous enough for a
# cold build of the small xray-core/xray-cli crates on a slower CI host.
_XRAY_CLI_BUILD_TIMEOUT_SECONDS = 600


def require_xray_cli_binary() -> None:
    """Ensure the real xray-cli release binary exists, building it if
    needed, so real-binary acceptance tests never silently skip.

    Bug #1827 remediation (L-5): this test class is the CENTRAL proof
    that a real rustc compile failure surfaces its diagnostic through
    xray_search -- the previous behaviour (pytest.skip when the binary
    was missing) let a fresh checkout's suite read green while
    contributing nothing (CLAUDE.md's "green suite proves nothing" trap;
    CI's `test` job is a 3-file smoke that never builds/runs this file
    either). This project's workflow REQUIRES ./rust-automation.sh
    (which needs cargo) whenever rust/ is touched, so cargo is expected
    on PATH in the normal development flow -- attempt a real build when
    the binary is missing, and skip ONLY when cargo itself is genuinely
    unavailable (a Rust-less environment). A build that IS attempted but
    FAILS raises loudly via pytest.fail, never a silent skip.
    """
    from code_indexer.xray.rust_backend import _PROJECT_ROOT, _XRAY_CLI_DEFAULT

    if _XRAY_CLI_DEFAULT.exists():
        return

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
    if result.returncode != 0 or not _XRAY_CLI_DEFAULT.exists():
        pytest.fail(
            "xray-cli release binary is required for this test and the "
            f"automatic 'cargo build --release' failed (exit "
            f"{result.returncode}):\n{result.stdout}\n{result.stderr}"
        )


@pytest.fixture(scope="function", autouse=True)
def cleanup_event_loop():
    """Ensure the main thread has a fresh, usable event loop after each xray
    test, regardless of whether the test (or code it called) ran
    asyncio.run() and left the loop cleared (Bug #1817)."""
    yield

    # Close any lingering event loop left behind by the test.
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.stop()
        if not loop.is_closed():
            loop.close()
    except RuntimeError:
        # No event loop exists (e.g. asyncio.run() already cleared it) --
        # this is precisely the state we are here to repair.
        pass

    # Always leave a fresh, open event loop set for whatever runs next.
    asyncio.set_event_loop(asyncio.new_event_loop())
