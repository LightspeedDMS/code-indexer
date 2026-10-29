import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "unit_lane_chunks.py"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("unit_lane_chunks", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["unit_lane_chunks"] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()

# ^ external row shapes = pytest --durations=0 output (setup/call/teardown, param ids with spaces)
_DURATIONS_LOG = """\
tests/unit/a/test_alpha.py::test_prints_a_fake_row PASSED
9.99s call     tests/unit/a/test_noise.py::test_before_header_is_not_a_row
============================== slowest durations ===============================
59.20s call     tests/unit/a/test_alpha.py::test_detect_deletions_persists_the_path_index
41.12s setup    tests/unit/a/test_alpha.py::test_failed_search_exits_non_zero[semantic]
1.02s teardown tests/unit/b/test_beta.py::TestCase::test_snapshot_raises_when_contention_outlasts
9.48s call     tests/unit/b/test_beta.py::TestX::test_unrestorable[sqlite-corrupt-failed its integrity check]
0.01s call     tests/unit/c/test_gamma.py::test_quick
(49981 durations < 0.005s hidden.  Use -vv to show these durations.)
= 19 failed, 20974 passed, 260 skipped in 7906.64s (2:11:46) =
"""


def test_parse_durations_sums_setup_call_teardown_per_file() -> None:
    durations = tool.parse_durations(_DURATIONS_LOG.splitlines())
    assert durations == pytest.approx(
        {
            "tests/unit/a/test_alpha.py": 59.20 + 41.12,
            "tests/unit/b/test_beta.py": 1.02 + 9.48,
            "tests/unit/c/test_gamma.py": 0.01,
        }
    )


def test_parse_durations_reads_the_top_n_header_of_server_fast_logs() -> None:
    lines = [
        "============================= slowest 10 durations =============================",
        "2.50s call     tests/unit/server/test_x.py::test_one",
    ]
    assert tool.parse_durations(lines) == {"tests/unit/server/test_x.py": 2.5}


def test_parse_durations_without_a_durations_section_is_empty() -> None:
    assert tool.parse_durations(["1.00s call     tests/unit/a/test_a.py::t"]) == {}


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _rows(rows: str) -> str:
    return "=== slowest durations ===\n" + rows


def test_history_takes_each_file_from_its_newest_run(tmp_path: Path) -> None:
    _write(
        tmp_path / "fast-automation-20261001_100000.log",
        _rows(
            "5.00s call     tests/unit/test_a.py::t\n"
            "7.00s call     tests/unit/test_old_only.py::t\n"
        ),
    )
    _write(
        tmp_path / "fast-automation-20261002_100000-chunk1.log",
        _rows("2.00s call     tests/unit/test_a.py::t\n"),
    )
    _write(
        tmp_path / "fast-automation-20261002_100000-chunk2.log",
        _rows("3.00s call     tests/unit/test_b.py::t\n"),
    )
    durations = tool.load_history_durations(
        tmp_path, ["fast-automation-*.log"], lookback=3
    )
    assert durations == {
        "tests/unit/test_a.py": 2.0,
        "tests/unit/test_b.py": 3.0,
        "tests/unit/test_old_only.py": 7.0,
    }


def test_history_skips_runs_without_rows_and_honours_lookback(tmp_path: Path) -> None:
    _write(
        tmp_path / "fast-automation-20261001_100000.log",
        _rows("1.00s call     tests/unit/test_oldest.py::t\n"),
    )
    _write(
        tmp_path / "fast-automation-20261002_100000.log",
        _rows("1.00s call     tests/unit/test_middle.py::t\n"),
    )
    _write(tmp_path / "fast-automation-20261003_100000.log", "killed early\n")
    durations = tool.load_history_durations(
        tmp_path, ["fast-automation-*.log"], lookback=1
    )
    assert durations == {"tests/unit/test_middle.py": 1.0}


def test_history_with_no_logs_is_empty(tmp_path: Path) -> None:
    assert tool.load_history_durations(tmp_path / "absent", ["x-*.log"], 3) == {}


def test_read_ignore_file_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    ignore_file = _write(
        tmp_path / "ignores.txt",
        "# header\n\ntests/unit/server/\n  tests/unit/test_x.py  \n# tail\n",
    )
    assert tool.read_ignore_file(ignore_file) == [
        "tests/unit/server/",
        "tests/unit/test_x.py",
    ]


def _tree(root: Path, *names: str) -> None:
    for name in names:
        _write(root / name, "def test_ok():\n    pass\n")


def test_select_walks_test_files_and_applies_ignores(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        "tests/unit/test_root.py",
        "tests/unit/helper.py",
        "tests/unit/a/test_a.py",
        "tests/unit/a/test_skip_me.py",
        "tests/unit/server/test_server.py",
        "tests/unit/.hidden/test_hidden.py",
        "tests/unit/__pycache__/test_cached.py",
        "tests/unit/b/test_b.py",
    )
    selected = tool.select_test_files(
        tmp_path,
        ["tests/unit/"],
        ["tests/unit/server/", "tests/unit/a/test_skip_me.py"],
    )
    assert selected == [
        "tests/unit/a/test_a.py",
        "tests/unit/b/test_b.py",
        "tests/unit/test_root.py",
    ]


def test_select_keeps_an_explicit_file_like_pytest(tmp_path: Path) -> None:
    _tree(tmp_path, "tests/unit/test_one.py")
    assert tool.select_test_files(
        tmp_path, ["tests/unit/test_one.py"], ["tests/unit/test_one.py"]
    ) == ["tests/unit/test_one.py"]


def test_select_explicit_dir_inside_an_ignored_dir_is_kept_like_pytest(
    tmp_path: Path,
) -> None:
    _tree(
        tmp_path,
        "tests/unit/server/test_top.py",
        "tests/unit/server/sub/test_deep.py",
        "tests/unit/server/skip/test_skipped.py",
    )
    ignores = ["tests/unit/server/", "tests/unit/server/skip/"]
    assert tool.select_test_files(tmp_path, ["tests/unit/server/"], ignores) == [
        "tests/unit/server/sub/test_deep.py",
        "tests/unit/server/test_top.py",
    ]
    assert tool.select_test_files(tmp_path, ["tests/unit/server/sub/"], ignores) == [
        "tests/unit/server/sub/test_deep.py"
    ]


def test_select_rejects_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        tool.select_test_files(tmp_path, ["tests/nope/"], [])


def _loads(chunks) -> list:
    return [round(chunk.seconds, 6) for chunk in chunks]


def test_plan_lpt_balances_a_synthetic_table() -> None:
    durations = {f"t/test_{n}.py": float(n) for n in (8, 7, 6, 5, 4, 3, 2, 1)}
    chunks = tool.plan_chunks(sorted(durations), durations, 3)
    assert sorted(_loads(chunks)) == [11.0, 12.0, 13.0]
    assert sum(len(chunk.files) for chunk in chunks) == 8
    assert max(_loads(chunks)) <= sum(durations.values()) / 3 + max(durations.values())


def test_plan_spreads_unmeasured_files_round_robin() -> None:
    files = ["t/test_big.py"] + [f"t/test_u{n}.py" for n in range(6)]
    chunks = tool.plan_chunks(files, {"t/test_big.py": 100.0}, 3)
    assert [chunk.unmeasured for chunk in chunks] == [2, 2, 2]
    assert sorted(_loads(chunks)) == [0.0, 0.0, 100.0]


def test_plan_without_history_splits_by_file_count() -> None:
    files = [f"t/test_{n:02d}.py" for n in range(10)]
    chunks = tool.plan_chunks(files, {}, 4)
    assert [len(chunk.files) for chunk in chunks] == [3, 3, 2, 2]
    assert sorted(f for chunk in chunks for f in chunk.files) == files


def test_plan_is_deterministic_whatever_the_input_order() -> None:
    durations = {f"t/test_{n}.py": float(n % 4) for n in range(40)}
    files = sorted(durations) + ["t/test_unknown_a.py", "t/test_unknown_b.py"]
    first = tool.plan_chunks(files, durations, 5)
    second = tool.plan_chunks(list(reversed(files)), durations, 5)
    assert [c.files for c in first] == [c.files for c in second]
    assert all(c.files == sorted(c.files) for c in first)


def test_plan_drops_empty_chunks() -> None:
    # ^ hazard an empty chunk => pytest with no file args collects the whole testpaths tree
    chunks = tool.plan_chunks(["t/test_a.py", "t/test_b.py"], {}, 8)
    assert [c.files for c in chunks] == [["t/test_a.py"], ["t/test_b.py"]]


def test_plan_rejects_fewer_than_one_chunk() -> None:
    with pytest.raises(ValueError):
        tool.plan_chunks(["t/test_a.py"], {}, 0)


@pytest.mark.parametrize(
    "codes, expected",
    [
        ([0, 0, 0], 0),
        ([0, 1, 0], 1),
        ([1, 2], 1),
        ([0, 5], 0),
        ([1, 5], 1),
        ([-15, 0], 1),
        ([0, 130], 1),
        ([5, 5], 5),
    ],
)
def test_lane_exit_code(codes, expected) -> None:
    assert tool.lane_exit_code(codes) == expected


def test_lane_exit_code_needs_at_least_one_chunk() -> None:
    with pytest.raises(ValueError):
        tool.lane_exit_code([])


def _run_main(root: Path, tmp_root: Path, *pytest_args: str) -> int:
    scratch_root = root / "scratch"
    scratch_root.mkdir(exist_ok=True)
    return int(tool.main(
        [
            "run", *_selection_args(root),
            "--log-prefix", "lane", "--timestamp", "20261028_140000",
            "--scratch-root", str(scratch_root), "--tmp-root", str(tmp_root),
            "--guard", str(_GUARD),
            "--", "-p", "no:cacheprovider", "-q", *pytest_args,
        ]
    ))  # fmt: skip


def test_main_run_with_nothing_selected_fails_loudly(
    tmp_path: Path, capsys, shm_root
) -> None:
    _cli_tree(tmp_path)
    assert _run_main(tmp_path, shm_root, "-k", "no_such_test_name") == 5
    out = capsys.readouterr().out
    assert out.count("EMPTY (exit 5)") == 2 and "PASS" not in out
    assert "no tests ran in any chunk" in out
    assert list(shm_root.iterdir()) == []


def test_main_run_reports_empty_chunks_as_empty(
    tmp_path: Path, capsys, shm_root
) -> None:
    _cli_tree(tmp_path)
    assert _run_main(tmp_path, shm_root, "-k", "test_a") == 0
    out = capsys.readouterr().out
    assert "chunk 1: EMPTY (exit 5)" in out and "chunk 2: PASS (exit 0)" in out
    assert list(shm_root.iterdir()) == []


def test_summarize_log_reads_the_final_summary_line(tmp_path: Path) -> None:
    log = _write(
        tmp_path / "chunk.log",
        "tests/unit/test_a.py::test_x FAILED\n"
        "= 19 failed, 20974 passed, 260 skipped, 251 deselected, 3 errors in 7906.64s (2:11:46) =\n",
    )
    summary = tool.summarize_log(log)
    assert (summary.passed, summary.failed, summary.errors, summary.skipped) == (
        20974,
        19,
        3,
        260,
    )
    assert "7906.64s" in summary.line


def test_summarize_log_reads_quiet_mode_lines(tmp_path: Path) -> None:
    log = _write(tmp_path / "chunk.log", "....\n1 error, 4 passed in 0.50s\n")
    summary = tool.summarize_log(log)
    assert (summary.passed, summary.errors) == (4, 1)


_GUARD = _PROJECT_ROOT / "scripts" / "real-server-home-guard.sh"

_PROBE = """\
import json, os, sys
out = os.path.join(os.environ["PROBE_OUT"], os.environ["CIDX_DATA_DIR"].replace("/", "_"))
keys = ("HOME", "CIDX_SERVER_DATA_DIR", "CIDX_DATA_DIR", "CIDX_REAL_HOME_GUARD_DIR", "TMPDIR")
ids = {"pid": os.getpid(), "pgrp": os.getpgrp(), "sid": os.getsid(0)}
with open(out, "w") as handle:
    record = {"env": {k: os.environ.get(k) for k in keys}, "args": sys.argv[1:]}
    json.dump(dict(record, ids=ids), handle)
print("2 passed in 0.10s")
sys.exit(1 if any("fail" in arg for arg in sys.argv[1:]) else 0)
"""


def _unlock_tree(root: Path) -> None:
    for dirpath, _dirs, _files in os.walk(root):
        os.chmod(dirpath, 0o700)


@pytest.fixture
def shm_root():
    root = Path(tempfile.mkdtemp(prefix="cidx-lane-test-", dir="/dev/shm"))
    yield root
    _unlock_tree(root)
    shutil.rmtree(root)


def _run_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shm_root: Path):
    probe = _write(tmp_path / "probe.py", _PROBE)
    out = tmp_path / "out"
    out.mkdir()
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    monkeypatch.setenv("CIDX_REAL_HOME_GUARD_DIR", str(tmp_path / "decoy"))
    chunks = [tool.Chunk(files=["t/test_ok.py"]), tool.Chunk(files=["t/test_fail.py"])]
    logs = [tmp_path / "logs" / f"chunk{k}.log" for k in (1, 2)]
    results = tool.run_chunks(
        chunks,
        command=[sys.executable, str(probe)],
        common_args=["-m", "not slow"],
        log_paths=logs,
        scratch_root=scratch_root,
        tmp_root=shm_root,
        guard=_GUARD,
        extra_env={"PROBE_OUT": str(out)},
    )
    seen = [json.loads(p.read_text()) for p in sorted(out.iterdir())]
    return results, seen, scratch_root, logs


