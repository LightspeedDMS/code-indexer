from __future__ import annotations

import faulthandler
import os
import signal
import subprocess
import sys
import textwrap
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Dict, Iterator, List, Optional, Tuple

import pytest

from code_indexer.server.utils import crash_dump
from code_indexer.server.utils.stall_watchdog_evidence import read_start_ticks

SRC_DIR = Path(__file__).resolve().parents[4] / "src"
CHILD_TIMEOUT_SECONDS = 60


@pytest.fixture
def restore_faulthandler(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(crash_dump, "_active", None)
    yield
    signal.signal(signal.SIGUSR1, signal.SIG_DFL)
    faulthandler.enable(file=2, all_threads=True)


def _own_dump_name() -> str:
    return f"faulthandler-{os.getpid()}-{read_start_ticks(os.getpid())}.log"


def _enable(logs_dir: Path) -> Optional[Path]:
    prepared = crash_dump.prepare_dump_file(logs_dir)
    return None if prepared is None else crash_dump.arm_crash_dumps(prepared)


def test_enable_points_faulthandler_at_a_per_process_file(
    tmp_path: Path, restore_faulthandler: None
) -> None:
    logs_dir = tmp_path / "logs"

    path = _enable(logs_dir)

    assert path == logs_dir / _own_dump_name()
    assert path.is_file()
    assert faulthandler.is_enabled()


def test_enable_same_dir_is_idempotent(
    tmp_path: Path, restore_faulthandler: None
) -> None:
    first = _enable(tmp_path / "logs")
    second = _enable(tmp_path / "logs")

    assert first is not None and first == second


def test_enable_in_a_new_dir_moves_dumps_there_without_warning(
    tmp_path: Path, restore_faulthandler: None, caplog: pytest.LogCaptureFixture
) -> None:
    first = _enable(tmp_path / "first-app" / "logs")
    assert first is not None and crash_dump._active is not None
    old_file = crash_dump._active.file
    second_dir = tmp_path / "second-app" / "logs"

    with caplog.at_level("WARNING", logger=crash_dump.__name__):
        second = _enable(second_dir)

    assert second == second_dir / _own_dump_name()
    assert second.is_file()
    assert not old_file.closed
    os.kill(os.getpid(), signal.SIGUSR1)
    assert crash_dump.SECTION_HEADER in second.read_text()
    assert first.read_text() == ""
    assert caplog.records == []


def test_dir_move_off_main_thread_keeps_sigusr1_and_does_not_warn(
    tmp_path: Path, restore_faulthandler: None, caplog: pytest.LogCaptureFixture
) -> None:
    first = _enable(tmp_path / "first-app" / "logs")
    assert first is not None
    result: Dict[str, Optional[Path]] = {}
    worker = threading.Thread(
        target=lambda: result.update(path=_enable(tmp_path / "second-app" / "logs"))
    )

    with caplog.at_level("WARNING", logger=crash_dump.__name__):
        worker.start()
        worker.join(timeout=CHILD_TIMEOUT_SECONDS)

    second = result["path"]
    assert second == tmp_path / "second-app" / "logs" / _own_dump_name()
    assert caplog.records == []
    os.kill(os.getpid(), signal.SIGUSR1)
    assert crash_dump.SECTION_HEADER in second.read_text()


def test_racing_second_prepare_of_same_path_closes_only_its_duplicate(
    tmp_path: Path, restore_faulthandler: None
) -> None:
    first = crash_dump.prepare_dump_file(tmp_path / "logs")
    second = crash_dump.prepare_dump_file(tmp_path / "logs")
    assert first is not None and second is not None
    assert first.file is not second.file

    assert crash_dump.arm_crash_dumps(first) == first.path
    assert crash_dump.arm_crash_dumps(second) == first.path

    assert second.file.closed
    assert not first.file.closed
    assert crash_dump._active is first


def test_move_points_faulthandler_at_new_file_before_touching_old(
    tmp_path: Path, restore_faulthandler: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _enable(tmp_path / "first-app" / "logs")
    assert first is not None and crash_dump._active is not None
    old_file = crash_dump._active.file
    real_enable = faulthandler.enable
    armed: List[Tuple[str, bool]] = []

    def spy_enable(file: IO[str], all_threads: bool) -> None:
        armed.append((file.name, old_file.closed))
        real_enable(file=file, all_threads=all_threads)

    monkeypatch.setattr(faulthandler, "enable", spy_enable)
    second = _enable(tmp_path / "second-app" / "logs")
    monkeypatch.setattr(faulthandler, "enable", real_enable)

    assert second is not None
    assert armed == [(str(second), False)]
    assert not old_file.closed


def test_unwritable_logs_dir_is_logged_not_raised(
    tmp_path: Path, restore_faulthandler: None, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "logs"
    blocker.write_text("a file, not a directory")

    with caplog.at_level("WARNING"):
        assert _enable(blocker) is None

    assert "faulthandler" in caplog.text


def test_off_main_thread_keeps_fatal_dumps_enabled(
    tmp_path: Path,
    restore_faulthandler: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    result: Dict[str, Optional[Path]] = {}
    worker = threading.Thread(
        target=lambda: result.update(path=_enable(tmp_path / "logs"))
    )

    with caplog.at_level("WARNING"):
        worker.start()
        worker.join(timeout=CHILD_TIMEOUT_SECONDS)

    assert result["path"] == tmp_path / "logs" / _own_dump_name()
    assert faulthandler.is_enabled()
    assert "SIGUSR1" in caplog.text


def test_in_process_sigusr1_appends_a_section_naming_main_thread(
    tmp_path: Path, restore_faulthandler: None
) -> None:
    path = _enable(tmp_path / "logs")
    assert path is not None

    os.kill(os.getpid(), signal.SIGUSR1)

    dump = path.read_text()
    assert dump.count(crash_dump.SECTION_HEADER) == 1
    assert "'MainThread'" in dump
    assert "test_in_process_sigusr1_appends_a_section_naming_main_thread" in dump


def test_render_failure_is_written_to_the_dump_file(
    tmp_path: Path, restore_faulthandler: None
) -> None:
    path = _enable(tmp_path / "logs")
    assert path is not None

    def broken() -> str:
        raise RuntimeError("frames unavailable")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(crash_dump, "_render_all_threads", broken)
        os.kill(os.getpid(), signal.SIGUSR1)

    assert (
        "=== SIGUSR1 dump failed: RuntimeError: frames unavailable" in path.read_text()
    )


def _run_child(logs_dir: Path, body: str) -> subprocess.CompletedProcess:
    script = textwrap.dedent(
        f"""
        import os, resource, signal, sys, threading
        from pathlib import Path
        from code_indexer.server.utils.crash_dump import arm_crash_dumps, prepare_dump_file
        path = arm_crash_dumps(prepare_dump_file(Path({str(logs_dir)!r})))
        print(path, flush=True)
        worker = threading.Thread(target=threading.Event().wait, name="w", daemon=True)
        worker.start()
        """
    ) + textwrap.dedent(body)
    env = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=CHILD_TIMEOUT_SECONDS,
    )


MANY_THREADS = 120  # above faulthandler's 100-thread cap


def test_sigusr1_dumps_all_of_120_named_threads(tmp_path: Path) -> None:
    result = _run_child(
        tmp_path,
        f"""
        stop = threading.Event()
        for i in range({MANY_THREADS}):
            threading.Thread(target=stop.wait, name=f"waiter-{{i}}", daemon=True).start()
        os.kill(os.getpid(), signal.SIGUSR1)
        os.kill(os.getpid(), signal.SIGUSR1)
        print("still-running", flush=True)
        """,
    )

    assert result.returncode == 0, result.stderr
    dump_path = Path(result.stdout.splitlines()[0])
    assert result.stdout.splitlines()[1] == "still-running"
    dump = dump_path.read_text()
    sections = dump.split("=== SIGUSR1 dump")
    assert len(sections) == 3, dump[:2000]
    last = sections[2]
    for i in range(MANY_THREADS):
        assert f"'waiter-{i}'" in last, f"waiter-{i} missing"
    assert "'MainThread'" in last
    assert last.count(" in wait") >= MANY_THREADS


def test_fatal_signal_writes_all_threads_to_the_file(tmp_path: Path) -> None:
    result = _run_child(
        tmp_path,
        """
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        import faulthandler
        faulthandler._sigsegv()
        """,
    )

    assert result.returncode == -signal.SIGSEGV
    dump = Path(result.stdout.splitlines()[0]).read_text()
    assert "Fatal Python error: Segmentation fault" in dump
    assert "Thread 0x" in dump and " in wait" in dump, dump
    assert "Current thread 0x" in dump, dump


def test_sweep_removes_only_empty_files_of_dead_lifetimes(
    tmp_path: Path, restore_faulthandler: None
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    dead_pid = os.getpid() + 1_000_000
    dead_empty = logs_dir / f"faulthandler-{dead_pid}-1.log"
    dead_empty.write_text("")
    dead_evidence = logs_dir / f"faulthandler-{dead_pid}-2.log"
    dead_evidence.write_text("Fatal Python error: Segmentation fault\n")
    unrelated = logs_dir / "worker-stall-1-2.evidence"
    unrelated.write_text("")

    own = _enable(logs_dir)
    assert own is not None
    crash_dump.sweep_dead_empty_dumps(logs_dir, keep=own)

    assert own.exists()
    assert not dead_empty.exists()
    assert dead_evidence.exists()
    assert unrelated.exists()


def test_enable_failure_returns_none_and_logs(
    tmp_path: Path,
    restore_faulthandler: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("cannot install fatal handlers")

    with pytest.MonkeyPatch.context() as patcher, caplog.at_level("WARNING"):
        patcher.setattr(faulthandler, "enable", refuse)
        assert _enable(tmp_path / "logs") is None

    assert crash_dump._active is None
    assert "faulthandler crash dumps disabled" in caplog.text


def test_sweep_keeps_empty_file_of_a_live_process(
    tmp_path: Path, restore_faulthandler: None
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        live_empty = (
            logs_dir / f"faulthandler-{sleeper.pid}-{read_start_ticks(sleeper.pid)}.log"
        )
        live_empty.write_text("")

        own = _enable(logs_dir)
        assert own is not None
        crash_dump.sweep_dead_empty_dumps(logs_dir, keep=own)

        assert live_empty.exists()
    finally:
        sleeper.kill()
        sleeper.wait(timeout=CHILD_TIMEOUT_SECONDS)


def test_sweep_skips_an_entry_a_peer_unlinked_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    dead_pid = os.getpid() + 1_000_000
    victim = logs_dir / f"faulthandler-{dead_pid}-1.log"
    others = [logs_dir / f"faulthandler-{dead_pid}-{n}.log" for n in (2, 3)]
    for path in [victim, *others]:
        path.write_text("")
    real_scandir = os.scandir

    @contextmanager
    def peer_unlinks_victim(path: Any) -> Iterator[Iterator[os.DirEntry]]:
        with real_scandir(path) as it:
            listed = sorted(it, key=lambda e: e.name != victim.name)

        def entries() -> Iterator[os.DirEntry]:
            for entry in listed:
                if entry.name == victim.name:
                    victim.unlink()
                yield entry

        yield entries()

    monkeypatch.setattr(os, "scandir", peer_unlinks_victim)
    with caplog.at_level("WARNING", logger=crash_dump.__name__):
        crash_dump.sweep_dead_empty_dumps(logs_dir, keep=logs_dir / "own.log")

    assert caplog.records == []
    assert not victim.exists()
    assert [p for p in others if p.exists()] == []


def test_sweep_of_a_missing_dir_still_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = tmp_path / "logs"

    with caplog.at_level("WARNING", logger=crash_dump.__name__):
        crash_dump.sweep_dead_empty_dumps(missing, keep=missing / "own.log")

    assert "faulthandler dump sweep of" in caplog.text
    assert "FileNotFoundError" in caplog.text
