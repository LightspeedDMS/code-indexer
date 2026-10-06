"""Tests for the shared per-process budget helper.

A node budget applied per uvicorn worker process is a budget multiplied by the
worker count. On tools-* that is x2 per pod and x12 fleet-wide, which is how a
"5 concurrent jobs" setting becomes 60 threads against one NFS mount.

The division itself is three characters of arithmetic; everything interesting is
in the edges -- an unknown worker count, a budget smaller than the worker count,
and a detector that must never raise at import time.
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path

import pytest

from code_indexer.utils.worker_budget import (
    coerce_worker_count,
    detect_worker_count,
    per_process_budget,
    read_argv_workers,
    read_config_json_workers,
)


# ──────────────────────────── per_process_budget ─────────────────────────────


@pytest.mark.parametrize(
    "budget,workers,expected",
    [
        (4, 2, 2),
        (4, 4, 1),
        (8, 2, 4),
        (4, 1, 4),
        (5, 2, 2),
    ],
)
def test_divides_the_node_budget_across_workers(budget, workers, expected) -> None:
    assert per_process_budget(budget, workers) == expected


@pytest.mark.parametrize("workers", [8, 16, 100])
def test_floors_at_one_so_a_pool_is_never_empty(workers: int) -> None:
    """A bound of 0 is not a smaller bound, it is a deadlock.

    A zero-size worker pool accepts jobs and runs none of them; a zero refresh
    budget means no repo is ever refreshed. Both look like a hang, not a limit.
    """
    assert per_process_budget(4, workers) == 1


@pytest.mark.parametrize("workers", [0, -1, -100])
def test_a_nonsensical_worker_count_divides_by_nothing(workers: int) -> None:
    """Unknown or absurd worker counts must not shrink the budget.

    The detector answers 1 when it cannot tell, but a caller passing a stored
    value could still hand us 0. Dividing by it would raise; treating it as
    "many" would silently gut the pool. Treat it as one.
    """
    assert per_process_budget(4, workers) == 4


def test_a_budget_below_the_worker_count_still_yields_one() -> None:
    assert per_process_budget(1, 12) == 1
    assert per_process_budget(0, 2) == 1


# ──────────────────────────── coerce_worker_count ────────────────────────────


def test_bool_is_rejected_even_though_it_is_an_int() -> None:
    """`True` would otherwise resolve to "1 worker" and divide nothing."""
    assert coerce_worker_count(True) is None
    assert coerce_worker_count(False) is None


@pytest.mark.parametrize("value", ["2", 2.0, None, [], {}])
def test_non_int_values_are_not_worker_counts(value) -> None:
    assert coerce_worker_count(value) is None


@pytest.mark.parametrize("value,expected", [(1, 1), (2, 2), (64, 64)])
def test_positive_ints_pass_through(value: int, expected: int) -> None:
    assert coerce_worker_count(value) == expected


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_ints_are_rejected(value: int) -> None:
    assert coerce_worker_count(value) is None


# ──────────────────────────── detect_worker_count ────────────────────────────


def test_takes_the_larger_when_sources_disagree() -> None:
    """The larger count yields the smaller per-process bound.

    Guessing high costs a queued job; guessing low costs the oversubscription
    this helper exists to remove. Only one of those is recoverable.
    """
    assert detect_worker_count(sources=(lambda: 2, lambda: 4)) == 4
    assert detect_worker_count(sources=(lambda: 4, lambda: 2)) == 4


def test_defaults_to_one_when_every_source_is_silent() -> None:
    """Unknown means "do not divide" -- i.e. exactly today's behaviour."""
    assert detect_worker_count(sources=(lambda: None, lambda: None)) == 1


def test_survives_a_raising_source() -> None:
    """This runs at import in at least one caller; it must never raise."""

    def _boom():
        raise RuntimeError("no systemd here")

    assert detect_worker_count(sources=(_boom, lambda: 3)) == 3
    assert detect_worker_count(sources=(_boom, _boom)) == 1


