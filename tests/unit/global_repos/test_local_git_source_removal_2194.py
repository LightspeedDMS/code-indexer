from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Tuple

import pytest

from code_indexer.storage.shared.hnsw_sync_state import (
    CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
)
from tests.utils.cidx_fake_provider_repo import FakeProviderRepo, unique_python_text
from tests.utils.fake_voyage_provider import running_fake_voyage_server
from tests.utils.golden_repo_metadata_stores import golden_repo_metadata_store
from tests.utils.refresh_fake_provider_env import (
    fake_provider_refresh_env,
    index_repo_for_real,
)
from tests.utils.refresh_fatal_store_harness import (
    ALIAS,
    Harness,
    build_harness,
)
from tests.utils.source_tree_age import age_source_files

pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]

_FAKE_KEY = "issue-2194-fake-key"
_GIT = ["git", "-c", "user.email=test@example.com", "-c", "user.name=test"]
_BRANCH = "master"
_QUERY_TIMEOUT_SECONDS = 120
_DOOMED = "doomed_000"
_KEPT = "kept_000"
_ALL_RESULTS = "50"

# ^ external a PostgreSQL-mode server passes this env to its cidx children => HNSW sync epoch disabled (AC46 fail-closed)
_EPOCH_MODES = [
    pytest.param(False, id="solo-epoch"),
    pytest.param(True, id="cluster-fail-closed"),
]


@pytest.fixture(params=_EPOCH_MODES)
def cluster_mode(request: Any, monkeypatch: pytest.MonkeyPatch) -> bool:
    if request.param:
        monkeypatch.setenv(CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV, "1")
    else:
        monkeypatch.delenv(CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV, raising=False)
    return bool(request.param)


@pytest.fixture
def metadata(tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store("sqlite", tmp_path) as backend:
        yield backend


@pytest.fixture
def fake_provider_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cluster_mode: bool
) -> Iterator[Tuple[Any, Dict[str, str], Path]]:
    with fake_provider_refresh_env(tmp_path, monkeypatch, _FAKE_KEY) as env:
        yield env


def _write(source: Path, name: str) -> None:
    (source / f"{name}.py").write_text(unique_python_text(name, 40))


def _git(source: Path, *args: str) -> None:
    subprocess.run([*_GIT, *args], cwd=source, check=True, capture_output=True)


def _git_init(source: Path, ignored: str) -> None:
    # ^ coupled with CidxMetaBackupBootstrap._REQUIRED_GITIGNORE_ENTRIES; a tracked index => snapshot restore rolls it back
    (source / ".gitignore").write_text(ignored)
    _git(source, "init", "-q", "-b", _BRANCH)
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "base")


def _source_builder(
    fake: Any, env: Dict[str, str], git_aware: bool
) -> Callable[[Path], None]:
    def build(source: Path) -> None:
        source.mkdir(parents=True)
        for i in range(3):
            _write(source, f"kept_{i:03d}")
        if git_aware:
            _git_init(source, ".code-indexer/\n.snapshot-reader-leases/\n")
        index_repo_for_real(source, fake, env, ALIAS)

    return build


def _refresh(harness: Harness) -> None:
    result = harness.scheduler._execute_refresh(ALIAS)
    assert result["success"] is True, result


def _published(harness: Harness) -> Path:
    target = harness.scheduler.alias_manager.read_alias(ALIAS)
    assert target is not None
    return Path(target)


