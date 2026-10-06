"""A reconcile whose store snapshot cannot be read fails loudly.

The reconcile reads every stored content point in one snapshot. When the
chunks.db behind it is damaged, reconciling against an EMPTY snapshot
re-embeds every file (paid provider calls) and records the store as
verified without having read it. Every snapshot error must propagate:
``smart_index`` marks the run failed (the next refresh retries with
--reconcile) and the CLI maps the error to its exit code (86 for SQLite-
reported corruption -- the restore path -- and 1 otherwise).

Real SmartIndexer, real FilesystemVectorStore with the CHUNKS_DB layout,
real temp git repository and real on-disk damage. The only test double is
the embedding provider (an external service), which records every text it
is asked to embed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Dict, Tuple, Type

import pytest

from code_indexer.config import Config
from code_indexer.services.index_failure_exit_codes import index_failure_exit_code
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import CorruptChunkDataError
from tests.unit.services.test_reconcile_non_git_content_id_2013 import (
    _CountingEmbeddingProvider,
    _committed_git_repo,
)

_SQLITE_HEADER_PAGE_SIZE_OFFSET = 16


def _overwrite_with_non_database_bytes(chunks_db: Path) -> None:
    chunks_db.write_bytes(b"not a sqlite database " * 512)


def _corrupt_every_page_after_the_first(chunks_db: Path) -> None:
    data = bytearray(chunks_db.read_bytes())
    page_size = int.from_bytes(
        data[_SQLITE_HEADER_PAGE_SIZE_OFFSET : _SQLITE_HEADER_PAGE_SIZE_OFFSET + 2],
        "big",
    )
    assert len(data) > page_size, "chunks.db must span more than one page"
    for offset in range(page_size, len(data)):
        data[offset] = 0xAB
    chunks_db.write_bytes(bytes(data))


def _block_the_rollback_journal(chunks_db: Path) -> None:
    (chunks_db.parent / f"{chunks_db.name}-journal").mkdir()


def _corrupt_one_data_blob(chunks_db: Path) -> None:
    conn = sqlite3.connect(str(chunks_db))
    try:
        conn.execute(
            "UPDATE chunks SET data = x'00112233' "
            "WHERE rowid = (SELECT MIN(rowid) FROM chunks)"
        )
        conn.commit()
    finally:
        conn.close()


# case -> (damage, error the snapshot read raises, CLI exit code, message
# fragment). The exact type proves the snapshot read itself raised -- the
# later write path wraps store failures in ChunkStoreUnavailableError.
_DAMAGE_CASES: Dict[
    str, Tuple[Callable[[Path], None], Type[BaseException], int, str]
] = {
    "not_a_database": (
        _overwrite_with_non_database_bytes,
        sqlite3.DatabaseError,
        86,
        "not a database",
    ),
    "malformed": (
        _corrupt_every_page_after_the_first,
        sqlite3.DatabaseError,
        86,
        "malformed",
    ),
    "disk_io_error": (
        _block_the_rollback_journal,
        sqlite3.OperationalError,
        1,
        "disk I/O error",
    ),
    "corrupt_data_blob": (
        _corrupt_one_data_blob,
        CorruptChunkDataError,
        1,
        "corrupt 'data' blob",
    ),
}


def _indexed_chunks_db_repo(
    tmp_path: Path,
) -> Tuple[SmartIndexer, _CountingEmbeddingProvider, Path]:
    repo = _committed_git_repo(tmp_path)
    config = Config(codebase_dir=repo)
    embedder = _CountingEmbeddingProvider()
    store = FilesystemVectorStore(
        base_path=repo / ".code-indexer" / "index",
        use_chunks_db_for_new_collections=True,
    )
    store.ensure_provider_aware_collection(config, embedder)
    indexer = SmartIndexer(
        config=config,
        embedding_provider=embedder,
        vector_store_client=store,
        metadata_path=tmp_path / "meta.json",
    )
    indexer.smart_index(force_full=True, quiet=True)
    assert embedder.embedded_texts, "the initial full index must embed the files"
    collection = store.resolve_collection_name(config, embedder)
    chunks_db = store.base_path / collection / "chunks.db"
    assert chunks_db.exists(), "the store must use the CHUNKS_DB layout"
    return indexer, embedder, chunks_db


@pytest.mark.parametrize("case", sorted(_DAMAGE_CASES))
def test_reconcile_over_a_damaged_store_raises_without_embedding_or_verifying(
    tmp_path: Path, case: str
) -> None:
    """``_reconcile_and_verify`` is the reconcile every entry point runs (the
    ambiguous-store check, --reconcile, the unsealed-resume fallback)."""
    damage, expected_type, expected_exit_code, expected_fragment = _DAMAGE_CASES[case]
    indexer, embedder, chunks_db = _indexed_chunks_db_repo(tmp_path)
    damage(chunks_db)
    embedder.embedded_texts.clear()

    with pytest.raises(Exception) as raised:
        indexer._reconcile_and_verify(
            batch_size=50,
            progress_callback=None,
            git_status=indexer.get_git_status(),
            provider_name=embedder.get_provider_name(),
            model_name=embedder.get_current_model(),
            files_count_to_process=None,
            quiet=True,
            vector_thread_count=None,
            fts_manager=None,
        )

    assert type(raised.value) is expected_type, repr(raised.value)
    assert expected_fragment in str(raised.value), repr(raised.value)
    assert index_failure_exit_code(raised.value) == expected_exit_code
    assert embedder.embedded_texts == [], (
        f"{case}: a reconcile over an unreadable store must not re-embed: "
        f"{embedder.embedded_texts}"
    )
    assert not indexer.progressive_metadata.metadata.get(
        "store_verified_by_reconcile", False
    ), f"{case}: the store was marked verified without being read"


def test_smart_index_reconcile_over_a_corrupt_blob_fails_the_run(
    tmp_path: Path,
) -> None:
    indexer, embedder, chunks_db = _indexed_chunks_db_repo(tmp_path)
    _corrupt_one_data_blob(chunks_db)
    embedder.embedded_texts.clear()

    with pytest.raises(Exception, match="corrupt 'data' blob"):
        indexer.smart_index(reconcile_with_database=True, quiet=True)

    assert embedder.embedded_texts == [], embedder.embedded_texts
    metadata = indexer.progressive_metadata.metadata
    assert not metadata.get("store_verified_by_reconcile", False)
    assert metadata["status"] == "failed", (
        "the next refresh must see a failed run and reconcile again"
    )