def test_run_chunks_isolates_each_chunk(tmp_path: Path, monkeypatch, shm_root) -> None:
    results, seen, scratch_root, logs = _run_probe(tmp_path, monkeypatch, shm_root)
    assert len(seen) == 2
    homes = {record["env"]["HOME"] for record in seen}
    data_dirs = {record["env"]["CIDX_SERVER_DATA_DIR"] for record in seen}
    tmpdirs = {record["env"]["TMPDIR"] for record in seen}
    assert len(homes) == 2 and len(data_dirs) == 2 and len(tmpdirs) == 2
    for record in seen:
        env = record["env"]
        assert env["HOME"].startswith(str(scratch_root) + "/")
        assert env["CIDX_SERVER_DATA_DIR"].startswith(str(scratch_root) + "/")
        assert env["CIDX_DATA_DIR"] == env["CIDX_SERVER_DATA_DIR"]
        assert env["CIDX_REAL_HOME_GUARD_DIR"] is None
        assert env["TMPDIR"].startswith(str(shm_root) + "/")
        ids = record["ids"]
        assert ids["pgrp"] == ids["pid"] and ids["sid"] != os.getsid(0)
    assert sorted(record["args"] for record in seen) == [
        ["-m", "not slow", "t/test_fail.py"],
        ["-m", "not slow", "t/test_ok.py"],
    ]