def _query(published: Path, env: Dict[str, str], *args: str) -> str:
    # ^ external reranker endpoints are not routed to the fake => disabled for every query mode
    command = ["query", *args, "--rerank-query", "", "--quiet"]
    proc = subprocess.run(
        [sys.executable, "-m", "code_indexer.cli", *command],
        cwd=published,
        env=env,
        capture_output=True,
        text=True,
        timeout=_QUERY_TIMEOUT_SECONDS,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return proc.stdout


def _semantic_hits(published: Path, env: Dict[str, str]) -> str:
    return _query(published, env, "unique marker text", "--limit", _ALL_RESULTS)


def _fts_hits(published: Path, env: Dict[str, str], name: str) -> str:
    return _query(published, env, f"{name}_function_7", "--fts")


def _hidden_branches_by_path(published: Path) -> Dict[str, List[List[str]]]:
    from code_indexer.storage.sqlite_chunk_store import ChunkStore

    dbs = list((published / ".code-indexer" / "index").glob("*/chunks.db"))
    assert len(dbs) == 1, dbs
    rows: Dict[str, List[List[str]]] = {}
    with ChunkStore(dbs[0], read_only=True) as store:
        for record in store.stream_all():
            payload = record["payload"]
            hidden = list(payload.get("hidden_branches") or [])
            rows.setdefault(str(payload.get("path")), []).append(hidden)
    return rows


def _watermarks(source: Path) -> Dict[str, Any]:
    metadata = json.loads(
        (source / ".code-indexer" / "metadata-voyage-ai.json").read_text()
    )
    return {
        "branch_commit_watermarks": metadata.get("branch_commit_watermarks"),
        "current_commit": metadata.get("current_commit"),
    }


def _add_refresh_then_remove(
    harness: Harness, fake: Any, env: Dict[str, str], git_aware: bool
) -> Path:
    # ^ hazard fresh mtimes => the incremental re-plans kept files and branch isolation hides the gone one; #2194 needs a plan of zero files
    age_source_files(harness.source)
    _refresh(harness)
    _write(harness.source, _DOOMED)
    if git_aware:
        _git(harness.source, "add", ".")
        _git(harness.source, "commit", "-qm", "add")
    age_source_files(harness.source)
    _refresh(harness)
    published = _published(harness)
    # ^ hazard asserting on an inline call with env => pytest prints env secrets; bind results first
    fts_before = _fts_hits(published, env, _DOOMED)
    semantic_before = _semantic_hits(published, env)
    assert f"{_DOOMED}.py" in fts_before, fts_before
    assert f"{_DOOMED}.py" in semantic_before, semantic_before

    (harness.source / f"{_DOOMED}.py").unlink()
    if git_aware:
        _git(harness.source, "add", "-A")
        _git(harness.source, "commit", "-qm", "remove")
    fake.ledger.begin_run("removing-refresh")
    _refresh(harness)
    assert fake.ledger.run_stats("removing-refresh")["inputs"] == 0
    return _published(harness)


def _removed_and_published(
    tmp_path: Path,
    metadata: Any,
    fake_provider_env: Tuple[Any, Dict[str, str], Path],
    git_aware: bool,
) -> Tuple[Harness, Path, Dict[str, str]]:
    fake, env, _ = fake_provider_env
    harness = build_harness(
        tmp_path,
        metadata,
        snapshot_mode="clean",
        source_builder=_source_builder(fake, env, git_aware),
    )
    return harness, _add_refresh_then_remove(harness, fake, env, git_aware), env


_SOURCE_KINDS = [pytest.param(True, id="git"), pytest.param(False, id="non-git")]


class TestRemovedLocalFileLeavesThePublishedIndex:
    @pytest.mark.parametrize("git_aware", _SOURCE_KINDS)
    def test_removed_file_leaves_fts_and_the_published_store(
        self,
        tmp_path: Path,
        metadata: Any,
        fake_provider_env: Tuple[Any, Dict[str, str], Path],
        git_aware: bool,
    ) -> None:
        harness, published, env = _removed_and_published(
            tmp_path, metadata, fake_provider_env, git_aware
        )

        evidence = _watermarks(harness.source)
        fts_doomed = _fts_hits(published, env, _DOOMED)
        fts_kept = _fts_hits(published, env, _KEPT)
        assert f"{_DOOMED}.py" not in fts_doomed, (fts_doomed, evidence)
        assert f"{_KEPT}.py" in fts_kept, fts_kept
        rows = _hidden_branches_by_path(published)
        assert rows.get(f"{_KEPT}.py") == [[]], rows
        if git_aware:
            assert rows.get(f"{_DOOMED}.py") == [[_BRANCH]], (rows, evidence)
        else:
            assert f"{_DOOMED}.py" not in rows, rows

    @pytest.mark.parametrize("git_aware", _SOURCE_KINDS)
    def test_removed_file_leaves_semantic_search(
        self,
        tmp_path: Path,
        metadata: Any,
        fake_provider_env: Tuple[Any, Dict[str, str], Path],
        git_aware: bool,
    ) -> None:
        _, published, env = _removed_and_published(
            tmp_path, metadata, fake_provider_env, git_aware
        )

        semantic = _semantic_hits(published, env)
        assert f"{_KEPT}.py" in semantic, semantic
        assert f"{_DOOMED}.py" not in semantic, semantic


def _hnsw_fingerprint(collection_dir: Path) -> Dict[str, Any]:
    meta = json.loads((collection_dir / "collection_meta.json").read_text())
    index = collection_dir / "hnsw_index.bin"
    sync_state = collection_dir / "hnsw_sync_state.json"
    return {
        "bin": index.read_bytes(),
        "inode": index.stat().st_ino,
        "last_rebuild": meta.get("hnsw_index", {}).get("last_rebuild"),
        "is_stale": meta.get("hnsw_index", {}).get("is_stale"),
        "sync_state": sync_state.read_text() if sync_state.exists() else None,
    }


def _cli_semantic_hits(repo: FakeProviderRepo) -> str:
    proc = repo.query("unique marker text", "--limit", _ALL_RESULTS)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return proc.stdout


def _reconcile(repo: FakeProviderRepo) -> "subprocess.CompletedProcess[str]":
    return repo.cidx(*repo.index_args(), "--fts", "--reconcile")


def _indexed_git_repo(
    tmp_path: Path, server: Any, cluster_mode: bool, extra_sitecustomize: str = ""
) -> Tuple[FakeProviderRepo, Path]:
    repo = FakeProviderRepo(tmp_path, server, _FAKE_KEY, extra_sitecustomize)
    if cluster_mode:
        repo.env[CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV] = "1"
    repo.write_files("kept", 3)
    doomed = repo.repo / "src" / f"{_DOOMED}.py"
    doomed.write_text(unique_python_text(_DOOMED, 40))
    _git_init(repo.repo, ".code-indexer/\n")
    indexed = repo.cidx(*repo.index_args(), "--fts")
    assert indexed.returncode == 0, indexed.stderr[-3000:]
    return repo, doomed


def _remove_and_commit(repo: FakeProviderRepo, path: Path) -> None:
    path.unlink()
    _git(repo.repo, "add", "-A")
    _git(repo.repo, "commit", "-qm", "remove")


def test_reconcile_that_only_unhides_a_restored_file_returns_it_to_semantic_search(
    tmp_path: Path, cluster_mode: bool
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode)
        content = doomed.read_text()

        _remove_and_commit(repo, doomed)
        hiding = _reconcile(repo)
        assert hiding.returncode == 0, hiding.stderr[-3000:]
        hidden = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" not in hidden, hidden

        doomed.write_text(content)
        _git(repo.repo, "add", "-A")
        _git(repo.repo, "commit", "-qm", "restore")
        server.ledger.begin_run("unhiding-reconcile")
        unhiding = _reconcile(repo)

        assert unhiding.returncode == 0, unhiding.stderr[-3000:]
        assert server.ledger.run_stats("unhiding-reconcile")["inputs"] == 0
        restored = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" in restored, restored
        assert f"{_KEPT}.py" in restored, restored


def _relocate_stored_row(
    repo: FakeProviderRepo, stored: str, new_path: str
) -> List[str]:
    from code_indexer.storage.sqlite_chunk_store import ChunkStore

    ids = _stored_ids(repo, stored)
    assert ids, stored
    with ChunkStore(repo.chunks_db) as store:
        for point_id in ids:
            assert store.update_payload_fields(point_id, {"path": new_path})
    return ids


def _stored_ids(repo: FakeProviderRepo, path: str) -> List[str]:
    return [pid for pid, stored in repo.stored_rows().items() if stored == path]


def _published_hnsw_ids(repo: FakeProviderRepo) -> List[str]:
    meta = json.loads((repo.collection_dir / "collection_meta.json").read_text())
    return [str(pid) for pid in meta["hnsw_index"]["id_mapping"].values()]


@pytest.mark.parametrize("with_added_file", [False, True], ids=["alone", "added"])
def test_reconcile_keeps_a_preserved_outside_root_vector_in_semantic_search(
    tmp_path: Path, cluster_mode: bool, with_added_file: bool
) -> None:
    outside = tmp_path / "outside" / "external_000.py"
    outside.parent.mkdir()
    outside.write_text("# exists outside the codebase root\n")
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode)
        relocated = repo.repo / "src" / "kept_002.py"
        relocated_ids = _relocate_stored_row(repo, "src/kept_002.py", str(outside))
        doomed_ids = _stored_ids(repo, f"src/{_DOOMED}.py")
        relocated.unlink()
        _git(repo.repo, "add", "-A")
        _git(repo.repo, "commit", "-qm", "relocated")
        # ^ why HNSW ids, not CLI query output; the CLI's git branch filter drops out-of-root results, the server serves HNSW
        assert set(relocated_ids) <= set(_published_hnsw_ids(repo))

        _remove_and_commit(repo, doomed)
        if with_added_file:
            (repo.repo / "src" / "added_000.py").write_text(
                unique_python_text("added_000", 40)
            )
            _git(repo.repo, "add", "-A")
            _git(repo.repo, "commit", "-qm", "added")
        reconciled = _reconcile(repo)

        assert reconciled.returncode == 0, reconciled.stderr[-3000:]
        after = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" not in after, after
        published = set(_published_hnsw_ids(repo))
        assert not published & set(doomed_ids), published
        assert set(relocated_ids) <= published, published