def test_a_source_returning_garbage_is_ignored_not_trusted() -> None:
    assert detect_worker_count(sources=(lambda: "4", lambda: 2)) == 2
    assert detect_worker_count(sources=(lambda: True, lambda: None)) == 1


# ─────────────────────────── read_config_json_workers ────────────────────────


def test_reads_workers_from_the_server_data_dir(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"workers": 2}))
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    assert read_config_json_workers() == 2


def test_extra_dir_envs_are_searched_too(tmp_path: Path, monkeypatch) -> None:
    """xray passes its own cache-dir env; the shared helper must not hardcode it."""
    monkeypatch.delenv("CIDX_SERVER_DATA_DIR", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"workers": 3}))
    monkeypatch.setenv("SOME_OTHER_DIR", str(tmp_path))
    assert read_config_json_workers(extra_dir_envs=("SOME_OTHER_DIR",)) == 3


@pytest.mark.parametrize("body", ["{not json", json.dumps({"workers": 0}), "{}"])
def test_malformed_or_useless_config_is_unknown_not_fatal(
    tmp_path: Path, monkeypatch, body: str
) -> None:
    (tmp_path / "config.json").write_text(body)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent-home"))
    assert read_config_json_workers() is None


def test_missing_file_is_unknown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent-home"))
    assert read_config_json_workers() is None


# ───────────────────────────── read_argv_workers ─────────────────────────────
#
# The rung that exists because the other two fail in a pod. Found by deploying
# the canary and asking the running process what it had detected: the answer was
# 1 while the container args said --workers 2, so every budget divided by 1 --
# in the one deployment the division was written for.
#
# Every !92 test injected detect_worker_count, so all 15 passed while the real
# detector was wrong. These drive the real thing against a synthetic /proc, and
# the last one against a real process tree.


def _fake_proc(tmp_path, entries):
    """Build a /proc-shaped tree: {pid: (cmdline_args, ppid)}."""
    root = tmp_path / "proc"
    for pid, (argv, ppid) in entries.items():
        d = root / pid
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "status").write_text(f"Name:\tpython\nPPid:\t{ppid}\n")
    return root


def test_argv_reads_workers_from_this_process(tmp_path):
    root = _fake_proc(
        tmp_path,
        {"7": (["python", "-m", "code_indexer.server.main", "--workers", "2"], "1")},
    )
    assert read_argv_workers(proc_root=root, pid="7") == 2


def test_argv_accepts_the_equals_form(tmp_path):
    root = _fake_proc(
        tmp_path,
        {"7": (["python", "-m", "code_indexer.server.main", "--workers=4"], "1")},
    )
    assert read_argv_workers(proc_root=root, pid="7") == 4


def test_argv_walks_up_to_the_uvicorn_master(tmp_path):
    """The case that matters, and the reason a self-only read is not enough.

    uvicorn spawns workers through multiprocessing, so a worker's OWN argv is
    ``python -B -c from multiprocessing.spawn import spawn_main; ...``. Observed
    live on the canary: master pid 7 carries --workers 2, workers 9 and 10 carry
    spawn_main. Reading only /proc/self/cmdline returns None for every worker --
    which is the process that actually sizes the pools.
    """
    root = _fake_proc(
        tmp_path,
        {
            "1": (
                [
                    "/usr/bin/tini",
                    "--",
                    "python",
                    "-m",
                    "code_indexer.server.main",
                    "--workers",
                    "2",
                ],
                "0",
            ),
            "7": (["python", "-m", "code_indexer.server.main", "--workers", "2"], "1"),
            "9": (
                ["python", "-B", "-c", "from multiprocessing.spawn import spawn_main"],
                "7",
            ),
        },
    )
    assert read_argv_workers(proc_root=root, pid="9") == 2


def test_argv_gives_up_rather_than_looping_on_a_cycle(tmp_path):
    root = _fake_proc(tmp_path, {"5": (["python"], "5")})
    assert read_argv_workers(proc_root=root, pid="5") is None


