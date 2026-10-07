"""Bug #2056: FTS loses every chunk reused from the embedding cache.

FileChunkingManager deletes ALL FTS documents of a processed file
(delete-by-path supersession, Bug #1761) and then re-added only the chunks
embedded in this run. Chunks whose vectors were reused from the smart
embedding cache (Story #470) were never re-added, so:

* an edited multi-chunk file lost its unchanged chunks from FTS;
* an unchanged file re-processed (re-index, refresh safety buffer, the
  FTS bootstrap of ``cidx index --fts``) lost ALL its FTS documents.

These tests drive the REAL FileChunkingManager with a REAL
FilesystemVectorStore (so cache hits happen exactly as in production) and a
REAL TantivyIndexManager. Only the embedding provider and the chunker are
deterministic test doubles.
"""

# mypy: ignore-errors
# Duck-typed fakes passed to FileChunkingManager's strictly-typed constructor
# params, matching test_fts_duplicate_reindex_1761.py.

import hashlib
import threading
from collections import Counter
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

from code_indexer.services.clean_slot_tracker import CleanSlotTracker
from code_indexer.services.file_chunking_manager import FileChunkingManager
from code_indexer.services.tantivy_index_manager import TantivyIndexManager
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_SIZE = 8
COLLECTION = "voyage-code-3"
CHUNK_SEPARATOR = "\n#---\n"


class _CountingVectorManager:
    """VectorCalculationManager stand-in: deterministic embeddings, and a
    record of every text sent for embedding (the provider-cost ledger)."""

    def __init__(self) -> None:
        self.cancellation_event = threading.Event()
        self.embedding_provider = Mock()
        self.embedding_provider.get_current_model.return_value = COLLECTION
        self.embedding_provider._get_model_token_limit.return_value = 120000
        self.embedded_texts: List[str] = []

    def submit_batch_task(
        self, chunk_texts: List[str], metadata: Dict[str, Any]
    ) -> "Future[Any]":
        from code_indexer.services.vector_calculation_manager import VectorResult

        self.embedded_texts.extend(chunk_texts)
        embeddings = tuple(
            tuple(
                float(b) / 255.0
                for b in hashlib.sha256(text.encode()).digest()[:VECTOR_SIZE]
            )
            for text in chunk_texts
        )
        future: "Future[Any]" = Future()
        future.set_result(
            VectorResult(
                task_id="batch",
                embeddings=embeddings,
                metadata=metadata.copy(),
                processing_time=0.0,
                error=None,
            )
        )
        return future


class _SeparatorChunker:
    """Deterministic chunker: one chunk per CHUNK_SEPARATOR-delimited block,
    with the real chunker's dict shape and 1-based line ranges."""

    def chunk_file(self, file_path: Path, repo_root: Path = None) -> List[Dict]:
        blocks = file_path.read_text().split(CHUNK_SEPARATOR)
        chunks = []
        line = 1
        for index, block in enumerate(blocks):
            n_lines = block.count("\n") + 1
            chunk = {
                "text": block,
                "chunk_index": index,
                "total_chunks": len(blocks),
                "size": len(block),
                "file_path": str(file_path),
                "file_extension": "py",
                "line_start": line,
                "line_end": line + n_lines - 1,
            }
            if IMAGE_MARKER in block:
                # Routed to the multimodal embedding branch.
                chunk["images"] = [{"path": IMAGE_NAME}]
            chunks.append(chunk)
            line += n_lines + 1  # the separator line
        return chunks


IMAGE_MARKER = "IMAGE:"
IMAGE_NAME = "diagram.png"


class _FakeMultimodalClient:
    """VoyageMultimodalClient stand-in returning a deterministic embedding."""

    def __init__(self) -> None:
        self.config = Mock()
        self.config.model = "voyage-multimodal-3"
        self.config.default_dimension = VECTOR_SIZE
        self.calls = 0

    def get_multimodal_embedding(self, text: str, image_paths: List[Path]):
        self.calls += 1
        digest = hashlib.sha256(text.encode()).digest()[:VECTOR_SIZE]
        return [float(b) / 255.0 for b in digest]


def _write_blocks(path: Path, blocks: List[str]) -> None:
    path.write_text(CHUNK_SEPARATOR.join(blocks))


def _index_pass(
    codebase_dir: Path,
    files: List[Path],
    store: FilesystemVectorStore,
    create_new_fts: bool,
    multimodal_client: Optional[_FakeMultimodalClient] = None,
) -> List[str]:
    """One indexing pass over `files` into the SAME on-disk FTS index, as a
    `cidx index --fts` run does. Returns the texts sent for embedding."""
    fts = TantivyIndexManager(codebase_dir / ".code-indexer" / "tantivy_index")
    fts.initialize_index(create_new=create_new_fts)
    vector_manager = _CountingVectorManager()
    try:
        with FileChunkingManager(
            vector_manager=vector_manager,
            chunker=_SeparatorChunker(),
            vector_store_client=store,
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=codebase_dir,
            fts_manager=fts,
            multimodal_client=multimodal_client,
        ) as manager:
            futures = []
            for file_path in files:
                content_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
                metadata = {
                    "project_id": "proj",
                    "file_hash": f"sha256:{content_hash}",
                    "collection_name": COLLECTION,
                    "git_available": False,
                }
                futures.append(
                    manager.submit_file_for_processing(file_path, metadata, None)
                )
            for future in futures:
                result = future.result(timeout=30.0)
                assert result.success, f"File processing failed: {result.error}"
        fts.commit()
    finally:
        fts.close()
    return vector_manager.embedded_texts


