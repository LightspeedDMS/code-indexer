"""Bug #2012 review: cancelling a refresh must stop EVERY subprocess it starts,
not only the three indexing steps -- git fetch/pull/branch/rev-parse/clone,
the local-repo `cidx init` repair, the snapshot `cidx fix-config`, and the
cidx-meta backup git calls.

A fake `git` (and `cidx`) is put first on PATH. It is a REAL process: for
the one subcommand under test it records its pid and a grandchild's pid and
then hangs; every other git subcommand is handed to the real git, so the
refresh runs its real flow against a real local origin repository.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Tuple, cast

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.utils.cancellable_subprocess import SubprocessCancelledError
from code_indexer.services.progress_subprocess_runner import IndexingCancelledError
from tests.unit.global_repos.test_refresh_scheduler_cancel_2012 import (
    ALIAS,
    CANCEL_BOUND_SECONDS,
    RecordingSnapshotManager,
    _wait_until_gone,
    alias_target,
    install_fake_cidx,
    make_local_repo_scheduler,
)

CANCELLATION = (IndexingCancelledError, SubprocessCancelledError)
REAL_GIT = shutil.which("git")
HANG_SECONDS = 120

_FAKE_GIT = """\
import json, os, subprocess, sys, time
argv = sys.argv[1:]
sub, i = "", 0
while i < len(argv):  # the subcommand follows -C <path> / -c <cfg> options
    if argv[i] in ("-C", "-c"):
        i += 2
    elif argv[i].startswith("-"):
        i += 1
    else:
        sub = argv[i]
        break
if sub == os.environ.get("FAKE_GIT_FAIL_ON"):
    sys.stderr.write(os.environ["FAKE_GIT_FAIL_STDERR"])
    sys.exit(128)
if sub == os.environ.get("FAKE_GIT_HANG_ON"):
    g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep({hang})"])
    with open(os.environ["FAKE_GIT_PID_FILE"], "w") as fh:
        json.dump({{"child_pid": os.getpid(), "grandchild_pid": g.pid}}, fh)
    time.sleep({hang})
os.execv(os.environ["FAKE_GIT_REAL"], [os.environ["FAKE_GIT_REAL"]] + argv)
"""


@pytest.fixture
def pid_file(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "child_pids.json"
    yield path
    if path.exists() and path.stat().st_size > 0:
        for pid in json.loads(path.read_text()).values():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _git(*args: str, cwd: Path) -> None:
    assert REAL_GIT is not None
    subprocess.run(
        [REAL_GIT, "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=60,
    )


def make_origin_and_clone(tmp_path: Path) -> Tuple[Path, Path]:
    """A real bare origin with one commit, cloned as the golden master."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    (work / "main.py").write_text("def main():\n    pass\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "first", cwd=work)
    origin = tmp_path / "origin.git"
    _git("clone", "--bare", str(work), str(origin), cwd=tmp_path)
    golden = tmp_path / "golden_repos"
    golden.mkdir()
    master = golden / "example-repo"
    _git("clone", str(origin), str(master), cwd=tmp_path)
    return origin, master


def push_new_commit(tmp_path: Path, origin: Path) -> None:
    other = tmp_path / "other"
    _git("clone", str(origin), str(other), cwd=tmp_path)
    (other / "more.py").write_text("x = 1\n")
    _git("add", "-A", cwd=other)
    _git("commit", "-m", "second", cwd=other)
    _git("push", "origin", "main", cwd=other)