def test_argv_stops_at_pid_0(tmp_path):
    root = _fake_proc(tmp_path, {"1": (["init"], "0")})
    assert read_argv_workers(proc_root=root, pid="1") is None


def test_argv_respects_the_hop_limit(tmp_path):
    """--workers further up than max_hops is not found, rather than walked forever."""
    chain = {
        "4": (["python", "-m", "code_indexer.server.main", "--workers", "8"], "0"),
        "3": (["python"], "4"),
        "2": (["python"], "3"),
        "1": (["python"], "2"),
    }
    root = _fake_proc(tmp_path, chain)
    assert read_argv_workers(proc_root=root, pid="1", max_hops=1) is None
    assert read_argv_workers(proc_root=root, pid="1", max_hops=3) == 8


def test_argv_ignores_a_nonsense_value(tmp_path):
    root = _fake_proc(
        tmp_path,
        {"7": (["python", "-m", "code_indexer.server.main", "--workers", "zero"], "1")},
    )
    assert read_argv_workers(proc_root=root, pid="7") is None
    root2 = _fake_proc(
        tmp_path / "b",
        {"7": (["python", "-m", "code_indexer.server.main", "--workers", "0"], "1")},
    )
    assert read_argv_workers(proc_root=root2, pid="7") is None


@pytest.mark.parametrize(
    "argv,expected,why",
    [
        (
            ["python", "-m", "code_indexer.server.main", "--workers", "2"],
            2,
            "pod launcher",
        ),
        (
            ["uvicorn", "code_indexer.server.app:app", "--workers", "4"],
            4,
            "uvicorn form",
        ),
        (
            [
                "python",
                "/usr/lib/python3/site-packages/code_indexer/server/main.py",
                "--workers",
                "2",
            ],
            2,
            "installed script path",
        ),
        (
            ["pytest", "/home/me/code_indexer/tests", "--workers", "4"],
            None,
            "pytest run inside a directory named code_indexer",
        ),
        (
            [
                "uvicorn",
                "other.app:app",
                "--workers",
                "8",
                "--reload-dir",
                "/src/code_indexer",
            ],
            None,
            "unrelated uvicorn that merely mentions the path",
        ),
        (
            ["python", "/tmp/x/code_indexer_server_main.py", "--workers", "3"],
            None,
            "a script NAMED like the server but not it",
        ),
        (["/usr/bin/supervisord", "--workers", "8"], None, "supervisor"),
        # Pins the prefix to the SERVER package, not the whole distribution: a
        # mutation widening it to "code_indexer" survived until this case
        # existed. Another cidx module is still not the thing whose --workers
        # sizes the server's pools.
        (
            ["python", "-m", "code_indexer.cli", "--workers", "4"],
            None,
            "a different code_indexer module",
        ),
        (
            ["python", "-m", "code_indexer.server_tools", "--workers", "4"],
            None,
            "a module that merely prefixes code_indexer.server",
        ),
    ],
)
def test_only_a_real_server_launch_supplies_the_worker_count(
    tmp_path, argv, expected, why
):
    """Matched as a module or script PATH, never as a bare substring.

    A substring test on "code_indexer" accepts any command line that merely
    mentions it, including anything run from a directory with that name. The
    cases below are the ones that distinguish the two, and the second-to-last
    was previously produced by this file's own end-to-end test.
    """
    root = _fake_proc(tmp_path, {"7": (argv, "1")})
    assert read_argv_workers(proc_root=root, pid="7") == expected, why


def test_an_unrelated_ancestors_workers_flag_is_ignored(tmp_path):
    """The walk must not take the first --workers it meets.

    A cidx process can sit under a supervisor, CI runner or terminal that was
    itself launched with --workers for something else entirely. Taking that
    number would size cidx's pools from an unrelated service's concurrency --
    silently, and in the direction of starving ourselves if it is large.
    """
    root = _fake_proc(
        tmp_path,
        {
            "1": (["/usr/bin/supervisord", "--workers", "8"], "0"),
            "7": (
                ["python", "-B", "-c", "from multiprocessing.spawn import spawn_main"],
                "1",
            ),
        },
    )
    assert read_argv_workers(proc_root=root, pid="7") is None


