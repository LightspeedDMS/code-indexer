"""Story #2121: resume-from-list is retired. An interrupted run (metadata
status ``in_progress`` or ``failed``, not ``--clear``) is always recovered by
the store-derived reconcile; no stored file list is ever replayed.

Every test drives the REAL ``cidx`` CLI (subprocess, the tree under test) in a
temporary repository against the local fake VoyageAI server (plain HTTP on
127.0.0.1, sentinel key): no real provider call is possible.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Iterator, List

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SENTINEL_KEY = "issue-2121-fake-key"
_FILE_PROGRESS = re.compile(r"^(\d+)/(\d+) files\b")
#: Files indexed before the interruption.
_BASE_FILE_COUNT = 6
#: Files synced while the interrupted run was pending (missing from the store).
_SYNCED_FILE_COUNT = 3
#: The staging shape: a stored work list of ~198k entries.
_STORED_LIST_ENTRIES = 198_000

pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]


@pytest.fixture
def fake_provider() -> Iterator[object]:
    # The shared helper stops the fake and asserts no ledger violation.
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


def _indexed_repo(tmp_path: Path, server) -> FakeProviderRepo:
    """A repository whose ``_BASE_FILE_COUNT`` files are indexed."""
    repo = FakeProviderRepo(tmp_path, server, _SENTINEL_KEY)
    repo.write_files("base", _BASE_FILE_COUNT)
    server.ledger.begin_run("initial")
    proc = repo.cidx(*repo.index_args())
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert server.ledger.run_stats("initial")["inputs"] > 0
    return repo


def _write_interrupted_state_with_stored_list(
    repo: FakeProviderRepo, status: str
) -> None:
    """An interrupted run's metadata whose stored work list holds every
    already-indexed file plus ~198k entries for files no longer on disk."""
    indexed = sorted(str(p) for p in (repo.repo / "src").glob("base_*.py"))
    assert len(indexed) == _BASE_FILE_COUNT
    stale = [
        str(repo.repo / "src" / f"gone_{i:06d}.py")
        for i in range(_STORED_LIST_ENTRIES - len(indexed))
    ]
    metadata = repo.metadata()
    metadata.update(
        {
            "status": status,
            "files_to_index": indexed + stale,
            "total_files_to_index": _STORED_LIST_ENTRIES,
            "current_file_index": 0,
            "completed_files": [],
        }
    )
    repo.metadata_path.write_text(json.dumps(metadata))


def _planned_file_totals(stdout: str) -> List[int]:
    """Distinct file-phase totals of the ``--progress-json`` lines, in order
    (the same reading as the re-embed harness's ``parse_progress_json``)."""
    totals: List[int] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        match = _FILE_PROGRESS.match(str(event.get("info", "")))
        if match and (not totals or totals[-1] != int(match.group(2))):
            totals.append(int(match.group(2)))
    return totals


@pytest.mark.parametrize("status", ["in_progress", "failed"])
@pytest.mark.parametrize(
    "extra_args",
    [pytest.param([], id="plain"), pytest.param(["--reconcile"], id="reconcile")],
)
def test_interrupted_run_with_a_198k_entry_stored_list_plans_only_files_missing_from_the_store(
    tmp_path: Path, fake_provider, status: str, extra_args: List[str]
) -> None:
    """The stored list (the indexed files + ~198k gone entries) once drove a
    resume that re-planned the indexed files and never saw the files synced
    while the run was pending. The recovery now plans exactly the files
    missing from the store."""
    repo = _indexed_repo(tmp_path, fake_provider)
    repo.write_files("synced", _SYNCED_FILE_COUNT)
    _write_interrupted_state_with_stored_list(repo, status)

    fake_provider.ledger.begin_run("recovery")
    recovery = repo.cidx(*repo.index_args(), "--progress-json", *extra_args)

    assert recovery.returncode == 0, recovery.stderr[-3000:]
    totals = _planned_file_totals(recovery.stdout)
    assert totals and totals[0] == _SYNCED_FILE_COUNT, (
        f"the recovery planned {totals}, not the files missing from the store"
    )
    stored_paths = set(repo.stored_rows().values())
    for i in range(_SYNCED_FILE_COUNT):
        assert any(p.endswith(f"synced_{i:03d}.py") for p in stored_paths)
    metadata = repo.metadata()
    assert metadata["status"] == "completed"
    assert not metadata.get("files_to_index")


def test_completed_run_records_its_listing_start_as_the_watermark(
    tmp_path: Path, fake_provider
) -> None:
    """The incremental watermark is the run's listing start, not its
    completion: a file written while the run was embedding (after it listed
    the repository) is newer than the watermark, so the next incremental
    run picks it up however long the run took."""
    repo = _indexed_repo(tmp_path, fake_provider)
    repo.write_files("changed", 2)

    fake_provider.ledger.begin_run("held", hold_seconds=2.0)
    proc = repo.spawn_index()
    try:
        assert fake_provider.ledger.wait_for_held(1, timeout=90), (
            "the run never sent an embedding request"
        )
        repo.write_files("mid_run", 1)
        mid_run_mtime = (repo.repo / "src" / "mid_run_000.py").stat().st_mtime
    finally:
        assert proc.wait(timeout=120) == 0

    metadata = repo.metadata()
    assert metadata["status"] == "completed"
    assert float(str(metadata["last_index_timestamp"])) <= mid_run_mtime, (
        "the watermark is later than a file written during the run"
    )

    fake_provider.ledger.begin_run("next")
    next_run = repo.cidx(*repo.index_args())
    assert next_run.returncode == 0, next_run.stderr[-3000:]
    assert any(p.endswith("mid_run_000.py") for p in repo.stored_rows().values())


@pytest.mark.parametrize(
    "extra_args",
    [pytest.param([], id="incremental"), pytest.param(["--reconcile"], id="reconcile")],
)
def test_progress_json_prints_an_empty_plan(
    tmp_path: Path, fake_provider, extra_args: List[str]
) -> None:
    """A run that plans nothing prints ``0/0 files`` on ``--progress-json``
    (the only channel the re-embed harness observes plans on); the
    interactive output does not change."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("base", _BASE_FILE_COUNT)
    an_hour_ago = time.time() - 3600
    for path in (repo.repo / "src").glob("*.py"):
        os.utime(path, (an_hour_ago, an_hour_ago))
    initial = repo.cidx(*repo.index_args())
    assert initial.returncode == 0, initial.stderr[-3000:]

    fake_provider.ledger.begin_run("empty-plan")
    empty = repo.cidx(*repo.index_args(), "--progress-json", *extra_args)

    assert empty.returncode == 0, empty.stderr[-3000:]
    assert fake_provider.ledger.run_stats("empty-plan")["inputs"] == 0
    assert _planned_file_totals(empty.stdout) == [0], empty.stdout[-2000:]

    interactive = repo.cidx(*repo.index_args(), *extra_args)
    assert interactive.returncode == 0, interactive.stderr[-3000:]
    assert "0/0 files" not in interactive.stdout


def _git(repo: FakeProviderRepo, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Example", "-c", "user.email=dev@example.com", *args],
        cwd=repo.repo,
        check=True,
        capture_output=True,
    )


def test_interrupted_run_is_reconciled_before_the_branch_change_block(
    tmp_path: Path, fake_provider
) -> None:
    """An interrupted run on a repository whose branch changed since it was
    recorded is recovered by the reconcile FIRST. The branch-change block
    used to win: it planned only the branch diff and marked the run
    completed, leaving the files synced while it was pending unindexed."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("base", _BASE_FILE_COUNT)
    (repo.repo / ".gitignore").write_text(".code-indexer/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    initial = repo.cidx(*repo.index_args())
    assert initial.returncode == 0, initial.stderr[-3000:]
    assert repo.metadata()["current_branch"] == "main"

    _git(repo, "checkout", "-q", "-b", "feature")
    repo.write_files("synced", _SYNCED_FILE_COUNT)
    metadata = repo.metadata()
    metadata["status"] = "in_progress"
    repo.metadata_path.write_text(json.dumps(metadata))

    recovery = repo.cidx(*repo.index_args(), "--progress-json")

    assert recovery.returncode == 0, recovery.stderr[-3000:]
    assert _planned_file_totals(recovery.stdout) == [_SYNCED_FILE_COUNT], (
        recovery.stdout[-2000:]
    )
    stored_paths = set(repo.stored_rows().values())
    for i in range(_SYNCED_FILE_COUNT):
        assert any(p.endswith(f"synced_{i:03d}.py") for p in stored_paths)
    assert repo.metadata()["status"] == "completed"


def _failing_git_diff_path(tmp_path: Path, base_path: str) -> str:
    """A PATH (test temp dir only) whose ``git`` fails every ``git diff``
    and runs the real git for everything else."""
    import shutil
    import stat

    real_git = shutil.which("git")
    assert real_git is not None
    shim_dir = tmp_path / "git-shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "diff" ]; then echo "fatal: simulated diff failure" >&2; '
        "exit 128; fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return f"{shim_dir}{os.pathsep}{base_path}"


def test_failed_git_diff_fails_the_recovery_loudly(
    tmp_path: Path, fake_provider
) -> None:
    """The recovery reconcile must never read a failed ``git diff`` as
    "nothing modified": a tracked file with working changes would then be
    compared by its HEAD blob, judged current, and the run would complete
    over a stale index. The run fails loudly and is not completed."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("base", _BASE_FILE_COUNT)  # writes repo/src/base_NNN.py
    (repo.repo / ".gitignore").write_text(".code-indexer/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    initial = repo.cidx(*repo.index_args())
    assert initial.returncode == 0, initial.stderr[-3000:]
    edited = repo.repo / "src" / "base_000.py"
    edited.write_text(edited.read_text() + "\n# uncommitted edit\n")
    metadata = repo.metadata()
    metadata["status"] = "in_progress"
    repo.metadata_path.write_text(json.dumps(metadata))

    repo.env["PATH"] = _failing_git_diff_path(tmp_path, repo.env["PATH"])
    recovery = repo.cidx(*repo.index_args())

    assert recovery.returncode != 0, recovery.stdout[-2000:]
    after = repo.metadata()
    assert after["status"] != "completed"
    assert not after.get("store_verified_by_reconcile")


def test_git_reconcile_does_not_replan_an_indexed_file_that_became_untracked(
    tmp_path: Path, fake_provider
) -> None:
    """A file indexed while tracked, then untracked (``git rm --cached``,
    content unchanged on disk) has no HEAD blob and is not in ``git diff``.
    It is stored under the blob of its content, so the reconcile derives that
    id and stops re-planning it."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("base", _BASE_FILE_COUNT)  # writes repo/src/base_NNN.py
    (repo.repo / ".gitignore").write_text(".code-indexer/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    initial = repo.cidx(*repo.index_args())
    assert initial.returncode == 0, initial.stderr[-3000:]
    _git(repo, "rm", "--cached", "-q", "src/base_000.py")
    _git(repo, "commit", "-q", "-m", "untrack base_000")

    plans = []
    for attempt in range(2):
        fake_provider.ledger.begin_run(f"reconcile-{attempt}")
        reconcile = repo.cidx(*repo.index_args(), "--progress-json", "--reconcile")
        assert reconcile.returncode == 0, reconcile.stderr[-3000:]
        plans.append(_planned_file_totals(reconcile.stdout))
        assert fake_provider.ledger.run_stats(f"reconcile-{attempt}")["inputs"] == 0
    assert plans[1] == [0], plans


def test_unresolvable_head_fails_the_recovery_loudly(
    tmp_path: Path, fake_provider
) -> None:
    """A HEAD whose branch ref points to a missing object is not an unborn
    branch: the recovery must not read "nothing modified" and complete."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("base", _BASE_FILE_COUNT)  # writes repo/src/base_NNN.py
    (repo.repo / ".gitignore").write_text(".code-indexer/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    initial = repo.cidx(*repo.index_args())
    assert initial.returncode == 0, initial.stderr[-3000:]
    edited = repo.repo / "src" / "base_000.py"
    edited.write_text(edited.read_text() + "\n# uncommitted edit\n")
    metadata = repo.metadata()
    metadata["status"] = "in_progress"
    repo.metadata_path.write_text(json.dumps(metadata))
    (repo.repo / ".git" / "refs" / "heads" / "main").write_text("12" * 20 + "\n")

    recovery = repo.cidx(*repo.index_args())

    assert recovery.returncode != 0, recovery.stdout[-2000:]
    after = repo.metadata()
    assert after["status"] != "completed"
    assert not after.get("store_verified_by_reconcile")


def test_unborn_repository_still_indexes_and_reconciles(
    tmp_path: Path, fake_provider
) -> None:
    """A git repository without any commit (provably unborn branch) indexes
    and reconciles normally."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("base", _BASE_FILE_COUNT)  # writes repo/src/base_NNN.py
    (repo.repo / ".gitignore").write_text(".code-indexer/\n")
    _git(repo, "init", "-q", "-b", "main")

    for extra in ([], ["--reconcile"]):
        run = repo.cidx(*repo.index_args(), *extra)
        assert run.returncode == 0, run.stderr[-3000:]
    stored = set(repo.stored_rows().values())
    for i in range(_BASE_FILE_COUNT):
        assert any(p.endswith(f"base_{i:03d}.py") for p in stored)
    assert repo.metadata()["status"] == "completed"


def test_git_reconcile_does_not_replan_an_indexed_uncommitted_edit(
    tmp_path: Path, fake_provider
) -> None:
    """The reconcile (now the recovery of every interrupted run) converges
    in a git repository: an uncommitted edit that is already indexed (stored
    under the blob of its working content) is not planned again."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("base", _BASE_FILE_COUNT)  # writes repo/src/base_NNN.py
    (repo.repo / ".gitignore").write_text(".code-indexer/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    initial = repo.cidx(*repo.index_args())
    assert initial.returncode == 0, initial.stderr[-3000:]
    edited = repo.repo / "src" / "base_000.py"
    edited.write_text(edited.read_text() + "\n# uncommitted edit\n")
    indexed_edit = repo.cidx(*repo.index_args())
    assert indexed_edit.returncode == 0, indexed_edit.stderr[-3000:]

    for attempt in range(2):
        fake_provider.ledger.begin_run(f"reconcile-{attempt}")
        reconcile = repo.cidx(*repo.index_args(), "--progress-json", "--reconcile")
        assert reconcile.returncode == 0, reconcile.stderr[-3000:]
        assert _planned_file_totals(reconcile.stdout) == [0], reconcile.stdout[-2000:]
        assert fake_provider.ledger.run_stats(f"reconcile-{attempt}")["inputs"] == 0
