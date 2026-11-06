"""``_run_xray_cli_process`` returns only once every process it started is gone.

Real processes, no xray-cli binary needed: the "xray-cli" is a Python child
that starts a detached grandchild (its own session, standing in for rustc,
which xray-cli starts in a separate process group) and exits at once. The
grandchild's stdout/stderr go to /dev/null, so only its inherited liveness
pipe write end can hold the call open: the call must wait for it -- and the compile slot must stay held until it is gone.
"""

from __future__ import annotations

import sys
import time
from unittest.mock import patch

from cidx_xray import rust_backend
from cidx_xray.rust_backend import RustNativeBackend

_GRANDCHILD_SECONDS = 1.0

_PARENT = f"""
import subprocess, sys
subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep({_GRANDCHILD_SECONDS})"],
    start_new_session=True,
    close_fds=False,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
"""


def test_call_waits_for_detached_descendants_before_returning() -> None:
    backend = RustNativeBackend()
    released_at: list = []
    real_release = rust_backend._release_compile_slot

    def _record_release() -> None:
        released_at.append(time.monotonic())
        real_release()

    started = time.monotonic()
    with patch.object(rust_backend, "_release_compile_slot", _record_release):
        stdout, error = backend._run_xray_cli_process(
            [sys.executable, "-c", _PARENT],
            timeout_seconds=30,
            on_process_spawned=None,
            acquire_compile_slot=True,
        )
    elapsed = time.monotonic() - started

    assert error is None
    assert elapsed >= _GRANDCHILD_SECONDS * 0.9, (
        "returned while a process started by xray-cli was still running"
    )
    assert released_at and released_at[0] - started >= _GRANDCHILD_SECONDS * 0.9, (
        "compile slot released before the invocation's processes were gone"
    )


def test_spawn_passes_the_liveness_pipe_write_end() -> None:
    backend = RustNativeBackend()
    seen: dict = {}
    real_popen = rust_backend.subprocess.Popen

    def _spy(*args, **kwargs):
        seen.update(kwargs)
        return real_popen(*args, **kwargs)

    with patch.object(rust_backend.subprocess, "Popen", side_effect=_spy):
        backend._run_xray_cli_process(
            [sys.executable, "-c", "pass"],
            timeout_seconds=30,
            on_process_spawned=None,
            acquire_compile_slot=False,
        )
    assert len(seen.get("pass_fds", ())) == 1
    assert seen.get("start_new_session") is True