def test_our_master_is_found_beneath_an_unrelated_workers_flag(tmp_path):
    """The discriminator must not be so strict it misses the real master.

    Decoy closer than the real one: the walk passes the supervisor's --workers 8
    and keeps going to the cidx master's --workers 2.
    """
    root = _fake_proc(
        tmp_path,
        {
            "1": (["/usr/bin/supervisord", "--workers", "8"], "0"),
            "5": (["python", "-m", "code_indexer.server.main", "--workers", "2"], "1"),
            "9": (
                ["python", "-B", "-c", "from multiprocessing.spawn import spawn_main"],
                "5",
            ),
        },
    )
    assert read_argv_workers(proc_root=root, pid="9") == 2


def test_a_uvicorn_style_launch_still_counts(tmp_path):
    """`uvicorn code_indexer.server.app:app --workers 4` names the module too."""
    root = _fake_proc(
        tmp_path,
        {"7": (["uvicorn", "code_indexer.server.app:app", "--workers", "4"], "1")},
    )
    assert read_argv_workers(proc_root=root, pid="7") == 4


def test_a_worker_that_cannot_find_its_launcher_says_so(tmp_path, caplog):
    """A console-script launcher is the live gap in this rung, so it must be loud.

    Today's entry points (`cidx`, `code-indexer`) both run the CLI, and the
    server starts as `python -m code_indexer.server.main` -- so nothing hits
    this. A future `cidx-server` script would name neither the module nor
    server/main.py and the rung would quietly stop working, every budget would
    divide by 1, and the node would be oversubscribed with no signal. That is
    exactly how the original defect survived: silently.

    Only a SPAWNED worker warns. Seeing multiprocessing.spawn in our own argv
    means uvicorn forked us, which it only does for >= 2 workers -- so the count
    genuinely matters here and we failed to read it.
    """
    root = _fake_proc(
        tmp_path,
        {
            "1": (["/usr/local/bin/cidx-server", "--workers", "2"], "0"),
            "9": (
                [
                    "python",
                    "-B",
                    "-c",
                    "from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=6)",
                ],
                "1",
            ),
        },
    )
    with caplog.at_level("WARNING"):
        assert read_argv_workers(proc_root=root, pid="9") is None
    assert any(
        "no cidx server launch was found" in r.message for r in caplog.records
    ), "a spawned worker that cannot resolve its worker count must say so"


@pytest.mark.parametrize(
    "argv,is_worker,why",
    [
        (
            [
                "python",
                "-B",
                "-c",
                "from multiprocessing.spawn import spawn_main; "
                "spawn_main(tracker_fd=6, pipe_handle=9)",
            ],
            True,
            "the program CPython actually generates for a spawn child",
        ),
        (
            ["/bin/sh", "-c", "python -c \"if 'multiprocessing.spawn' in cmd: ...\""],
            False,
            "a script whose own TEXT mentions multiprocessing.spawn",
        ),
        (
            [
                "python",
                "-c",
                "from multiprocessing.spawn import spawn_main",
            ],
            False,
            "imports spawn_main but never calls it",
        ),
        (
            ["python", "-c", "spawn_main(tracker_fd=1)"],
            False,
            "calls a local spawn_main without multiprocessing's import",
        ),
        (
            ["python", "-m", "code_indexer.server.main", "--workers", "2"],
            False,
            "the uvicorn master is not a spawn child",
        ),
    ],
)
def test_only_a_real_spawn_child_counts_as_a_worker(argv, is_worker, why):
    """Both the import AND the call, never a bare substring.

    The first version tested for "multiprocessing.spawn" alone and warned about
    a `sh -c` script whose own text contained the phrase -- observed live on the
    canary, from the verification command itself. A diagnostic that cries wolf
    is worse than none: it trains everyone to ignore the one case it exists for.
    """
    from code_indexer.utils.worker_budget import _looks_like_a_spawned_worker

    assert _looks_like_a_spawned_worker(argv) is is_worker, why