def make_git_repo_scheduler(
    tmp_path: Path, origin: Path, master: Path
) -> Tuple[RefreshScheduler, RecordingSnapshotManager]:
    from code_indexer.config import ConfigManager
    from code_indexer.global_repos.global_registry import GlobalRegistry

    golden = master.parent
    registry = GlobalRegistry(str(golden))
    registry.register_global_repo("example-repo", ALIAS, str(origin), str(master))
    (golden / "aliases").mkdir(exist_ok=True)
    (golden / "aliases" / f"{ALIAS}.json").write_text(
        json.dumps({"target_path": str(master)})
    )
    query_tracker = QueryTracker()
    recorder = RecordingSnapshotManager()
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=ConfigManager(tmp_path / ".code-indexer" / "config.json"),
        query_tracker=query_tracker,
        cleanup_manager=CleanupManager(query_tracker),
        registry=registry,
        snapshot_manager=cast(Any, recorder),
    )
    return scheduler, recorder


def install_fake_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pid_file: Path,
    hang_on: str = "",
    fail_on: str = "",
    fail_stderr: str = "",
) -> None:
    bin_dir = tmp_path / "gitbin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "fake_git.py"
    script.write_text(_FAKE_GIT.format(hang=HANG_SECONDS))
    shim = bin_dir / "git"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GIT_REAL", str(REAL_GIT))
    monkeypatch.setenv("FAKE_GIT_PID_FILE", str(pid_file))
    monkeypatch.setenv("FAKE_GIT_HANG_ON", hang_on)
    monkeypatch.setenv("FAKE_GIT_FAIL_ON", fail_on)
    monkeypatch.setenv("FAKE_GIT_FAIL_STDERR", fail_stderr)


def child_started(pid_file: Path) -> Callable[[], bool]:
    return lambda: pid_file.exists() and pid_file.stat().st_size > 0


def expect_cancelled(action: Callable[[], Any], pid_file: Path) -> None:
    """`action` must raise a cancellation within the bound, with the hung
    child and its whole process group gone."""
    start = time.monotonic()
    with pytest.raises(CANCELLATION):
        action()
    elapsed = time.monotonic() - start
    assert pid_file.exists(), "the hung subprocess was never started"
    pids = json.loads(pid_file.read_text())
    assert elapsed < CANCEL_BOUND_SECONDS + 5, f"cancel took {elapsed:.1f}s"
    assert _wait_until_gone(pids["child_pid"], 5.0), "subprocess survived cancel"
    assert _wait_until_gone(pids["grandchild_pid"], 5.0), "its group survived"


@pytest.mark.parametrize(
    "hang_on,force_reset,new_remote_commit",
    [
        ("branch", False, False),  # branch verification
        ("fetch", False, False),  # change detection
        ("pull", False, True),  # update
        ("rev-parse", True, False),  # force-reset branch detection
    ],
)
def test_refresh_git_subprocess_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pid_file: Path,
    hang_on: str,
    force_reset: bool,
    new_remote_commit: bool,
) -> None:
    origin, master = make_origin_and_clone(tmp_path)
    if new_remote_commit:
        push_new_commit(tmp_path, origin)
    scheduler, recorder = make_git_repo_scheduler(tmp_path, origin, master)
    install_fake_git(tmp_path, monkeypatch, pid_file, hang_on=hang_on)

    expect_cancelled(
        lambda: scheduler._execute_refresh(
            ALIAS, force_reset=force_reset, cancel_check=child_started(pid_file)
        ),
        pid_file,
    )
    assert recorder.calls == []


def test_reclone_after_corrupt_fetch_is_cancelled_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path
) -> None:
    origin, master = make_origin_and_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    install_fake_git(
        tmp_path,
        monkeypatch,
        pid_file,
        hang_on="clone",
        fail_on="fetch",
        fail_stderr="error: packfile is corrupt\n",
    )

    expect_cancelled(
        lambda: scheduler._execute_refresh(ALIAS, cancel_check=child_started(pid_file)),
        pid_file,
    )
    assert (master / "main.py").exists(), "a cancelled re-clone must keep master"
    leftovers = [p.name for p in master.parent.iterdir() if "reclone" in p.name]
    assert leftovers == [], f"partial re-clone left behind: {leftovers}"


