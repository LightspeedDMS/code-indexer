"""Bug #1746 Change 4 wiring: process_files_high_throughput() must call the
chunk-store preflight check BEFORE the file-submission loop begins.

Root cause (production incident, GitHub issue #1746): a root-owned/
unwritable chunks.db placeholder was only ever discovered when the FIRST
file's vector-storage write actually attempted the open -- by then hashing
and (in a real repo) chunking/embedding work had already happened for that
file, and without Change 2's fix every remaining file would have been
touched too.

This is the wiring test: it proves process_files_high_throughput() itself
invokes FilesystemVectorStore.preflight_chunk_store_writable() before
submitting a single file, for the common case (chunks.db already unwritable
before the run starts) -- distinct from the Change 2 integration test
(tests/integration/services/test_high_throughput_processor_1746.py), which
deliberately disables this preflight to isolate the mid-batch cancellation
mechanism for the case where the store becomes unwritable AFTER the
preflight passed.

Real HighThroughputProcessor, real FilesystemVectorStore (CHUNKS_DB
layout), real temp directory, real FixedSizeChunker/FileFinder, and a
deterministic FAKE (not Mock()) EmbeddingProvider (no network calls).
"""

import hashlib
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pytest

from code_indexer.config import Config
from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
from code_indexer.services.file_identifier import FileIdentifier
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.services.high_throughput_processor import HighThroughputProcessor
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError

VECTOR_DIM = 8
FAKE_MODEL_TOKEN_LIMIT = 120_000


class _CountingFixedSizeChunker(FixedSizeChunker):
    """Real FixedSizeChunker subclass (not a mock/monkeypatch) that records
    every chunk_file() call, then delegates to the real algorithm via
    super(). This test expects the call list to stay empty (the preflight
    check must abort before any file is chunked)."""

    def __init__(self, config, calls: List[Path]) -> None:
        super().__init__(config)
        self._calls = calls

    def chunk_file(self, file_path: Path, repo_root: Optional[Path] = None):
        self._calls.append(file_path)
        return super().chunk_file(file_path, repo_root=repo_root)


class _CountingFileIdentifier(FileIdentifier):
    """Real FileIdentifier subclass (not a mock/monkeypatch) that records
    every get_file_metadata() call, then delegates to the real
    implementation via super(). Bug #1746 M1 (code review finding): the
    preflight check's own comment claims it runs before ANY file is
    hashed/chunked/embedded, but it used to run AFTER the entire parallel
    hash phase -- this class lets a test prove the hash phase itself
    never starts."""

    def __init__(self, project_dir, config, calls: List[Path]) -> None:
        super().__init__(project_dir, config)
        self._calls = calls

    def get_file_metadata(self, file_path: Path):
        self._calls.append(file_path)
        return super().get_file_metadata(file_path)


class DeterministicFakeEmbeddingProvider(EmbeddingProvider):
    """Real EmbeddingProvider implementation with deterministic, hash-derived
    vectors -- no network calls, no mocking of the interface under test."""

    def __init__(self, console=None):
        super().__init__(console)

    def _vector_for(self, text: str) -> List[float]:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        seed = int(text_hash[:8], 16)
        rng = np.random.default_rng(seed)
        vec = rng.random(VECTOR_DIM).astype(np.float32)
        vec = vec / np.linalg.norm(vec)
        return vec.tolist()  # type: ignore[no-any-return]

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ) -> List[float]:
        return self._vector_for(text)

    def get_embeddings_batch(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        embedding_purpose=None,
        retry: bool = True,
    ) -> List[List[float]]:
        return [self._vector_for(t) for t in texts]

    def get_embedding_with_metadata(
        self, text: str, model: Optional[str] = None, *, embedding_purpose=None
    ) -> EmbeddingResult:
        return EmbeddingResult(
            embedding=self._vector_for(text),
            model="voyage-code-3",
            tokens_used=len(text.split()),
            provider="fake-voyage-ai",
        )

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], model: Optional[str] = None, *, embedding_purpose=None
    ) -> BatchEmbeddingResult:
        return BatchEmbeddingResult(
            embeddings=[self._vector_for(t) for t in texts],
            model="voyage-code-3",
            total_tokens_used=sum(len(t.split()) for t in texts),
            provider="fake-voyage-ai",
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "name": "voyage-code-3",
            "provider": "fake-voyage-ai",
            "dimensions": VECTOR_DIM,
            "max_tokens": 16000,
            "supports_batch": True,
            "api_endpoint": "fake://test",
        }

    def get_provider_name(self) -> str:
        return "voyage-ai"

    def get_current_model(self) -> str:
        return "voyage-code-3"

    def supports_batch_processing(self) -> bool:
        return True

    def _get_model_token_limit(self) -> int:
        return FAKE_MODEL_TOKEN_LIMIT