# ^ why fail one HNSW rebuild in the child, not kill it; the hide is committed before the publish either way
_FAIL_NEXT_HNSW_REBUILD = """
import os as _os

if _os.path.exists(_FAIL_FLAG):
    from code_indexer.storage import hnsw_index_manager as _manager

    _real_rebuild = _manager.HNSWIndexManager.rebuild_from_vectors

    def _rebuild_failing_once(self, *args, **kwargs):
        if _os.path.exists(_FAIL_FLAG):
            _os.unlink(_FAIL_FLAG)
            raise OSError("injected HNSW publish failure")
        return _real_rebuild(self, *args, **kwargs)

    _manager.HNSWIndexManager.rebuild_from_vectors = _rebuild_failing_once
"""


def test_reconcile_after_an_interrupted_hide_publish_removes_the_file(
    tmp_path: Path, cluster_mode: bool
) -> None:
    flag = tmp_path / "fail-next-hnsw-rebuild"
    injection = f"_FAIL_FLAG = {str(flag)!r}\n" + _FAIL_NEXT_HNSW_REBUILD
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode, injection)
        _remove_and_commit(repo, doomed)

        flag.touch()
        interrupted = _reconcile(repo)
        assert interrupted.returncode != 0, interrupted.stdout[-2000:]
        assert not flag.exists(), "the injected HNSW failure never fired"
        stale = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" in stale, stale

        server.ledger.begin_run("healing-reconcile")
        healing = _reconcile(repo)

        assert healing.returncode == 0, healing.stderr[-3000:]
        assert server.ledger.run_stats("healing-reconcile")["inputs"] == 0
        healed = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" not in healed, healed
        assert f"{_KEPT}.py" in healed, healed


