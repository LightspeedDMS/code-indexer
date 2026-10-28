"""Story #2119: an embedding already committed in ``chunks.db`` is never sent
to the provider again, and a reuse-lookup error aborts the run instead of
paying.

Every test drives the REAL ``cidx`` CLI (subprocess, the tree under test) in
a temporary non-git repository (the ``local://`` shape) against the local
fake VoyageAI server (scripts/analysis/reembed_repro/fake_voyage_server.py,
plain HTTP on 127.0.0.1, sentinel key): no real provider call is possible.
The fake records the sha256 of every input text, which equals the
``content_hash`` the indexer stores per chunk, so "sent again" is checked
against the real store's rows.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator, List

import pytest

from tests.utils.cidx_fake_provider_repo import (
    FakeProviderRepo,
    kill_after_rows as _kill_after_rows,
    unique_python_text as _file_text,
)
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SENTINEL_KEY = "issue-2119-fake-key"

pytestmark = [pytest.mark.slow, pytest.mark.timeout(240)]


@pytest.fixture
def fake_provider() -> Iterator[object]:
    # The shared helper stops the fake and asserts no ledger violation.
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


def _Repo(root: Path, server) -> FakeProviderRepo:
    """A non-git repository initialised for cidx, pointed at the fake."""
    return FakeProviderRepo(root, server, _SENTINEL_KEY)


def test_rerun_after_kill_sends_no_committed_chunk(
    tmp_path: Path, fake_provider
) -> None:
    """The incident mechanism: a refresh is killed after it committed some
    files' rows; the next run must not pay for any of them again."""
    repo = _Repo(tmp_path, fake_provider)
    repo.write_files("base", 5)
    fake_provider.ledger.begin_run("initial")
    proc = repo.cidx(*repo.index_args())
    assert proc.returncode == 0, proc.stderr[-3000:]
    initial_rows = repo.committed_row_count()
    assert initial_rows > 0

    repo.write_files("synced", 40)
    # Hold every response so the run is slow enough to be killed mid-way.
    fake_provider.ledger.begin_run("killed", hold_seconds=0.4)
    _kill_after_rows(repo, initial_rows + 15, deadline_s=90)

    committed = repo.stored_content_hashes()
    killed_run_committed = committed - fake_provider.ledger.run_hashes("initial")
    assert len(killed_run_committed) >= 15, "the killed run committed too little"

    fake_provider.ledger.begin_run("rerun")
    proc = repo.cidx(*repo.index_args())
    assert proc.returncode == 0, proc.stderr[-3000:]

    resent = fake_provider.ledger.run_hashes("rerun") & committed
    assert resent == set(), (
        f"the rerun paid again for {len(resent)} chunk(s) already committed in "
        f"chunks.db (of {len(killed_run_committed)} committed by the killed run)"
    )
    # The rerun still finished the job: every synced file's content is stored.
    assert fake_provider.ledger.run_hashes("rerun") <= repo.stored_content_hashes()


def test_duplicate_files_in_one_run_are_embedded_once(
    tmp_path: Path, fake_provider
) -> None:
    """Byte-identical copies share content-addressed point ids: the first
    copy of each content group is embedded and committed, its copies reuse
    it in the same run (harness check A3, ``dup_same_run == 0``)."""
    repo = _Repo(tmp_path, fake_provider)
    repo.write_files("unique", 4)
    for group in range(4):
        text = _file_text(f"shared_{group}", 30)
        for copy in range(6):
            (repo.repo / "src" / f"dup_{group}_{copy}.py").write_text(text)
    # Held responses keep each first copy in flight while its copies start.
    fake_provider.ledger.begin_run("initial", hold_seconds=0.3)

    proc = repo.cidx(*repo.index_args())

    assert proc.returncode == 0, proc.stderr[-3000:]
    stats = fake_provider.ledger.run_stats("initial")
    assert stats["dup_same_run"] == 0, (
        f"{stats['dup_same_run']} input(s) were embedded more than once in one run"
    )
    from code_indexer.storage.sqlite_chunk_store import ChunkStore

    with ChunkStore(repo.chunks_db) as store:
        stored_paths = store.distinct_paths()
    # Copies share point ids, so each group's rows carry ONE of its paths;
    # every group and every unique file must be stored.
    assert {p for p in stored_paths if p.startswith("src/unique_")} == {
        f"src/unique_{i:03d}.py" for i in range(4)
    }
    assert {p.split("_")[1] for p in stored_paths if p.startswith("src/dup_")} == {
        "0",
        "1",
        "2",
        "3",
    }