NUM_FILES = 15


def _write_repo(codebase_dir: Path) -> List[Path]:
    codebase_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for i in range(NUM_FILES):
        p = codebase_dir / f"module_{i:03d}.py"
        p.write_text(f"# module {i}\ndef function_{i}():\n    return {i}\n" * 20)
        files.append(p)
    return files


@pytest.mark.integration
class TestBug1746Change4PreflightWiring:
    """AC: Given a target collection's chunks.db exists but cannot be
    opened for write, when an indexing run starts, then the run fails
    within a bounded short time (no files chunked/embedded) with
    files_processed == 0."""

    def test_preflight_blocks_before_any_file_reaches_the_pipeline(
        self, tmp_path
    ) -> None:
        codebase_dir = tmp_path / "repo"
        files = _write_repo(codebase_dir)

        index_dir = codebase_dir / ".code-indexer" / "index"
        store = FilesystemVectorStore(
            base_path=index_dir, use_chunks_db_for_new_collections=True
        )
        collection_name = "voyage-code-3"
        store.create_collection(collection_name, vector_size=VECTOR_DIM)

        collection_path = index_dir / collection_name
        chunks_db_path = collection_path / "chunks.db"
        chunks_db_path.touch()
        os.chmod(chunks_db_path, 0o000)

        try:
            config = Config(codebase_dir=codebase_dir)
            provider = DeterministicFakeEmbeddingProvider()
            processor = HighThroughputProcessor(
                config=config,
                embedding_provider=provider,
                vector_store_client=store,
            )

            chunk_file_calls: List[Path] = []
            processor.fixed_size_chunker = _CountingFixedSizeChunker(
                config, chunk_file_calls
            )

            start = time.time()
            with pytest.raises(ChunkStoreUnavailableError):
                processor.process_files_high_throughput(
                    files=files,
                    vector_thread_count=4,
                    batch_size=50,
                )
            elapsed = time.time() - start

            assert chunk_file_calls == [], (
                f"expected ZERO files to reach the chunking pipeline -- "
                f"the preflight check must fail before any file "
                f"submission -- but {len(chunk_file_calls)} were touched"
            )
            assert elapsed < 5.0, (
                f"preflight failure took {elapsed:.2f}s -- expected "
                f"near-instant (no hashing/chunking/embedding work at all)"
            )
        finally:
            os.chmod(chunks_db_path, 0o644)

    def test_preflight_blocks_before_hash_phase_not_just_before_chunking(
        self, tmp_path
    ) -> None:
        """M1 (code review finding): the preflight check's own comment
        claims it runs "before any file is hashed/chunked/embedded", but
        it used to run AFTER the entire parallel hash phase -- a real
        repro showed every file get hashed before the abort fired. This
        test proves the hash phase itself never starts."""
        codebase_dir = tmp_path / "repo"
        files = _write_repo(codebase_dir)

        index_dir = codebase_dir / ".code-indexer" / "index"
        store = FilesystemVectorStore(
            base_path=index_dir, use_chunks_db_for_new_collections=True
        )
        collection_name = "voyage-code-3"
        store.create_collection(collection_name, vector_size=VECTOR_DIM)

        collection_path = index_dir / collection_name
        chunks_db_path = collection_path / "chunks.db"
        chunks_db_path.touch()
        os.chmod(chunks_db_path, 0o000)

        try:
            config = Config(codebase_dir=codebase_dir)
            provider = DeterministicFakeEmbeddingProvider()
            processor = HighThroughputProcessor(
                config=config,
                embedding_provider=provider,
                vector_store_client=store,
            )

            hash_calls: List[Path] = []
            processor.file_identifier = _CountingFileIdentifier(
                config.codebase_dir, config, hash_calls
            )

            with pytest.raises(ChunkStoreUnavailableError):
                processor.process_files_high_throughput(
                    files=files,
                    vector_thread_count=4,
                    batch_size=50,
                )

            assert hash_calls == [], (
                f"expected ZERO files to reach the hash phase -- the "
                f"preflight check must fail BEFORE hashing begins, not "
                f"merely before chunking -- but {len(hash_calls)} files "
                f"were hashed"
            )
        finally:
            os.chmod(chunks_db_path, 0o644)
