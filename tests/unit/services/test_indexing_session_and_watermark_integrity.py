from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List, Optional

import pytest

from code_indexer.storage.shared.hnsw_sync_state import (
    CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
)
from tests.unit.global_repos.test_local_git_source_removal_2194 import (
    _BRANCH,
    _DOOMED,
    _FAKE_KEY,
    _KEPT,
    _cli_semantic_hits,
    _git,
    _git_init,
    _indexed_git_repo,
    cluster_mode,  # noqa: F401  (pytest fixture: solo epoch and cluster fail-closed)
)
from tests.utils.cidx_fake_provider_repo import FakeProviderRepo, unique_python_text
from tests.utils.fake_voyage_provider import running_fake_voyage_server
from tests.utils.source_tree_age import age_source_files

pytestmark = pytest.mark.timeout(300)

# ^ why mark the real result cancelled, not signal the child; a cancelled run reports it only through its ProcessingStats
_CANCEL_NEXT_FILE_PROCESSING = """
import os as _os

if _os.path.exists(_FLAG):
    from code_indexer.services import high_throughput_processor as _processor

    _real_process = _processor.HighThroughputProcessor.process_files_high_throughput

    def _process_cancelled_once(self, *args, **kwargs):
        stats = _real_process(self, *args, **kwargs)
        if _os.path.exists(_FLAG):
            _os.unlink(_FLAG)
            stats.cancelled = True
        return stats

    _processor.HighThroughputProcessor.process_files_high_throughput = (
        _process_cancelled_once
    )
"""


def _injection(flag: Path, script: str) -> str:
    return f"_FLAG = {str(flag)!r}\n" + script


def _watermark(repo: FakeProviderRepo) -> Optional[str]:
    watermarks: Any = repo.metadata().get("branch_commit_watermarks") or {}
    value = watermarks.get(_BRANCH)
    return None if value is None else str(value)


def _head(repo: FakeProviderRepo) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo.repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _index(repo: FakeProviderRepo) -> "subprocess.CompletedProcess[str]":
    return repo.cidx(*repo.index_args(), "--fts")


def _remove_doomed_and_modify_kept(repo: FakeProviderRepo, doomed: Path) -> None:
    doomed.unlink()
    (repo.repo / "src" / "kept_001.py").write_text(
        unique_python_text("modified_001", 40)
    )
    _git(repo.repo, "add", "-A")
    _git(repo.repo, "commit", "-qm", "remove and modify")


def test_cancelled_incremental_keeps_the_watermark_and_the_next_run_removes_the_deletion(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    flag = tmp_path / "cancel-next-file-processing"
    injection = _injection(flag, _CANCEL_NEXT_FILE_PROCESSING)
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode, injection)
        age_source_files(repo.repo)
        base = _watermark(repo)
        _remove_doomed_and_modify_kept(repo, doomed)

        flag.touch()
        _index(repo)
        assert not flag.exists(), "the injected cancellation never fired"
        assert _watermark(repo) == base

        following = _index(repo)

        assert following.returncode == 0, following.stderr[-3000:]
        hits = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" not in hits, hits
        assert f"{_KEPT}.py" in hits, hits


