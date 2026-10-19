"""A timed-out Codex run is stopped through the shared process-group helper.

Real processes: a stand-in ``codex`` first on PATH traps SIGTERM (recording
that it received it) and starts a child in its group. The shared helper
(``utils/process_group.terminate_process_group``) sends SIGTERM first and
watches the whole group, so the stand-in sees SIGTERM and nothing of the
group survives the call.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from code_indexer.server.services.codex_invoker import CodexInvoker

_FAKE_CODEX = """#!/bin/bash
sleep 300 &
echo $! > "$CODEX_CHILD_PID"
trap 'echo term > "$CODEX_TERM_MARK"; kill $!; exit 0' TERM
wait
"""


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def test_timeout_stops_the_codex_group_with_sigterm_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(_FAKE_CODEX)
    fake.chmod(0o755)
    child_pid_file = tmp_path / "child.pid"
    term_mark = tmp_path / "term.mark"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("CODEX_CHILD_PID", str(child_pid_file))
    monkeypatch.setenv("CODEX_TERM_MARK", str(term_mark))
    work = tmp_path / "work"
    work.mkdir()

    started = time.monotonic()
    result = CodexInvoker(codex_home=str(tmp_path / "codex-home")).invoke(
        flow="describe", cwd=str(work), prompt="example prompt", timeout=1
    )

    assert time.monotonic() - started < 20
    assert result.success is False
    assert "timed out" in (result.error or "")
    assert term_mark.exists(), "the group was SIGKILLed without a SIGTERM first"
    child = int(child_pid_file.read_text())
    assert not _alive(child), f"codex child {child} outlived the timeout"


_DETACHED_HOLDER = """#!/bin/bash
setsid sleep 60 &
echo $! > "$CODEX_CHILD_PID"
sleep 300
"""


def test_the_post_termination_drain_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detached child outside the group keeps stdout open: the drain after
    terminating the group must not wait for it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(_DETACHED_HOLDER)
    fake.chmod(0o755)
    child_pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("CODEX_CHILD_PID", str(child_pid_file))
    work = tmp_path / "work"
    work.mkdir()

    started = time.monotonic()
    try:
        result = CodexInvoker(codex_home=str(tmp_path / "codex-home")).invoke(
            flow="describe", cwd=str(work), prompt="example prompt", timeout=1
        )
        elapsed = time.monotonic() - started
    finally:
        if child_pid_file.exists():
            try:
                os.kill(int(child_pid_file.read_text()), 9)
            except OSError:
                pass
    assert result.success is False
    assert elapsed < 20, f"the drain waited {elapsed:.0f}s for a detached child"