_FAIL_NEXT_BRANCH_ISOLATION = """
import os as _os

if _os.path.exists(_FAIL_FLAG):
    from code_indexer.services import high_throughput_processor as _processor

    _real_isolation = _processor.HighThroughputProcessor.hide_files_not_in_branch_thread_safe

    def _isolation_failing_once(self, *args, **kwargs):
        if _os.path.exists(_FAIL_FLAG):
            _os.unlink(_FAIL_FLAG)
            raise OSError("injected failure before branch isolation")
        return _real_isolation(self, *args, **kwargs)

    _processor.HighThroughputProcessor.hide_files_not_in_branch_thread_safe = (
        _isolation_failing_once
    )
"""


def test_reconcile_failing_before_branch_isolation_does_not_publish_the_hidden_file(
    tmp_path: Path, cluster_mode: bool
) -> None:
    flag = tmp_path / "fail-next-branch-isolation"
    injection = f"_FAIL_FLAG = {str(flag)!r}\n" + _FAIL_NEXT_BRANCH_ISOLATION
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, doomed = _indexed_git_repo(tmp_path, server, cluster_mode, injection)
        doomed.unlink()
        (repo.repo / "src" / "added_000.py").write_text(
            unique_python_text("added_000", 40)
        )
        _git(repo.repo, "add", "-A")
        _git(repo.repo, "commit", "-qm", "remove and add")

        flag.touch()
        interrupted = _reconcile(repo)
        assert interrupted.returncode != 0, interrupted.stdout[-2000:]
        assert not flag.exists(), "the injected failure never fired"

        quiet = _reconcile(repo)

        assert quiet.returncode == 0, quiet.stderr[-3000:]
        hits = _cli_semantic_hits(repo)
        assert f"{_DOOMED}.py" not in hits, hits
        assert f"{_KEPT}.py" in hits, hits