def test_cancelled_full_index_records_no_watermark(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    flag = tmp_path / "cancel-next-file-processing"
    injection = _injection(flag, _CANCEL_NEXT_FILE_PROCESSING)
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo = FakeProviderRepo(tmp_path, server, _FAKE_KEY, injection)
        if cluster_mode:
            repo.env[CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV] = "1"
        repo.write_files("kept", 3)
        _git_init(repo.repo, ".code-indexer/\n")

        flag.touch()
        _index(repo)

        assert not flag.exists(), "the injected cancellation never fired"
        assert _watermark(repo) is None, _head(repo)


def test_incremental_failing_before_branch_isolation_does_not_publish_the_hidden_file(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    from tests.unit.global_repos.test_local_git_source_removal_2194 import (
        _FAIL_NEXT_BRANCH_ISOLATION,
    )

    flag = tmp_path / "fail-next-branch-isolation"
    injection = f"_FAIL_FLAG = {str(flag)!r}\n" + _FAIL_NEXT_BRANCH_ISOLATION
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode, injection)
        age_source_files(repo.repo)
        doomed.unlink()
        (repo.repo / "src" / "added_000.py").write_text(
            unique_python_text("added_000", 40)
        )
        _git(repo.repo, "add", "-A")
        _git(repo.repo, "commit", "-qm", "remove and add")

        flag.touch()
        interrupted = _index(repo)
        assert interrupted.returncode != 0, interrupted.stdout[-2000:]
        assert not flag.exists(), "the injected failure never fired"

        following = _index(repo)

        assert following.returncode == 0, following.stderr[-3000:]
        hits = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" not in hits, hits
        assert "added_000.py" in hits, hits


def _git_out(repo: FakeProviderRepo, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo.repo, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def _seed_duplicate_then_remove_doomed(
    repo: FakeProviderRepo, doomed: Path, commit: bool = True
) -> List[str]:
    from code_indexer.storage.sqlite_chunk_store import ChunkStore
    from tests.unit.global_repos.test_local_git_source_removal_2194 import (
        _published_hnsw_ids,
        _stored_ids,
    )

    kept_blob = _git_out(repo, "rev-parse", "HEAD:src/kept_001.py")
    # ^ why a deleted file's rows; a re-processed file reclaims its content-addressed ids => the seed would vanish
    seeded = _stored_ids(repo, f"src/{_DOOMED}.py")
    assert seeded
    # ^ why kept_001's own blob; a mismatching id => kept_001 re-indexed and its orphans (the seed) deleted before the cleanup
    with ChunkStore(repo.chunks_db) as store:
        for point_id in seeded:
            assert store.update_payload_fields(
                point_id,
                {
                    "path": "src/kept_001.py",
                    "git_commit_hash": "working_dir_seeded",
                    "git_blob_hash": kept_blob,
                },
            )
    assert set(seeded) <= set(_published_hnsw_ids(repo))
    doomed.unlink()
    if not commit:
        return seeded
    (repo.repo / "src" / "added_000.py").write_text(unique_python_text("added_000", 40))
    _git(repo.repo, "add", "-A")
    _git(repo.repo, "commit", "-qm", "remove and add")
    return seeded


def _assert_seeded_hidden_and_unpublished(
    repo: FakeProviderRepo, seeded: List[str]
) -> None:
    from code_indexer.storage.sqlite_chunk_store import ChunkStore
    from tests.unit.global_repos.test_local_git_source_removal_2194 import (
        _published_hnsw_ids,
    )

    with ChunkStore(repo.chunks_db, read_only=True) as store:
        hidden = {
            str(record["id"]): list(record["payload"].get("hidden_branches") or [])
            for record in store.stream_all()
            if str(record["id"]) in seeded
        }
    assert hidden == {point_id: [_BRANCH] for point_id in seeded}, hidden
    published = set(_published_hnsw_ids(repo))
    assert not published & set(seeded), published


def test_reconcile_duplicate_cleanup_is_covered_by_the_publish(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode)
        seeded = _seed_duplicate_then_remove_doomed(repo, doomed)

        reconciled = repo.cidx(*repo.index_args(), "--fts", "--reconcile")

        assert reconciled.returncode == 0, reconciled.stderr[-3000:]
        _assert_seeded_hidden_and_unpublished(repo, seeded)


# ^ why fail only the cleanup's call; branch isolation uses the same payload update
_FAIL_NEXT_CLEANUP_HIDE = """
import os as _os
import sys as _sys

if _os.path.exists(_FLAG):
    from code_indexer.storage import filesystem_vector_store as _store

    _real_update = _store.FilesystemVectorStore._batch_update_payload_only

    def _update_failing_once(self, *args, **kwargs):
        caller = _sys._getframe(1).f_code.co_name
        if caller == "_cleanup_multiple_visible_content_points" and _os.path.exists(_FLAG):
            _os.unlink(_FLAG)
            return False
        return _real_update(self, *args, **kwargs)

    _store.FilesystemVectorStore._batch_update_payload_only = _update_failing_once
"""


def test_failed_duplicate_hide_is_retried_by_the_next_reconcile(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    flag = tmp_path / "fail-next-cleanup-hide"
    injection = _injection(flag, _FAIL_NEXT_CLEANUP_HIDE)
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode, injection)
        from tests.unit.global_repos.test_local_git_source_removal_2194 import (
            _published_hnsw_ids,
        )

        base = _watermark(repo)
        seeded = _seed_duplicate_then_remove_doomed(repo, doomed)
        published_before = _published_hnsw_ids(repo)

        flag.touch()
        failed = repo.cidx(*repo.index_args(), "--fts", "--reconcile")

        assert failed.returncode != 0, failed.stdout[-2000:]
        assert not flag.exists(), "the injected hide failure never fired"
        assert _watermark(repo) == base
        assert _published_hnsw_ids(repo) == published_before
        assert set(seeded) <= set(published_before)

        healing = _index(repo)

        assert healing.returncode == 0, healing.stderr[-3000:]
        _assert_seeded_hidden_and_unpublished(repo, seeded)


# ^ why observe the real pager, not stub it; the guard is about which callers scroll, the scroll itself runs unchanged
_COUNT_PAGER_CALLS = """
import os as _os
import sys as _sys

if _os.path.exists(_FLAG):
    from code_indexer.services import smart_indexer as _module

    _real_pager = _module.SmartIndexer._scroll_all_content_points

    def _counting_pager(self, *args, **kwargs):
        with open(_FLAG, "a") as log:
            log.write(_sys._getframe(1).f_code.co_name + "\\n")
        return _real_pager(self, *args, **kwargs)

    _module.SmartIndexer._scroll_all_content_points = _counting_pager
"""


def test_quiet_reconcile_with_nothing_owed_never_scrolls_for_the_cleanup(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    log = tmp_path / "pager-calls"
    injection = _injection(log, _COUNT_PAGER_CALLS)
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, cluster_mode, injection)
        log.write_text("")

        quiet = repo.cidx(*repo.index_args(), "--fts", "--reconcile")

        assert quiet.returncode == 0, quiet.stderr[-3000:]
        callers = log.read_text().split()
        assert callers, "the snapshot read never reached the pager"
        assert "_cleanup_multiple_visible_content_points" not in callers, callers


# ^ why KeyboardInterrupt, not False; an interrupt keeps status completed => the next plain run takes the quiet incremental return
_INTERRUPT_NEXT_CLEANUP_HIDE = _FAIL_NEXT_CLEANUP_HIDE.replace(
    "return False", "raise KeyboardInterrupt('injected')"
)


def test_quiet_incremental_republish_runs_the_owed_cleanup(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    flag = tmp_path / "interrupt-next-cleanup-hide"
    injection = _injection(flag, _INTERRUPT_NEXT_CLEANUP_HIDE)
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode, injection)
        age_source_files(repo.repo)
        seeded = _seed_duplicate_then_remove_doomed(repo, doomed, commit=False)
        # ^ why delete kept_002 too; the seed leaves nothing owed => without a hide the reconcile never republishes
        (repo.repo / "src" / "kept_002.py").unlink()

        flag.touch()
        interrupted = repo.cidx(*repo.index_args(), "--reconcile")
        assert interrupted.returncode != 0, interrupted.stdout[-2000:]
        assert not flag.exists(), "the injected cleanup interrupt never fired"

        quiet = repo.cidx(*repo.index_args())

        assert quiet.returncode == 0, quiet.stderr[-3000:]
        _assert_seeded_hidden_and_unpublished(repo, seeded)


def test_quiet_republish_keeps_a_preserved_outside_root_vector(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    from tests.unit.global_repos.test_local_git_source_removal_2194 import (
        _published_hnsw_ids,
        _relocate_stored_row,
        _stored_ids,
    )
    from tests.unit.services.test_quiet_incremental_hnsw_republish_2197 import (
        _INTERRUPT_NEXT_HNSW_REBUILD,
    )

    outside = tmp_path / "outside" / "external_000.py"
    outside.parent.mkdir()
    outside.write_text("# exists outside the codebase root\n")
    flag = tmp_path / "fail-next-hnsw-rebuild"
    injection = f"_FAIL_FLAG = {str(flag)!r}\n" + _INTERRUPT_NEXT_HNSW_REBUILD
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode, injection)
        age_source_files(repo.repo)
        relocated = _relocate_stored_row(repo, "src/kept_002.py", str(outside))
        doomed_ids = _stored_ids(repo, f"src/{_DOOMED}.py")
        (repo.repo / "src" / "kept_002.py").unlink()
        doomed.unlink()

        flag.touch()
        interrupted = repo.cidx(*repo.index_args(), "--reconcile")
        assert interrupted.returncode != 0, interrupted.stdout[-2000:]
        assert not flag.exists(), "the injected interrupt never fired"

        quiet = repo.cidx(*repo.index_args())

        assert quiet.returncode == 0, quiet.stderr[-3000:]
        published = set(_published_hnsw_ids(repo))
        assert set(relocated) <= published, published
        assert not published & set(doomed_ids), published


# ^ external git_aware_processor.py:GitAwareDocumentProcessor.get_git_status reports "unknown" when git cannot be read
# ^ why the whole child, not one call; the CLI reads git status more than once before smart_index uses it
_UNKNOWN_NEXT_COMMIT_READ = """
import os as _os

if _os.path.exists(_FLAG):
    _os.unlink(_FLAG)
    from code_indexer.services import git_aware_processor as _git_aware

    _real_status = _git_aware.GitAwareDocumentProcessor.get_git_status

    def _status_unknown(self, *args, **kwargs):
        status = _real_status(self, *args, **kwargs)
        status["current_commit"] = "unknown"
        return status

    _git_aware.GitAwareDocumentProcessor.get_git_status = _status_unknown
"""


def test_unknown_commit_read_keeps_the_watermark_and_a_later_run_catches_the_deletion(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    flag = tmp_path / "unknown-next-commit-read"
    injection = _injection(flag, _UNKNOWN_NEXT_COMMIT_READ)
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode, injection)
        age_source_files(repo.repo)
        base = _watermark(repo)
        doomed.unlink()
        _git(repo.repo, "add", "-A")
        _git(repo.repo, "commit", "-qm", "remove")

        flag.touch()
        unknown = _index(repo)
        assert unknown.returncode == 0, unknown.stderr[-3000:]
        assert not flag.exists(), "the injected unknown commit read never fired"
        assert _watermark(repo) == base

        following = _index(repo)

        assert following.returncode == 0, following.stderr[-3000:]
        hits = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" not in hits, hits
        assert f"{_KEPT}.py" in hits, hits


def _set_watermark(repo: FakeProviderRepo, commit: str) -> None:
    import json

    metadata = repo.metadata()
    metadata["branch_commit_watermarks"] = {_BRANCH: commit}
    repo.metadata_path.write_text(json.dumps(metadata))


def _remove_and_commit_doomed(repo: FakeProviderRepo, doomed: Path) -> None:
    doomed.unlink()
    _git(repo.repo, "add", "-A")
    _git(repo.repo, "commit", "-qm", "remove")


def test_missing_watermark_commit_routes_to_a_reconcile(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode)
        age_source_files(repo.repo)
        _set_watermark(repo, "0123456789abcdef" * 2 + "01234567")
        _remove_and_commit_doomed(repo, doomed)

        run = _index(repo)

        assert run.returncode == 0, run.stderr[-3000:]
        hits = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" not in hits, hits
        assert f"{_KEPT}.py" in hits, hits
        assert _watermark(repo) == _head(repo)


_FAIL_NEXT_GIT_DELTA = """
import os as _os
import subprocess as _subprocess

if _os.path.exists(_FLAG):
    from code_indexer.services import smart_indexer as _module

    _real_run = _subprocess.run

    def _run_failing_delta_once(cmd, *args, **kwargs):
        if list(cmd[:3]) == ["git", "diff", "--name-status"] and _os.path.exists(_FLAG):
            _os.unlink(_FLAG)
            return _subprocess.CompletedProcess(cmd, 128, b"", b"injected failure")
        return _real_run(cmd, *args, **kwargs)

    _module.subprocess.run = _run_failing_delta_once
"""


def test_failed_git_delta_fails_the_run_and_keeps_the_watermark(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    flag = tmp_path / "fail-next-git-delta"
    injection = _injection(flag, _FAIL_NEXT_GIT_DELTA)
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode, injection)
        age_source_files(repo.repo)
        base = _watermark(repo)
        _remove_and_commit_doomed(repo, doomed)

        flag.touch()
        failed = _index(repo)
        assert failed.returncode != 0, failed.stdout[-2000:]
        assert not flag.exists(), "the injected delta failure never fired"
        assert _watermark(repo) == base

        following = _index(repo)

        assert following.returncode == 0, following.stderr[-3000:]
        hits = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" not in hits, hits


# ^ why match by payload, not caller; an un-hide is the only update whose hidden_branches lacks the branch
_FAIL_NEXT_UNHIDE = """
import os as _os

if _os.path.exists(_FLAG):
    from code_indexer.storage import filesystem_vector_store as _store

    _real_update = _store.FilesystemVectorStore._batch_update_payload_only

    def _unhide_failing_once(self, updates, *args, **kwargs):
        unhide = any(
            "master" not in (u.get("payload", {}).get("hidden_branches") or [])
            for u in updates
        )
        if unhide and _os.path.exists(_FLAG):
            _os.unlink(_FLAG)
            return False
        return _real_update(self, updates, *args, **kwargs)

    _store.FilesystemVectorStore._batch_update_payload_only = _unhide_failing_once
"""


def test_failed_unhide_fails_the_run_and_the_file_returns_on_the_next_run(
    tmp_path: Path,
    cluster_mode: bool,  # noqa: F811
) -> None:
    flag = tmp_path / "fail-next-unhide"
    injection = _injection(flag, _FAIL_NEXT_UNHIDE)
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode, injection)
        content = doomed.read_text()
        _remove_and_commit_doomed(repo, doomed)
        hiding = repo.cidx(*repo.index_args(), "--fts", "--reconcile")
        assert hiding.returncode == 0, hiding.stderr[-3000:]
        doomed.write_text(content)
        _git(repo.repo, "add", "-A")
        _git(repo.repo, "commit", "-qm", "restore")
        base = _watermark(repo)

        flag.touch()
        failed = repo.cidx(*repo.index_args(), "--fts", "--reconcile")
        assert failed.returncode != 0, failed.stdout[-2000:]
        assert not flag.exists(), "the injected un-hide failure never fired"
        assert _watermark(repo) == base

        following = _index(repo)

        assert following.returncode == 0, following.stderr[-3000:]
        hits = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" in hits, hits


def _break_store(repo: FakeProviderRepo, how: str) -> None:
    if how == "corrupt":
        with open(repo.chunks_db, "r+b") as db:
            db.write(b"not a sqlite database at all" * 4)
    elif how == "missing":
        repo.chunks_db.unlink()
    else:
        repo.chunks_db.chmod(0o444)


# ^ see services/index_failure_exit_codes.py:index_failure_exit_code; 86 corrupt, 87 other store failure, 89 missing store
@pytest.mark.parametrize(
    "how, exit_code",
    [("corrupt", 86), ("missing", 89), ("readonly", 87)],
    ids=["corrupt-86", "missing-89", "readonly-87"],
)
def test_deletion_only_incremental_store_failure_exits_with_its_typed_code(
    tmp_path: Path, how: str, exit_code: int
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, False)
        age_source_files(repo.repo)
        _remove_and_commit_doomed(repo, doomed)
        _break_store(repo, how)
        run = _index(repo)

        assert run.returncode == exit_code, run.stdout[-2000:]