def test_run_chunks_reports_exit_codes_logs_and_cleans_scratch(
    tmp_path: Path, monkeypatch, shm_root
) -> None:
    results, _, scratch_root, logs = _run_probe(tmp_path, monkeypatch, shm_root)
    assert [r.exit_code for r in results] == [0, 1]
    assert [r.log for r in results] == logs
    assert all("2 passed" in log.read_text() for log in logs)
    assert [r.summary.passed for r in results] == [2, 2]
    assert all(r.wall_seconds >= 0 for r in results)
    assert tool.lane_exit_code([r.exit_code for r in results]) == 1
    assert list(scratch_root.iterdir()) == []
    assert list(shm_root.iterdir()) == []


_WAITS_FOR_THE_OTHER_TMPDIR = """\
import os, sys, time
out, mode = sys.argv[1], sys.argv[2]
marker = os.path.join(out, "quick")


def write(path, text):
    with open(path, "w") as handle:
        handle.write(text)


if mode == "quick":
    write(marker, os.environ["TMPDIR"])
    sys.exit(0)
verdict = "still present"
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    if os.path.exists(marker):
        with open(marker) as handle:
            quick_tmpdir = handle.read()
        if quick_tmpdir and not os.path.exists(quick_tmpdir):
            verdict = "gone"
            break
    time.sleep(0.05)
write(os.path.join(out, "verdict"), verdict)
"""


def test_run_chunks_removes_a_chunk_tmpdir_when_that_chunk_ends(
    tmp_path: Path, shm_root
) -> None:
    script = _write(tmp_path / "waits.py", _WAITS_FOR_THE_OTHER_TMPDIR)
    out = tmp_path / "out"
    out.mkdir()
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    results = tool.run_chunks(
        # ^ invariant run_chunks appends chunk.files to command => mode = sys.argv[2]
        [tool.Chunk(files=["quick"]), tool.Chunk(files=["patient"])],
        command=[sys.executable, str(script), str(out)],
        common_args=[],
        log_paths=[tmp_path / "chunk1.log", tmp_path / "chunk2.log"],
        scratch_root=scratch_root,
        tmp_root=shm_root,
        guard=_GUARD,
        extra_env={},
    )
    assert [r.exit_code for r in results] == [0, 0]
    assert (out / "verdict").read_text() == "gone"
    assert list(shm_root.iterdir()) == []