def test_stale_metadata_git_rev_parse_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path
) -> None:
    origin, master = make_origin_and_clone(tmp_path)
    meta = master / ".code-indexer" / "index" / "voyage-code-3"
    meta.mkdir(parents=True)
    (master / ".code-indexer" / "metadata.json").write_text(
        json.dumps({"status": "completed", "current_commit": "abc1234"})
    )
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    install_fake_git(tmp_path, monkeypatch, pid_file, hang_on="rev-parse")

    expect_cancelled(
        lambda: scheduler._stale_index_signal(
            str(master), ALIAS, cancel_check=child_started(pid_file)
        ),
        pid_file,
    )


def test_local_repo_repair_cidx_init_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path
) -> None:
    install_fake_cidx(tmp_path, monkeypatch, "init", pid_file)
    scheduler, recorder = make_local_repo_scheduler(tmp_path)
    (
        tmp_path / "golden_repos" / "example-repo" / ".code-indexer" / "config.json"
    ).unlink()

    expect_cancelled(
        lambda: scheduler._execute_refresh(ALIAS, cancel_check=child_started(pid_file)),
        pid_file,
    )
    assert recorder.calls == []


def test_snapshot_fix_config_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path
) -> None:
    from tests.unit.global_repos.test_refresh_scheduler_cancel_2012 import (
        CancellingSnapshotManager,
    )

    install_fake_cidx(tmp_path, monkeypatch, "fix-config", pid_file)
    scheduler, _ = make_local_repo_scheduler(tmp_path)
    snapshots = CancellingSnapshotManager(tmp_path / "golden_repos", lambda: None)
    scheduler._snapshot_manager = cast(Any, snapshots)
    target_before = alias_target(tmp_path)

    expect_cancelled(
        lambda: scheduler._execute_refresh(ALIAS, cancel_check=child_started(pid_file)),
        pid_file,
    )
    assert alias_target(tmp_path) == target_before


def test_already_cancelled_refresh_starts_no_metadata_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path
) -> None:
    """The repo-metrics git calls (ls-files/rev-list) at the start of
    indexing must not even start for a job that is already cancelled."""
    origin, master = make_origin_and_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    install_fake_git(tmp_path, monkeypatch, pid_file, hang_on="ls-files")

    with pytest.raises(CANCELLATION):
        scheduler._index_source(
            alias_name=ALIAS, source_path=str(master), cancel_check=lambda: True
        )
    assert not pid_file.exists(), "git ls-files ran for a cancelled job"


def test_metrics_git_call_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path
) -> None:
    """A cancel landing while the repo-metrics `git ls-files` runs must stop
    that git process group and propagate (not be swallowed as a metrics
    failure that silently continues into indexing)."""
    origin, master = make_origin_and_clone(tmp_path)
    scheduler, _ = make_git_repo_scheduler(tmp_path, origin, master)
    install_fake_git(tmp_path, monkeypatch, pid_file, hang_on="ls-files")

    expect_cancelled(
        lambda: scheduler._index_source(
            alias_name=ALIAS,
            source_path=str(master),
            cancel_check=child_started(pid_file),
        ),
        pid_file,
    )


def test_cidx_meta_backup_git_calls_are_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pid_file: Path
) -> None:
    from code_indexer.server.services.cidx_meta_backup.branch_detect import (
        detect_default_branch,
    )
    from code_indexer.server.services.cidx_meta_backup.sync import (
        CidxMetaBackupSync,
    )

    _, master = make_origin_and_clone(tmp_path)
    install_fake_git(tmp_path, monkeypatch, pid_file, hang_on="remote")
    expect_cancelled(
        lambda: detect_default_branch(
            str(master), cancel_check=child_started(pid_file)
        ),
        pid_file,
    )

    pid_file.unlink()
    install_fake_git(tmp_path, monkeypatch, pid_file, hang_on="status")
    expect_cancelled(
        lambda: CidxMetaBackupSync(
            str(master), "main", cancel_check=child_started(pid_file)
        ).sync(),
        pid_file,
    )
