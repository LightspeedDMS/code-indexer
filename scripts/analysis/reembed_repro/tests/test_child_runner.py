"""Self-tests for spawning and interrupting a child process group."""

import os
import signal
import sys
import textwrap

import pytest

from child_runner import InterruptSpec, run_child
from fake_voyage_server import FakeVoyageServer

KEY = "reembed-repro-fake-key"


@pytest.fixture
def server():
    srv = FakeVoyageServer(expected_api_key=KEY)
    srv.start(host="127.0.0.1", port=0)
    try:
        yield srv
    finally:
        srv.stop()


def _script(tmp_path, body):
    path = tmp_path / "child.py"
    path.write_text(textwrap.dedent(body))
    return [sys.executable, str(path)]


def test_parse_interrupt_specs():
    term = InterruptSpec.parse("SIGTERM@chunks:2000")
    kill = InterruptSpec.parse("SIGKILL@early:2.5")
    assert (term.signum, term.trigger, term.value) == (signal.SIGTERM, "chunks", 2000)
    assert (kill.signum, kill.trigger, kill.value) == (signal.SIGKILL, "early", 2.5)
    assert str(term) == "SIGTERM@chunks:2000"
    for bad in ("SIGHUP@early:1", "SIGTERM@late:1", "SIGTERM", "SIGTERM@chunks:x"):
        with pytest.raises(ValueError):
            InterruptSpec.parse(bad)


def test_uninterrupted_child_exits_zero(tmp_path, server):
    server.ledger.begin_run("r")
    result = run_child(
        _script(tmp_path, "print('hi')\n"),
        cwd=tmp_path,
        env=dict(os.environ),
        ledger=server.ledger,
        log_prefix=tmp_path / "r",
        interrupt=None,
        max_seconds=30,
    )
    assert (result.exit_code, result.interrupted) == (0, False)
    assert (tmp_path / "r.out").read_text() == "hi\n"


def test_chunks_trigger_sigterms_once_inputs_arrive(tmp_path, server):
    body = f"""
    import httpx
    with httpx.Client() as c:
        while True:
            c.post("{server.base_url}/v1/embeddings",
                   json={{"input": ["a", "b"], "model": "voyage-code-3"}},
                   headers={{"Authorization": "Bearer {KEY}"}})
    """
    server.ledger.begin_run("r")
    result = run_child(
        _script(tmp_path, body),
        cwd=tmp_path,
        env=dict(os.environ),
        ledger=server.ledger,
        log_prefix=tmp_path / "r",
        interrupt=InterruptSpec.parse("SIGTERM@chunks:10"),
        max_seconds=60,
    )
    assert result.interrupted is True
    assert result.exit_code == -signal.SIGTERM
    assert result.inputs_at_interrupt is not None
    assert result.inputs_at_interrupt >= 10


def test_early_trigger_kills_whole_process_group(tmp_path, server):
    pid_file = tmp_path / "grandchild.pid"
    body = f"""
    import subprocess, sys, time
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open({str(pid_file)!r}, "w").write(str(p.pid))
    time.sleep(60)
    """
    server.ledger.begin_run("r")
    result = run_child(
        _script(tmp_path, body),
        cwd=tmp_path,
        env=dict(os.environ),
        ledger=server.ledger,
        log_prefix=tmp_path / "r",
        interrupt=InterruptSpec.parse("SIGKILL@early:1.5"),
        max_seconds=60,
    )
    assert result.interrupted is True
    assert result.exit_code == -signal.SIGKILL
    grandchild = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild, 0)


def test_parse_planned_trigger():
    spec = InterruptSpec.parse("SIGTERM@planned:0.5")
    assert (spec.signum, spec.trigger, spec.value) == (signal.SIGTERM, "planned", 0.5)


def test_planned_trigger_fires_after_probe_turns_true(tmp_path, server):
    marker = tmp_path / "planned.marker"
    body = f"""
    import pathlib, time
    time.sleep(0.5)
    pathlib.Path({str(marker)!r}).write_text("x")
    time.sleep(60)
    """
    server.ledger.begin_run("r")
    result = run_child(
        _script(tmp_path, body),
        cwd=tmp_path,
        env=dict(os.environ),
        ledger=server.ledger,
        log_prefix=tmp_path / "r",
        interrupt=InterruptSpec.parse("SIGTERM@planned:0.2"),
        max_seconds=60,
        planned_probe=marker.exists,
    )
    assert result.interrupted is True
    assert result.exit_code == -signal.SIGTERM
    assert result.duration_s < 10