@pytest.mark.parametrize("change", ["add", "modify"])
def test_reconcile_failing_after_processing_still_publishes_the_new_content(
    tmp_path: Path, cluster_mode: bool, change: str
) -> None:
    flag = tmp_path / "fail-next-branch-isolation"
    injection = f"_FAIL_FLAG = {str(flag)!r}\n" + _FAIL_NEXT_BRANCH_ISOLATION
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, cluster_mode, injection)
        if change == "add":
            target, marker = "added_000.py", "added_000_function_0"
        else:
            target, marker = "kept_001.py", "modified_001_function_0"
        (repo.repo / "src" / target).write_text(
            unique_python_text(marker.rsplit("_function", 1)[0], 40)
        )
        _git(repo.repo, "add", "-A")
        _git(repo.repo, "commit", "-qm", change)

        flag.touch()
        interrupted = _reconcile(repo)
        assert interrupted.returncode != 0, interrupted.stdout[-2000:]
        assert not flag.exists(), "the injected failure never fired"

        quiet = _reconcile(repo)

        assert quiet.returncode == 0, quiet.stderr[-3000:]
        hits = _cli_semantic_hits(repo)
        assert marker in hits, hits


def test_reconcile_with_nothing_to_do_keeps_the_hnsw_index(
    tmp_path: Path, cluster_mode: bool
) -> None:
    with running_fake_voyage_server(_FAKE_KEY) as server:
        repo, _ = _indexed_git_repo(tmp_path, server, cluster_mode)
        before = _hnsw_fingerprint(repo.collection_dir)

        server.ledger.begin_run("quiet-reconcile")
        reconciled = _reconcile(repo)

        assert reconciled.returncode == 0, reconciled.stderr[-3000:]
        assert server.ledger.run_stats("quiet-reconcile")["inputs"] == 0
        after = _hnsw_fingerprint(repo.collection_dir)
        assert after["inode"] == before["inode"]
        assert after["bin"] == before["bin"]
        assert after["last_rebuild"] == before["last_rebuild"]
        assert after["is_stale"] == before["is_stale"]
        assert after["sync_state"] == before["sync_state"]