def _pids_gone(pids) -> bool:
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        return False
    return True


_LEAVES_A_GRANDCHILD = """\
import subprocess, sys
child = subprocess.Popen(["sleep", "60"])
open(sys.argv[1], "w").write(str(child.pid))
"""


def test_run_chunks_reaps_descendants_of_a_chunk_that_already_exited(
    tmp_path: Path, shm_root
) -> None:
    script = _write(tmp_path / "leaves.py", _LEAVES_A_GRANDCHILD)
    marker = tmp_path / "grandchild.pid"
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    results = tool.run_chunks(
        # ^ invariant run_chunks appends chunk.files to command => marker = sys.argv[1]
        [tool.Chunk(files=[str(marker)])],
        command=[sys.executable, str(script)],
        common_args=[],
        log_paths=[tmp_path / "chunk1.log"],
        scratch_root=scratch_root,
        tmp_root=shm_root,
        guard=_GUARD,
        extra_env={},
    )
    assert [r.exit_code for r in results] == [0]
    grandchild = int(marker.read_text())
    assert _wait_until(lambda: _pids_gone([grandchild]), 10)


def test_run_chunks_needs_one_log_per_chunk(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        tool.run_chunks(
            [tool.Chunk(files=["t/test_a.py"])],
            command=["true"],
            common_args=[],
            log_paths=[],
            scratch_root=tmp_path,
            tmp_root=tmp_path,
            guard=_GUARD,
            extra_env={},
        )


def _open_lock(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


def test_lock_refuses_a_second_holder_until_the_first_closes(tmp_path: Path) -> None:
    lock = tmp_path / "lanes.lock"
    first, second = _open_lock(lock), _open_lock(lock)
    try:
        tool.acquire_lane_lock(first, "fast", 4242)
        assert lock.read_text().split() == ["4242", "fast"]
        with pytest.raises(tool.LaneBusy) as busy:
            tool.acquire_lane_lock(second, "slow", 4343)
        assert "fast" in str(busy.value) and "4242" in str(busy.value)
        os.close(first)
        first = -1
        tool.acquire_lane_lock(second, "slow", 4343)
        assert lock.read_text().split() == ["4343", "slow"]
    finally:
        for fd in (first, second):
            if fd >= 0:
                os.close(fd)


_HOLDER = 'exec 9<>"$1"; "$2" "$3" lock --fd 9 --lane "$4" --pid $$ || exit $?; exec sleep "$5"'


def _holder(lock: Path, lane: str, hold_seconds: int) -> "subprocess.Popen[bytes]":
    return subprocess.Popen(
        ["bash", "-c", _HOLDER, "_", str(lock), sys.executable, str(_SCRIPT_PATH)]
        + [lane, str(hold_seconds)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_concurrent_lanes_get_exactly_one_lock(tmp_path: Path) -> None:
    lock = tmp_path / "lanes.lock"
    holders = [_holder(lock, f"lane{n}", 3) for n in range(8)]
    codes = sorted(holder.wait(timeout=60) for holder in holders)
    assert codes == [0] + [3] * 7


def test_a_killed_holder_frees_the_lock(tmp_path: Path) -> None:
    lock = tmp_path / "lanes.lock"
    holder = _holder(lock, "fast", 60)
    try:
        assert _wait_until(lambda: lock.exists() and "fast" in lock.read_text(), 30)
        second = _open_lock(lock)
        try:
            with pytest.raises(tool.LaneBusy):
                tool.acquire_lane_lock(second, "slow", 1)
            holder.kill()
            holder.wait(timeout=30)
            tool.acquire_lane_lock(second, "slow", 1)
        finally:
            os.close(second)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


def _cli_tree(root: Path) -> None:
    _write(root / "tests/unit/test_pass_a.py", "def test_a():\n    assert True\n")
    _write(root / "tests/unit/test_pass_b.py", "def test_b():\n    assert True\n")
    _write(root / "tests/unit/sub/test_fails.py", "def test_c():\n    assert False\n")
    _write(
        root / "tests/unit/ignored/test_never.py", "def test_d():\n    assert False\n"
    )
    _write(root / "ignores.txt", "# lane ignores\ntests/unit/ignored/\n")


def _selection_args(root: Path) -> list:
    return [
        "--root", str(root),
        "--paths", "tests/unit/",
        "--ignore-file", str(root / "ignores.txt"),
        "--chunks", "2",
        "--history-dir", str(root / "telemetry"),
        "--history-glob", "lane-*.log",
    ]  # fmt: skip


def test_main_plan_prints_the_plan_and_writes_lists(tmp_path: Path, capsys) -> None:
    _cli_tree(tmp_path)
    lists = tmp_path / "lists"
    rc = tool.main(["plan", *_selection_args(tmp_path), "--list-out", str(lists)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "chunk 1:" in out and "chunk 2:" in out and "3 files" in out
    listed = sorted(
        line
        for path in sorted(lists.glob("chunk*.txt"))
        for line in path.read_text().splitlines()
    )
    assert listed == [
        "tests/unit/sub/test_fails.py",
        "tests/unit/test_pass_a.py",
        "tests/unit/test_pass_b.py",
    ]


def test_main_run_executes_isolated_pytest_chunks(
    tmp_path: Path, capsys, shm_root
) -> None:
    _cli_tree(tmp_path)
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    rc = tool.main(
        [
            "run",
            *_selection_args(tmp_path),
            "--log-prefix", "lane",
            "--timestamp", "20261028_120000",
            "--scratch-root", str(scratch_root),
            "--tmp-root", str(shm_root),
            "--guard", str(_GUARD),
            "--",
            "-p", "no:cacheprovider",
            "--durations=0",
            "-q",
        ]
    )  # fmt: skip
    out = capsys.readouterr().out
    assert rc == 1
    logs = sorted((tmp_path / "telemetry").glob("lane-20261028_120000-chunk*.log"))
    assert [log.name for log in logs] == [
        "lane-20261028_120000-chunk1.log",
        "lane-20261028_120000-chunk2.log",
    ]
    assert all("slowest durations" in log.read_text() for log in logs)
    assert "FAIL" in out and "PASS" in out
    assert "2 passed" in out and "1 failed" in out
    assert list(scratch_root.iterdir()) == []
    assert list(shm_root.iterdir()) == []


def test_main_lock_takes_an_fd_and_refuses_a_second_holder(
    tmp_path: Path, capsys
) -> None:
    lock = tmp_path / "lanes.lock"
    first, second = _open_lock(lock), _open_lock(lock)
    try:
        assert (
            tool.main(["lock", "--fd", str(first), "--lane", "fast", "--pid", "11"])
            == 0
        )
        assert (
            tool.main(["lock", "--fd", str(second), "--lane", "slow", "--pid", "12"])
            == 3
        )
        assert "two lanes never run at once" in capsys.readouterr().err
    finally:
        os.close(first)
        os.close(second)


def _wait_until(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return bool(predicate())


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_run_stops_the_whole_chunk_group_on_signal(
    tmp_path: Path, signum, shm_root
) -> None:
    marker = tmp_path / "chunk.pid"
    _write(
        tmp_path / "tests/unit/test_sleeps.py",
        "import os, subprocess, time\n"
        "def test_sleeps():\n"
        "    child = subprocess.Popen(['sleep', '60'])\n"
        f"    open({str(marker)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "    time.sleep(60)\n",
    )
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    runner = subprocess.Popen(
        [
            sys.executable, str(_SCRIPT_PATH), "run",
            "--root", str(tmp_path), "--paths", "tests/unit/", "--chunks", "1",
            "--history-dir", "telemetry", "--log-prefix", "lane",
            "--timestamp", "20261028_130000", "--scratch-root", str(scratch_root),
            "--tmp-root", str(shm_root),
            "--guard", str(_GUARD), "--", "-p", "no:cacheprovider", "-q",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        assert _wait_until(lambda: marker.exists() and " " in marker.read_text(), 60)
        pids = [int(p) for p in marker.read_text().split()]
        runner.send_signal(signum)
        assert runner.wait(timeout=30) == 130
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()

    def _all_gone() -> bool:
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            return False
        return True

    assert _wait_until(_all_gone, 10)
    assert list(scratch_root.iterdir()) == []
    assert list(shm_root.iterdir()) == []


def test_run_cleanup_survives_a_second_signal(tmp_path: Path, shm_root) -> None:
    marker = tmp_path / "chunk.pid"
    _write(
        tmp_path / "tests/unit/test_stubborn.py",
        "import os, signal, subprocess, time\n"
        "def test_stubborn():\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    child = subprocess.Popen(['sleep', '60'])\n"
        f"    open({str(marker)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "    time.sleep(60)\n",
    )
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    runner = subprocess.Popen(
        [
            sys.executable, str(_SCRIPT_PATH), "run",
            "--root", str(tmp_path), "--paths", "tests/unit/", "--chunks", "1",
            "--history-dir", "telemetry", "--log-prefix", "lane",
            "--timestamp", "20261028_150000", "--scratch-root", str(scratch_root),
            "--tmp-root", str(shm_root),
            "--guard", str(_GUARD), "--", "-p", "no:cacheprovider", "-q",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        assert _wait_until(lambda: marker.exists() and " " in marker.read_text(), 60)
        pids = [int(p) for p in marker.read_text().split()]
        runner.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        runner.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        runner.send_signal(signal.SIGINT)
        assert runner.wait(timeout=30) == 130
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()
    assert _wait_until(lambda: _pids_gone(pids), 10)
    assert list(scratch_root.iterdir()) == []
    assert list(shm_root.iterdir()) == []


_HELPER = _PROJECT_ROOT / "scripts" / "unit_lane_common.sh"

_KILLED_RUNNER_LANE = """\
set -e
source "$HELPER"
unit_lane_start testlane "$LOCK"
echo "scratch=$UNIT_LANE_SCRATCH"
echo "tmproot=$UNIT_LANE_TMP_ROOT"
unit_lane_spawn "$ROOT/summary.log" --root "$ROOT" --paths tests/unit/ --chunks 1 \\
    --history-dir telemetry --log-prefix lane --timestamp 20261028_160000 \\
    -- -p no:cacheprovider -q
for _ in $(seq 300); do [ -s "$MARKER" ] && break; sleep 0.2; done
kill -9 "$UNIT_LANE_PID"
wait "$UNIT_LANE_PID" || true
exec 9>&-
exec 8<>"$LOCK"
python3 "$TOOL" lock --fd 8 --lane intruder --pid $$ && echo "intruder got the lock" \\
    || echo "intruder refused rc=$?"
exec 8>&-
"""


def test_killed_runner_keeps_the_lane_locked_and_its_chunks_are_stopped(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "chunk.pid"
    _write(
        tmp_path / "tests/unit/test_sleeps.py",
        "import os, subprocess, time\n"
        "def test_sleeps():\n"
        "    child = subprocess.Popen(['sleep', '60'])\n"
        f"    open({str(marker)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "    time.sleep(60)\n",
    )
    lock = tmp_path / "lanes.lock"
    env = dict(os.environ, HELPER=str(_HELPER), LOCK=str(lock), ROOT=str(tmp_path))
    env.update(MARKER=str(marker), TOOL=str(_SCRIPT_PATH))
    lane = subprocess.run(
        ["bash", "-c", _KILLED_RUNNER_LANE],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "intruder refused rc=3" in lane.stdout, lane.stdout + lane.stderr
    pids = [int(p) for p in marker.read_text().split()]
    assert _wait_until(lambda: _pids_gone(pids), 10)
    scratch = Path(lane.stdout.split("scratch=", 1)[1].split()[0])
    assert not scratch.exists()
    tmp_root = lane.stdout.split("tmproot=", 1)[1].split()[0]
    assert tmp_root.startswith("/dev/shm/cidx-lane-tmp-")
    assert not Path(tmp_root).exists()
    probe = _open_lock(lock)
    try:
        tool.acquire_lane_lock(probe, "after", 1)
    finally:
        os.close(probe)


def test_summarize_log_without_summary(tmp_path: Path) -> None:
    summary = tool.summarize_log(_write(tmp_path / "chunk.log", "Traceback ...\n"))
    assert (summary.passed, summary.failed, summary.errors) == (0, 0, 0)
    assert summary.line == "no summary"


_MOUNTS = """\
/dev/mapper/root / xfs rw,relatime 0 0
tmpfs /dev/shm tmpfs rw,nosuid,nodev 0 0
/dev/sdb1 /dev/shm/with\\040space ext4 rw 0 0
"""


def test_mount_fstype_takes_the_longest_mount_prefix() -> None:
    assert tool.mount_fstype(Path("/dev/shm/lane/chunk1"), _MOUNTS) == "tmpfs"
    assert tool.mount_fstype(Path("/dev/shm"), _MOUNTS) == "tmpfs"
    assert tool.mount_fstype(Path("/dev/shmother/x"), _MOUNTS) == "xfs"
    assert tool.mount_fstype(Path("/home/user"), _MOUNTS) == "xfs"
    assert tool.mount_fstype(Path("/dev/shm/with space/x"), _MOUNTS) == "ext4"


def test_require_tmpfs_accepts_a_dev_shm_dir(shm_root) -> None:
    tool.require_tmpfs(shm_root)


def _real_fstype(path: Path) -> str:
    return str(tool.mount_fstype(path.resolve(), Path("/proc/self/mounts").read_text()))


def test_require_tmpfs_refuses_a_disk_dir() -> None:
    disk_dir = _PROJECT_ROOT / "scripts"
    assert _real_fstype(disk_dir) != "tmpfs", "precondition: the checkout is on disk"
    with pytest.raises(tool.TmpRootRefused, match="not on tmpfs"):
        tool.require_tmpfs(disk_dir)


def test_require_tmpfs_refuses_a_missing_or_unwritable_dir(shm_root) -> None:
    with pytest.raises(tool.TmpRootRefused, match="not a directory"):
        tool.require_tmpfs(shm_root / "missing")
    locked = shm_root / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(tool.TmpRootRefused, match="not writable"):
            tool.require_tmpfs(locked)
    finally:
        locked.chmod(0o700)


_LEAVES_AN_UNREMOVABLE_DIR = """\
import os, sys
stuck = os.path.join(os.environ["TMPDIR"], "stuck")
os.makedirs(os.path.join(stuck, "inner"))
os.chmod(stuck, 0o500)
"""


def test_run_chunks_fails_naming_a_chunk_tmpdir_it_could_not_remove(
    tmp_path: Path, shm_root
) -> None:
    script = _write(tmp_path / "stuck.py", _LEAVES_AN_UNREMOVABLE_DIR)
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    with pytest.raises(tool.TmpCleanupFailed) as failed:
        tool.run_chunks(
            [tool.Chunk(files=["t/test_a.py"])],
            command=[sys.executable, str(script)],
            common_args=[],
            log_paths=[tmp_path / "chunk1.log"],
            scratch_root=scratch_root,
            tmp_root=shm_root,
            guard=_GUARD,
            extra_env={},
        )
    [leftover] = list(shm_root.iterdir())
    assert failed.value.paths == [leftover]
    assert str(leftover) in str(failed.value)
    assert [r.exit_code for r in failed.value.results] == [0]


def test_main_run_exits_non_zero_naming_a_leftover_tmpdir(
    tmp_path: Path, capsys, shm_root
) -> None:
    _write(
        tmp_path / "tests/unit/test_stuck.py",
        "import os\n"
        "def test_stuck():\n"
        "    stuck = os.path.join(os.environ['TMPDIR'], 'stuck')\n"
        "    os.makedirs(os.path.join(stuck, 'inner'))\n"
        "    os.chmod(stuck, 0o500)\n",
    )
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    rc = tool.main(
        [
            "run", "--root", str(tmp_path), "--paths", "tests/unit/",
            "--chunks", "1", "--history-dir", "telemetry",
            "--log-prefix", "lane", "--timestamp", "20261029_090000",
            "--scratch-root", str(scratch_root), "--tmp-root", str(shm_root),
            "--guard", str(_GUARD), "--", "-p", "no:cacheprovider", "-q",
        ]
    )  # fmt: skip
    captured = capsys.readouterr()
    [leftover] = list(shm_root.iterdir())
    assert rc == 1
    assert "chunk 1: PASS (exit 0)" in captured.out
    assert "FAILED" in captured.err and str(leftover) in captured.err


def test_interrupted_run_reports_a_leftover_tmpdir_and_keeps_exit_130(
    tmp_path: Path, shm_root
) -> None:
    marker = tmp_path / "stuck.ready"
    _write(
        tmp_path / "tests/unit/test_stuck_then_sleep.py",
        "import os, time\n"
        "def test_stuck_then_sleep():\n"
        "    stuck = os.path.join(os.environ['TMPDIR'], 'stuck')\n"
        "    os.makedirs(os.path.join(stuck, 'inner'))\n"
        "    os.chmod(stuck, 0o500)\n"
        f"    open({str(marker)!r}, 'w').write('ready')\n"
        "    time.sleep(60)\n",
    )
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    runner = subprocess.Popen(
        [
            sys.executable, str(_SCRIPT_PATH), "run",
            "--root", str(tmp_path), "--paths", "tests/unit/", "--chunks", "1",
            "--history-dir", "telemetry", "--log-prefix", "lane",
            "--timestamp", "20261029_100000", "--scratch-root", str(scratch_root),
            "--tmp-root", str(shm_root),
            "--guard", str(_GUARD), "--", "-p", "no:cacheprovider", "-q",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )  # fmt: skip
    try:
        assert _wait_until(marker.exists, 60)
        runner.send_signal(signal.SIGTERM)
        _, err = runner.communicate(timeout=30)
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()
    [leftover] = list(shm_root.iterdir())
    assert runner.returncode == 130
    assert "FAILED" in err and str(leftover) in err


def _tmp_root_with_owner(parent: Path, name: str, owner: str) -> Path:
    root = parent / name
    root.mkdir()
    (root / "owner").write_text(owner)
    (root / "data").write_text("x" * 1024)
    return root


def _dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def _identity(pid: int) -> str:
    return f"{pid} {tool.process_start_ticks(pid)}\n"


def test_sweep_removes_roots_of_dead_or_reused_owners_and_keeps_live_ones(
    shm_root,
) -> None:
    dead = _tmp_root_with_owner(
        shm_root, "cidx-lane-tmp-dead", f"{_dead_pid()} 12345\n"
    )
    reused = _tmp_root_with_owner(
        shm_root, "cidx-lane-tmp-reused", f"{os.getpid()} 1\n"
    )
    live = _tmp_root_with_owner(shm_root, "cidx-lane-tmp-live", _identity(os.getpid()))
    dead_claim = _tmp_root_with_owner(
        shm_root, ".cidx-lane-tmp-claim-dead", f"{_dead_pid()} 12345\n"
    )
    other = _tmp_root_with_owner(shm_root, "cidx-lane-other", f"{_dead_pid()} 1\n")
    target = _tmp_root_with_owner(shm_root, "elsewhere", f"{_dead_pid()} 1\n")
    link = shm_root / "cidx-lane-tmp-link"
    link.symlink_to(target)

    removed = tool.sweep_stale_tmp_roots(shm_root)

    assert sorted(removed) == sorted([dead, reused, dead_claim])
    assert not dead.exists() and not reused.exists() and not dead_claim.exists()
    assert live.exists() and other.exists() and link.is_symlink() and target.exists()


def test_sweep_waits_out_the_claim_grace_for_an_ownerless_root(shm_root) -> None:
    young = shm_root / ".cidx-lane-tmp-claim-young"
    young.mkdir()
    old = shm_root / "cidx-lane-tmp-legacy"
    old.mkdir()
    aged = time.time() - tool.OWNERLESS_GRACE_SECONDS - 5
    os.utime(old, (aged, aged))

    assert tool.sweep_stale_tmp_roots(shm_root) == [old]
    assert young.exists() and not old.exists()


def test_sweep_fails_naming_a_stale_root_it_could_not_remove(shm_root) -> None:
    dead = _tmp_root_with_owner(
        shm_root, "cidx-lane-tmp-stuck", f"{_dead_pid()} 12345\n"
    )
    (dead / "inner").mkdir()
    (dead / "inner" / "f").write_text("x")
    os.chmod(dead / "inner", 0o500)
    with pytest.raises(tool.TmpCleanupFailed, match=str(dead)):
        tool.sweep_stale_tmp_roots(shm_root)


def test_create_tmp_root_records_its_live_owner(shm_root) -> None:
    root = tool.create_tmp_root(shm_root, os.getpid())
    assert root.parent == shm_root and root.name.startswith("cidx-lane-tmp-")
    assert (root / "owner").read_text() == _identity(os.getpid())
    assert list(shm_root.iterdir()) == [root]
    assert tool.sweep_stale_tmp_roots(shm_root) == []


_LEADER_LEAVES_A_WRITER = """\
import os, subprocess, sys, time
out = sys.argv[1]
if out == "--linger":
    time.sleep(1.5)
    sys.exit(0)
writer = (
    "import os, sys, time\\n"
    "beat = os.path.join(os.environ['TMPDIR'], 'beat')\\n"
    "deadline = time.monotonic() + 30\\n"
    "while time.monotonic() < deadline:\\n"
    "    try:\\n"
    "        with open(beat, 'w') as handle:\\n"
    "            handle.write('alive')\\n"
    "    except OSError:\\n"
    "        with open(sys.argv[1], 'w') as handle:\\n"
    "            handle.write('lost its TMPDIR')\\n"
    "        sys.exit(1)\\n"
    "    time.sleep(0.02)\\n"
)
child = subprocess.Popen([sys.executable, "-c", writer, os.path.join(out, "lost")])
with open(os.path.join(out, "child.pid"), "w") as handle:
    handle.write(str(child.pid))
"""


def test_run_chunks_stops_a_chunk_group_before_removing_its_tmpdir(
    tmp_path: Path, shm_root, capsys
) -> None:
    script = _write(tmp_path / "leader.py", _LEADER_LEAVES_A_WRITER)
    out = tmp_path / "out"
    out.mkdir()
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    results = tool.run_chunks(
        # ^ invariant run_chunks appends chunk.files to command => out = sys.argv[1]
        [tool.Chunk(files=[str(out)]), tool.Chunk(files=["--linger"])],
        command=[sys.executable, str(script)],
        common_args=[],
        log_paths=[tmp_path / "chunk1.log", tmp_path / "chunk2.log"],
        scratch_root=scratch_root,
        tmp_root=shm_root,
        guard=_GUARD,
        extra_env={},
    )
    child = int((out / "child.pid").read_text())
    err = capsys.readouterr().err
    assert [r.exit_code for r in results] == [0, 0]
    assert not (out / "lost").exists(), "TMPDIR removed under a live child"
    assert _pids_gone([child])
    assert "chunk 1" in err and str(child) in err
    assert list(shm_root.iterdir()) == []


def _fake_proc_with_stat(base: Path, pid: int, readable: bool) -> Path:
    proc_root = base / "proc"
    (proc_root / str(pid)).mkdir(parents=True)
    stat_file = proc_root / str(pid) / "stat"
    stat_file.write_text(f"{pid} (python3) S 1 {pid} {pid} 0 -1 " + "0 " * 20)
    if not readable:
        stat_file.chmod(0)
    return proc_root


def test_sweep_keeps_a_root_whose_owner_cannot_be_read(
    tmp_path: Path, shm_root, capsys
) -> None:
    owner_pid = 424242
    proc_root = _fake_proc_with_stat(tmp_path, owner_pid, readable=False)
    root = _tmp_root_with_owner(shm_root, "cidx-lane-tmp-unknown", f"{owner_pid} 7\n")

    assert tool.sweep_stale_tmp_roots(shm_root, proc_root=proc_root) == []

    assert root.exists()
    err = capsys.readouterr().err
    assert str(root) in err and "kept" in err


def test_sweep_treats_a_missing_proc_entry_as_a_dead_owner(
    tmp_path: Path, shm_root
) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    root = _tmp_root_with_owner(shm_root, "cidx-lane-tmp-gone", "424243 7\n")

    assert tool.sweep_stale_tmp_roots(shm_root, proc_root=proc_root) == [root]
    assert not root.exists()


_LANE_START_ONLY = """\
set -e
source "$HELPER"
unit_lane_start testlane "$LOCK"
echo "tmproot=$UNIT_LANE_TMP_ROOT"
echo "owner=$(cat "$UNIT_LANE_TMP_ROOT/owner")"
echo "shell=$$"
if [[ -n "${STUCK:-}" ]]; then
    mkdir -p "$UNIT_LANE_TMP_ROOT/stuck/inner"
    chmod 500 "$UNIT_LANE_TMP_ROOT/stuck"
fi
"""


def _run_lane_start(tmp_path: Path, **extra: str) -> "subprocess.CompletedProcess[str]":
    env = dict(os.environ, HELPER=str(_HELPER), LOCK=str(tmp_path / "lanes.lock"))
    env.update(extra)
    return subprocess.run(
        ["bash", "-c", _LANE_START_ONLY],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _field(output: str, key: str) -> str:
    return output.split(f"{key}=", 1)[1].splitlines()[0]


def test_lane_start_sweeps_stale_tmpfs_roots_and_keeps_live_ones(
    tmp_path: Path,
) -> None:
    shm = Path("/dev/shm")
    stale = Path(tempfile.mkdtemp(prefix="cidx-lane-tmp-teststale-", dir=shm))
    (stale / "owner").write_text(f"{_dead_pid()} 12345\n")
    live = Path(tempfile.mkdtemp(prefix="cidx-lane-tmp-testlive-", dir=shm))
    (live / "owner").write_text(_identity(os.getpid()))
    root = None
    try:
        lane = _run_lane_start(tmp_path)
        assert lane.returncode == 0, lane.stdout + lane.stderr
        assert not stale.exists()
        assert live.exists()
        root = Path(_field(lane.stdout, "tmproot"))
        assert root.parent == shm and root.name.startswith("cidx-lane-tmp-")
        shell = int(_field(lane.stdout, "shell"))
        assert _field(lane.stdout, "owner").split()[0] == str(shell)
        # ^ invariant the lane shell has exited here => its EXIT trap removed its own root
        assert not root.exists()
    finally:
        for leftover in (stale, live, root):
            if leftover is not None:
                shutil.rmtree(leftover, ignore_errors=True)


def test_lane_exit_fails_naming_a_tmpfs_root_it_could_not_remove(
    tmp_path: Path,
) -> None:
    lane = _run_lane_start(tmp_path, STUCK="1")
    root = Path(_field(lane.stdout, "tmproot"))
    try:
        assert lane.returncode != 0
        assert str(root) in lane.stderr and "FAILED" in lane.stderr
        assert root.exists()
    finally:
        _unlock_tree(root)
        shutil.rmtree(root, ignore_errors=True)


def test_main_run_refuses_a_tmp_root_not_on_tmpfs(tmp_path: Path, capsys) -> None:
    _cli_tree(tmp_path)
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    rc = tool.main(
        [
            "run", *_selection_args(tmp_path),
            "--log-prefix", "lane", "--timestamp", "20261028_150000",
            "--scratch-root", str(scratch_root),
            "--tmp-root", str(_PROJECT_ROOT / "scripts"),
            "--guard", str(_GUARD), "--", "-q",
        ]
    )  # fmt: skip
    err = capsys.readouterr().err
    assert rc == 2
    assert "REFUSED" in err and "not on tmpfs" in err
    assert not (tmp_path / "telemetry").exists()
    assert list(scratch_root.iterdir()) == []