def test_planned_trigger_requires_a_probe(tmp_path, server):
    server.ledger.begin_run("r")
    with pytest.raises(ValueError, match="planned_probe"):
        run_child(
            _script(tmp_path, "pass\n"),
            cwd=tmp_path,
            env=dict(os.environ),
            ledger=server.ledger,
            log_prefix=tmp_path / "r",
            interrupt=InterruptSpec.parse("SIGTERM@planned:0"),
            max_seconds=30,
        )


def test_parse_cancel_restart_and_new_triggers():
    cancel = InterruptSpec.parse("CANCEL@chunks:5")
    restart = InterruptSpec.parse("RESTART@planned:1")
    held = InterruptSpec.parse("SIGTERM@inflight:4")
    final = InterruptSpec.parse("SIGKILL@final:1.5")
    assert (cancel.signum, cancel.name, cancel.grace) == (signal.SIGTERM, "CANCEL", 2.0)
    assert (restart.signum, restart.name, restart.grace) == (
        signal.SIGTERM,
        "RESTART",
        90.0,
    )
    assert (held.trigger, held.value) == ("inflight", 4)
    assert (final.signum, final.trigger, final.value) == (signal.SIGKILL, "final", 1.5)
    for text in (
        "CANCEL@chunks:5",
        "RESTART@planned:1.0",
        "SIGTERM@inflight:4",
        "SIGKILL@final:1.5",
    ):
        assert str(InterruptSpec.parse(text)) == text


def test_cancel_escalates_to_sigkill_after_its_grace(tmp_path, server):
    body = """
    import signal, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
    """
    server.ledger.begin_run("r")
    result = run_child(
        _script(tmp_path, body),
        cwd=tmp_path,
        env=dict(os.environ),
        ledger=server.ledger,
        log_prefix=tmp_path / "r",
        interrupt=InterruptSpec.parse("CANCEL@early:1"),
        max_seconds=60,
    )
    assert result.exit_code == -signal.SIGKILL
    assert "escalated to SIGKILL after 2.0s" in result.note
    assert result.duration_s < 15


def test_inflight_trigger_fires_while_a_response_is_held(tmp_path, server):
    body = f"""
    import httpx
    httpx.post("{server.base_url}/v1/embeddings", timeout=30,
               json={{"input": ["held"], "model": "voyage-code-3"}},
               headers={{"Authorization": "Bearer {KEY}"}})
    """
    server.ledger.begin_run("r", hold_seconds=3.0)
    result = run_child(
        _script(tmp_path, body),
        cwd=tmp_path,
        env=dict(os.environ),
        ledger=server.ledger,
        log_prefix=tmp_path / "r",
        interrupt=InterruptSpec.parse("SIGTERM@inflight:1"),
        max_seconds=60,
    )
    assert result.interrupted is True
    (req,) = server.ledger.requests("r")
    assert result.unanswered_at_kill == {req.request_id}
    assert server.ledger.kill_at("r") is not None


def test_final_trigger_fires_after_the_finalize_probe(tmp_path, server):
    marker = tmp_path / "final.marker"
    body = f"""
    import pathlib, time
    time.sleep(0.5)
    pathlib.Path({str(marker)!r}).write_text("x")
    time.sleep(60)
    """
    server.ledger.begin_run("r")
    result = run_child(
        _script(tmp_path, body),
        cwd=tmp_path,
        env=dict(os.environ),
        ledger=server.ledger,
        log_prefix=tmp_path / "r",
        interrupt=InterruptSpec.parse("SIGKILL@final:0.2"),
        max_seconds=60,
        finalize_probe=marker.exists,
    )
    assert (result.interrupted, result.exit_code) == (True, -signal.SIGKILL)


def test_child_finishing_before_trigger_is_not_interrupted(tmp_path, server):
    server.ledger.begin_run("r")
    result = run_child(
        _script(tmp_path, "pass\n"),
        cwd=tmp_path,
        env=dict(os.environ),
        ledger=server.ledger,
        log_prefix=tmp_path / "r",
        interrupt=InterruptSpec.parse("SIGTERM@chunks:5"),
        max_seconds=30,
    )
    assert (result.exit_code, result.interrupted) == (0, False)
    assert "exited before" in result.note