def test_an_ordinary_process_that_finds_nothing_stays_quiet(tmp_path, caplog):
    """The CLI, a test runner, a shell -- none of them are workers.

    Warning there would train everyone to ignore the message, which is the same
    as not having it.
    """
    root = _fake_proc(
        tmp_path,
        {"1": (["bash"], "0"), "5": (["python", "-c", "print(1)"], "1")},
    )
    with caplog.at_level("WARNING"):
        assert read_argv_workers(proc_root=root, pid="5") is None
    assert not [r for r in caplog.records if "no cidx server launch" in r.message]


def test_a_worker_that_DOES_find_its_launcher_stays_quiet(tmp_path, caplog):
    """The normal pod case must not warn."""
    root = _fake_proc(
        tmp_path,
        {
            "7": (["python", "-m", "code_indexer.server.main", "--workers", "2"], "1"),
            "9": (
                ["python", "-B", "-c", "from multiprocessing.spawn import spawn_main"],
                "7",
            ),
        },
    )
    with caplog.at_level("WARNING"):
        assert read_argv_workers(proc_root=root, pid="9") == 2
    assert not [r for r in caplog.records if "no cidx server launch" in r.message]


def test_argv_missing_proc_is_unknown_not_fatal(tmp_path):
    """Runs at import; a non-Linux host must get None, never an exception."""
    assert read_argv_workers(proc_root=tmp_path / "nope", pid="1") is None


def test_argv_is_consulted_by_the_real_default_detector(tmp_path, monkeypatch):
    """detect_worker_count() must actually USE the new rung.

    A correct read_argv_workers wired into nothing is the shape of the original
    bug, where the mechanism was right and the deployment still divided by 1.
    """
    import code_indexer.utils.worker_budget as wb

    monkeypatch.setattr(wb, "read_applied_worker_count", lambda: 1)
    monkeypatch.setattr(wb, "read_config_json_workers", lambda *a, **k: None)
    monkeypatch.setattr(wb, "read_argv_workers", lambda *a, **k: 2)
    assert wb.detect_worker_count() == 2


def test_end_to_end_against_a_real_process_tree(tmp_path):
    """No synthetic /proc and no injection: a real parent/child pair.

    The parent is launched with ``--workers 3`` in its real argv and starts a
    CHILD whose own argv does not contain it -- the uvicorn master/worker shape.
    The child calls read_argv_workers() against the real /proc and must walk up
    to find it.

    Uses subprocess rather than os.fork(): forking inside a pytest process can
    deadlock on inherited locks, and an earlier version of this test hung the
    suite for minutes before being killed.
    """
    import subprocess
    import sys

    if not pathlib.Path("/proc/self/cmdline").exists():
        pytest.skip("/proc unavailable on this platform")

    src = str(pathlib.Path(__file__).resolve().parents[3] / "src")
    child = tmp_path / "child.py"
    child.write_text(
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "from code_indexer.utils.worker_budget import read_argv_workers\n"
        "print(read_argv_workers())\n"
    )
    # A real launcher path: .../code_indexer/server/main.py. The earlier
    # version used a script merely NAMED code_indexer_server_main.py, which
    # the substring marker accepted -- the exact false positive this
    # discriminator now rejects, sitting in its own test.
    server_dir = tmp_path / "code_indexer" / "server"
    server_dir.mkdir(parents=True)
    parent = server_dir / "main.py"
    parent.write_text(
        "import subprocess, sys\n"
        f"out = subprocess.run([sys.executable, {str(child)!r}],\n"
        "                     capture_output=True, text=True, timeout=30)\n"
        "sys.stdout.write(out.stdout)\n"
        "sys.stderr.write(out.stderr)\n"
    )
    res = subprocess.run(
        [sys.executable, str(parent), "--workers", "3"],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert res.returncode == 0, res.stderr[-400:]
    assert res.stdout.strip() == "3", (
        f"child read {res.stdout.strip()!r} -- it must walk up to the parent, "
        f"whose argv carries --workers 3"
    )