def _fts_docs_per_path(index_dir: Path) -> Counter:
    import tantivy

    index = tantivy.Index.open(str(index_dir))
    index.reload()
    searcher = index.searcher()
    counts: Counter = Counter()
    if searcher.num_docs == 0:
        return counts
    hits = searcher.search(tantivy.Query.all_query(), searcher.num_docs).hits
    for _score, address in hits:
        counts[searcher.doc(address).get_first("path")] += 1
    return counts


def _hits(index_dir: Path, token: str) -> List[Dict[str, Any]]:
    fts = TantivyIndexManager(index_dir)
    fts.initialize_index(create_new=False)
    try:
        return fts.search(token, limit=20)
    finally:
        fts.close()


def _new_store(tmp_path: Path) -> FilesystemVectorStore:
    store = FilesystemVectorStore(base_path=tmp_path / ".code-indexer" / "index")
    store.create_collection(collection_name=COLLECTION, vector_size=VECTOR_SIZE)
    return store


BLOCKS = [
    "def alpha():\n    return 'ALPHATOKEN'",
    "def bravo():\n    return 'BRAVOTOKEN'",
    "def charlie():\n    return 'CHARLIETOKEN'",
]


class TestFtsKeepsCacheReusedChunks2056:
    def test_edited_multi_chunk_file_keeps_unchanged_chunk_in_fts(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "module.py"
        index_dir = tmp_path / ".code-indexer" / "tantivy_index"
        store = _new_store(tmp_path)

        _write_blocks(source, BLOCKS)
        first = _index_pass(tmp_path, [source], store, create_new_fts=True)
        assert sorted(first) == sorted(BLOCKS)

        edited = [BLOCKS[0], "def delta():\n    return 'DELTATOKEN'", BLOCKS[2]]
        _write_blocks(source, edited)
        second = _index_pass(tmp_path, [source], store, create_new_fts=False)

        # Cost: only the edited chunk is re-embedded; the others are reused.
        assert second == [edited[1]]
        # Unchanged chunks (cache-reused) are still searchable.
        assert len(_hits(index_dir, "ALPHATOKEN")) == 1
        assert len(_hits(index_dir, "CHARLIETOKEN")) == 1
        # The new content is found, the replaced content is gone.
        assert len(_hits(index_dir, "DELTATOKEN")) == 1
        assert _hits(index_dir, "BRAVOTOKEN") == []
        assert _fts_docs_per_path(index_dir) == Counter({"module.py": 3})

    def test_repeated_unchanged_runs_keep_each_chunk_exactly_once(
        self, tmp_path: Path
    ) -> None:
        index_dir = tmp_path / ".code-indexer" / "tantivy_index"
        store = _new_store(tmp_path)
        first_file = tmp_path / "module.py"
        second_file = tmp_path / "other.py"
        _write_blocks(first_file, BLOCKS)
        _write_blocks(second_file, ["def echo():\n    return 'ECHOTOKEN'"])
        files = [first_file, second_file]

        _index_pass(tmp_path, files, store, create_new_fts=True)
        expected = Counter({"module.py": 3, "other.py": 1})
        assert _fts_docs_per_path(index_dir) == expected

        for _ in range(3):
            embedded = _index_pass(tmp_path, files, store, create_new_fts=False)
            assert embedded == [], "unchanged content must never be re-embedded"
            assert _fts_docs_per_path(index_dir) == expected
            for token in ("ALPHATOKEN", "BRAVOTOKEN", "CHARLIETOKEN", "ECHOTOKEN"):
                assert len(_hits(index_dir, token)) == 1, token

    def test_shrunk_file_leaves_no_stale_chunks(self, tmp_path: Path) -> None:
        source = tmp_path / "module.py"
        index_dir = tmp_path / ".code-indexer" / "tantivy_index"
        store = _new_store(tmp_path)
        _write_blocks(source, BLOCKS)
        _index_pass(tmp_path, [source], store, create_new_fts=True)

        _write_blocks(source, BLOCKS[:2])
        embedded = _index_pass(tmp_path, [source], store, create_new_fts=False)

        assert embedded == []
        assert _fts_docs_per_path(index_dir) == Counter({"module.py": 2})
        assert _hits(index_dir, "CHARLIETOKEN") == []
        assert len(_hits(index_dir, "ALPHATOKEN")) == 1
        assert len(_hits(index_dir, "BRAVOTOKEN")) == 1

    def test_multimodal_chunk_text_is_kept_in_fts(self, tmp_path: Path) -> None:
        source = tmp_path / "module.py"
        index_dir = tmp_path / ".code-indexer" / "tantivy_index"
        (tmp_path / IMAGE_NAME).write_bytes(b"\x89PNG\r\n\x1a\n")
        store = _new_store(tmp_path)
        blocks = [BLOCKS[0], f"# {IMAGE_MARKER} {IMAGE_NAME} FOXTROTTOKEN"]
        _write_blocks(source, blocks)

        for create_new in (True, False):
            multimodal = _FakeMultimodalClient()
            _index_pass(
                tmp_path,
                [source],
                store,
                create_new_fts=create_new,
                multimodal_client=multimodal,
            )
            assert multimodal.calls == 1
            assert _fts_docs_per_path(index_dir) == Counter({"module.py": 2})
            assert len(_hits(index_dir, "FOXTROTTOKEN")) == 1
            assert len(_hits(index_dir, "ALPHATOKEN")) == 1