def _chunker(repo: FakeProviderRepo):
    """The tree's chunker, configured from the repository's own config."""
    from code_indexer.config import ConfigManager
    from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker

    return FixedSizeChunker(
        ConfigManager(repo.repo / ".code-indexer" / "config.json").load()
    )


def _chunk_keys(chunker, repo: FakeProviderRepo, path: Path) -> List[str]:
    """sha256 of each chunk text (the indexer's content_hash)."""
    import hashlib

    return [
        hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
        for chunk in chunker.chunk_file(path, repo.repo)
    ]


def test_prepended_chunk_sends_only_the_new_chunk(
    tmp_path: Path, fake_provider
) -> None:
    """A changed file whose old chunks moved to other chunk indexes (here: one
    new chunk prepended) reuses every stored chunk by content: only the new
    chunk is sent."""
    repo = _Repo(tmp_path, fake_provider)
    target = repo.repo / "src" / "long_module.py"
    target.write_text(_file_text("long_module", 200))
    fake_provider.ledger.begin_run("initial")
    proc = repo.cidx(*repo.index_args())
    assert proc.returncode == 0, proc.stderr[-3000:]
    chunker = _chunker(repo)
    old_keys = _chunk_keys(chunker, repo, target)
    assert len(old_keys) >= 3

    step = int(chunker.step_size)
    prefix = ("# prepended " + "x" * 60 + "\n") * (step // 73 + 1)
    target.write_text(prefix[:step] + target.read_text())
    new_keys = _chunk_keys(chunker, repo, target)
    assert new_keys[1:] == old_keys, "fixture: the old chunks must shift by one"

    fake_provider.ledger.begin_run("after-prepend")
    proc = repo.cidx(*repo.index_args())

    assert proc.returncode == 0, proc.stderr[-3000:]
    sent = fake_provider.ledger.run_hashes("after-prepend")
    assert sent == {new_keys[0]}, (
        f"{len(sent & set(old_keys))} stored chunk(s) were sent again after "
        f"moving to another chunk index"
    )


def test_missing_store_of_an_established_collection_aborts(
    tmp_path: Path, fake_provider
) -> None:
    """Codex P2-3: an established CHUNKS_DB collection whose chunks.db went
    missing is not an empty cache. The run fails closed with its own kind
    (Story #2126: missing store, exit 89 -- never corruption), pays nothing
    and does not silently recreate the store. A fresh collection (the
    initial run here) indexes normally."""
    from code_indexer.services.index_failure_exit_codes import (
        EXIT_CODE_CHUNK_STORE_MISSING,
    )

    repo = _Repo(tmp_path, fake_provider)
    repo.write_files("base", 3)
    fake_provider.ledger.begin_run("initial")
    proc = repo.cidx(*repo.index_args())
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert fake_provider.ledger.run_stats("initial")["inputs"] > 0

    repo.chunks_db.unlink()
    future = time.time() + 120
    os.utime(repo.repo / "src" / "base_000.py", (future, future))

    fake_provider.ledger.begin_run("store-missing")
    proc = repo.cidx(*repo.index_args())

    assert fake_provider.ledger.run_stats("store-missing")["inputs"] == 0, (
        "a missing store was read as an empty cache and the provider was paid"
    )
    assert proc.returncode == EXIT_CODE_CHUNK_STORE_MISSING, (
        proc.returncode,
        proc.stderr[-3000:],
    )
    assert not repo.chunks_db.exists(), "the missing store was silently recreated"


def test_missing_store_is_not_recreated_by_the_reconcile_pre_delete(
    tmp_path: Path, fake_provider
) -> None:
    """Codex round 2, P2-A: on a non-git reconcile with a missing
    path_index.bin, the modified-file pre-delete rebuilds the path index
    through a read-write store open BEFORE any file is processed. That open
    must not recreate the missing store of an established collection (an
    empty store would then be read as "nothing stored" and paid again)."""
    from code_indexer.services.index_failure_exit_codes import (
        EXIT_CODE_CHUNK_STORE_MISSING,
    )

    repo = _Repo(tmp_path, fake_provider)
    repo.write_files("base", 3)
    fake_provider.ledger.begin_run("initial")
    proc = repo.cidx(*repo.index_args())
    assert proc.returncode == 0, proc.stderr[-3000:]

    repo.chunks_db.unlink()
    path_index = repo.chunks_db.with_name("path_index.bin")
    if path_index.exists():
        path_index.unlink()
    target = repo.repo / "src" / "base_000.py"
    target.write_text(target.read_text() + "def appended():\n    return 0\n")

    fake_provider.ledger.begin_run("reconcile")
    proc = repo.cidx(*repo.index_args(), "--reconcile")

    assert fake_provider.ledger.run_stats("reconcile")["inputs"] == 0, (
        "the missing store was recreated empty and the provider was paid"
    )
    assert proc.returncode == EXIT_CODE_CHUNK_STORE_MISSING, (
        proc.returncode,
        proc.stderr[-3000:],
    )
    assert not repo.chunks_db.exists(), "the missing store was silently recreated"


def test_fresh_index_and_explicit_clear_build_the_store(
    tmp_path: Path, fake_provider
) -> None:
    """The legitimate creators of a store keep working: a fresh build, and
    an explicit --clear (which clears the layout discriminator first)."""
    repo = _Repo(tmp_path, fake_provider)
    repo.write_files("base", 3)
    fake_provider.ledger.begin_run("fresh")
    proc = repo.cidx(*repo.index_args())
    assert proc.returncode == 0, proc.stderr[-3000:]
    fresh = repo.stored_content_hashes()
    assert fresh

    fake_provider.ledger.begin_run("clear")
    proc = repo.cidx(*repo.index_args(), "--clear")

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert repo.stored_content_hashes() == fresh


def test_corrupt_stored_blob_aborts_before_any_provider_call(
    tmp_path: Path, fake_provider
) -> None:
    """A reuse-lookup error must never be read as "nothing stored": the run
    aborts with the fatal chunk-store exit code and pays nothing."""
    from code_indexer.services.index_failure_exit_codes import (
        EXIT_CODE_CHUNK_STORE_ENVIRONMENT,
    )

    repo = _Repo(tmp_path, fake_provider)
    repo.write_files("base", 3)
    fake_provider.ledger.begin_run("initial")
    proc = repo.cidx(*repo.index_args())
    assert proc.returncode == 0, proc.stderr[-3000:]

    target = repo.repo / "src" / "base_000.py"
    conn = sqlite3.connect(str(repo.chunks_db))
    try:
        damaged = conn.execute(
            "UPDATE chunks SET data = ? WHERE path = ?",
            (b"not-a-zstd-frame", "src/base_000.py"),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    assert damaged > 0
    # Plan the file again: a newer mtime, same content.
    future = time.time() + 120
    os.utime(target, (future, future))

    fake_provider.ledger.begin_run("after-corruption")
    proc = repo.cidx(*repo.index_args())

    assert fake_provider.ledger.run_stats("after-corruption")["inputs"] == 0, (
        "a lookup error was treated as a cache miss and paid the provider"
    )
    assert proc.returncode == EXIT_CODE_CHUNK_STORE_ENVIRONMENT, (
        proc.returncode,
        proc.stderr[-3000:],
    )
