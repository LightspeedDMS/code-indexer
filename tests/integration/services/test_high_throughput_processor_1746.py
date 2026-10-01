"""Bug #1746 Change 2: HighThroughputProcessor must STOP the batch (cancel
not-yet-started futures, propagate the fatal error) instead of continuing to
submit/process every remaining file when a fatal ChunkStoreUnavailableError
surfaces from any in-flight file.

Root cause (production incident, GitHub issue #1746): the completion loop in
process_files_high_throughput used to treat ANY exception from a file's
future the same way -- increment failed_files and move on to the next
already-submitted file. With a fatal chunk-store-open failure (Change 1),
this meant the batch burned CPU processing every remaining file in the repo
before discarding each result, instead of aborting immediately.

This is a REAL integration test: a real HighThroughputProcessor, a real
FilesystemVectorStore (CHUNKS_DB layout) writing real files to a real temp
directory, a real FixedSizeChunker/FileFinder (via DocumentProcessor), and a
deterministic FAKE (not Mock()) EmbeddingProvider implementing the real
interface with no network calls. The ONLY thing replaced is the network-bound
embedding call.

The Change 4 preflight check (added in the same issue) is deliberately
monkeypatched to a no-op here so THIS test isolates Change 2's own mid-batch
mechanism -- without that, Change 4 would catch the unwritable chunks.db
before any file is even submitted, and this test would never exercise
Change 2's cancellation-of-in-flight-futures code path at all. Change 4 gets
its own dedicated test elsewhere.
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
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.services.high_throughput_processor import HighThroughputProcessor
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError


class _CountingFixedSizeChunker(FixedSizeChunker):
    """Real FixedSizeChunker subclass (not a mock/monkeypatch) that records
    every chunk_file() call and adds a small artificial delay, then
    delegates to the real algorithm via super(). Used to instrument how
    many files actually reach the chunking pipeline without replacing the
    real chunking behavior."""

    def __init__(self, config, calls: List[Path], delay_seconds: float) -> None:
        super().__init__(config)
        self._calls = calls
        self._delay_seconds = delay_seconds

    def chunk_file(self, file_path: Path, repo_root: Optional[Path] = None):
        self._calls.append(file_path)
        time.sleep(self._delay_seconds)
        return super().chunk_file(file_path, repo_root=repo_root)


VECTOR_DIM = 8
FAKE_MODEL_TOKEN_LIMIT = 120_000


class DeterministicFakeEmbeddingProvider(EmbeddingProvider):
    """Real EmbeddingProvider implementation with deterministic, hash-derived
    vectors -- no network calls, no mocking of the interface under test.
    Mirrors the established pattern in
    tests/unit/server/services/test_search_service_multimodal_real_infra_1480.py.
    """

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
        # Duck-typed hook FileChunkingManager calls on the real
        # VoyageAI/Cohere provider classes (batching decision) -- not part
        # of the EmbeddingProvider ABC itself.
        return FAKE_MODEL_TOKEN_LIMIT


NUM_FILES = 30
PER_FILE_DELAY_SECONDS = 0.25


def _write_repo(codebase_dir: Path) -> List[Path]:
    codebase_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for i in range(NUM_FILES):
        p = codebase_dir / f"module_{i:03d}.py"
        p.write_text(f"# module {i}\ndef function_{i}():\n    return {i}\n" * 20)
        files.append(p)
    return files


@pytest.mark.integration
class TestBug1746Change2StopBatchOnFatalChunkStoreError:
    """AC: Given ChunkStoreUnavailableError is raised from any in-flight
    file future, when the processor observes it, then: (a) no further files
    are submitted, (b) already-submitted-but-not-started futures are
    cancelled, (c) the error propagates to the caller (SmartIndexer) rather
    than the run completing with a large failed_files count and success
    semantics. Given N files where file K (K well below N) hits the fatal
    error, files_processed + chunks_created reflects work on fewer than N
    files."""

    def test_fatal_error_stops_batch_before_all_files_touched(
        self, tmp_path, monkeypatch
    ) -> None:
        codebase_dir = tmp_path / "repo"
        files = _write_repo(codebase_dir)

        index_dir = codebase_dir / ".code-indexer" / "index"
        store = FilesystemVectorStore(
            base_path=index_dir, use_chunks_db_for_new_collections=True
        )
        collection_name = "voyage-code-3"
        store.create_collection(collection_name, vector_size=VECTOR_DIM)

        # Bug #1746 Change 4 is deliberately disabled for THIS test -- see
        # module docstring. Isolates Change 2's own mechanism.
        if hasattr(store, "preflight_chunk_store_writable"):
            monkeypatch.setattr(
                store, "preflight_chunk_store_writable", lambda *a, **k: None
            )

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

            # Real chunker (a real FixedSizeChunker subclass, not a
            # mock/monkeypatch), instrumented with a counting wrapper + a
            # small per-file delay so a "process every remaining file"
            # regression would take proportionally longer
            # (NUM_FILES * PER_FILE_DELAY_SECONDS / thread_count), while the
            # fixed batch-stop behavior finishes in a small, bounded number
            # of files regardless of NUM_FILES.
            chunk_file_calls: List[Path] = []
            processor.fixed_size_chunker = _CountingFixedSizeChunker(
                config, chunk_file_calls, PER_FILE_DELAY_SECONDS
            )

            start = time.time()
            with pytest.raises(ChunkStoreUnavailableError):
                processor.process_files_high_throughput(
                    files=files,
                    vector_thread_count=2,
                    batch_size=50,
                )
            elapsed = time.time() - start

            # (b)/(c): fatal error propagated, and nowhere close to every
            # file reached the pipeline. Old buggy behavior would touch all
            # NUM_FILES; the fix must touch only a small, bounded number
            # (thread pool is thread_count+2 = 4 workers).
            assert len(chunk_file_calls) < NUM_FILES, (
                f"expected far fewer than {NUM_FILES} files to reach the "
                f"chunking pipeline before the fatal error aborted the "
                f"batch, but {len(chunk_file_calls)} files were touched"
            )
            assert len(chunk_file_calls) <= 8, (
                f"expected the number of touched files to stay close to the "
                f"thread pool's in-flight capacity (4 workers), got "
                f"{len(chunk_file_calls)}"
            )

            # Bounded wall-clock time: old behavior (all NUM_FILES touched
            # serially through 2 worker threads at PER_FILE_DELAY_SECONDS
            # each) would take at least
            # NUM_FILES * PER_FILE_DELAY_SECONDS / 2 =~ 3.75s just for the
            # injected delay, on top of real chunk/embed work. The fixed
            # behavior aborts after only a handful of files.
            old_behavior_minimum = NUM_FILES * PER_FILE_DELAY_SECONDS / 2
            assert elapsed < old_behavior_minimum, (
                f"fatal error took {elapsed:.2f}s to surface -- expected "
                f"well under the {old_behavior_minimum:.2f}s the old "
                f"'process every remaining file' behavior would have taken"
            )
        finally:
            os.chmod(chunks_db_path, 0o644)
