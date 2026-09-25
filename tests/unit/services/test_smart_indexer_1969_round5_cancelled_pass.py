"""Unit tests for Bug #1969 Round 5, finding R4-F3 (P3): a cancelled
reprocess pass must not clear the durable self-heal sidecar entries it
was reprocessing -- otherwise a user-cancelled reprocess gets silently
treated as complete, the watermark advances, and the wipe becomes
permanently unresolved (the sidecar entry is gone, but the file was never
actually reprocessed).

Unit-level (not a full `smart_index()` reproduction): directly exercises
`SmartIndexer._reprocess_newly_pending_self_heal_paths()` (propagates a
cancelled reprocess pass into the aggregate `ProcessingStats.cancelled`)
and `SmartIndexer._clear_self_heal_reprocess_paths_if_safe()` (must NOT
clear when `stats.cancelled` is True) against a REAL `FilesystemVectorStore`
-backed durable sidecar -- only `process_files_high_throughput` (the
actual embedding/chunking pipeline) is stubbed, since driving a real
mid-batch user cancellation deterministically is not practical at the
unit level and is not what this fix is about (the fix is the aggregation
and gating logic around whatever `cancelled` value that pipeline reports).
"""

from pathlib import Path
from typing import Optional

from code_indexer.config import Config
from code_indexer.indexing.processor import ProcessingStats
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.collection_dedup_repair import (
    record_self_heal_reprocess_pending,
)

VECTOR_DIM = 4


class _NoOpEmbeddingProvider(EmbeddingProvider):
    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ):
        return [0.0] * VECTOR_DIM

    def get_embeddings_batch(self, texts, model=None):
        return [[0.0] * VECTOR_DIM for _ in texts]

    def get_embedding_with_metadata(self, text, model=None):
        return EmbeddingResult(embedding=[0.0] * VECTOR_DIM, model="test")

    def get_embeddings_batch_with_metadata(self, texts, model=None):
        return BatchEmbeddingResult(
            embeddings=[[0.0] * VECTOR_DIM for _ in texts], model="test"
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self):
        return {"dimensions": VECTOR_DIM, "max_tokens": 8192}

    def get_provider_name(self) -> str:
        return "noop-test-provider"

    def get_current_model(self) -> str:
        return "noop-test-model"

    def supports_batch_processing(self) -> bool:
        return True


def _make_indexer(tmp_path: Path) -> SmartIndexer:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = Config(codebase_dir=repo)
    embedding_provider = _NoOpEmbeddingProvider()
    vector_store = FilesystemVectorStore(base_path=repo / ".code-indexer" / "index")
    vector_store.ensure_provider_aware_collection(config, embedding_provider)
    return SmartIndexer(
        config=config,
        embedding_provider=embedding_provider,
        vector_store_client=vector_store,
        metadata_path=tmp_path / "metadata.json",
    )


def test_cancelled_reprocess_pass_propagates_into_stats_cancelled(
    tmp_path, monkeypatch
):
    indexer = _make_indexer(tmp_path)
    collection_name = indexer.vector_store_client.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )
    collection_path = tmp_path / "repo" / ".code-indexer" / "index" / collection_name
    wiped_file = tmp_path / "repo" / "wiped.py"
    wiped_file.write_text("# wiped file content\n")
    record_self_heal_reprocess_pending(collection_path, frozenset({"wiped.py"}))

    cancelled_stats = ProcessingStats()
    cancelled_stats.cancelled = True
    monkeypatch.setattr(
        indexer,
        "process_files_high_throughput",
        lambda **kwargs: cancelled_stats,
    )

    stats = ProcessingStats()
    stats, pending = indexer._reprocess_newly_pending_self_heal_paths(
        collection_name, [], stats, 1, None, None
    )

    assert pending == frozenset({"wiped.py"})
    assert stats.cancelled is True, (
        "a cancelled reprocess pass must propagate into the aggregate "
        "ProcessingStats.cancelled -- otherwise the caller would treat "
        "this run as cleanly completed"
    )


def test_clear_is_skipped_when_stats_cancelled(tmp_path):
    indexer = _make_indexer(tmp_path)
    collection_name = indexer.vector_store_client.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )
    collection_path = tmp_path / "repo" / ".code-indexer" / "index" / collection_name
    record_self_heal_reprocess_pending(collection_path, frozenset({"wiped.py"}))

    cancelled_stats = ProcessingStats()
    cancelled_stats.cancelled = True

    indexer._clear_self_heal_reprocess_paths_if_safe(
        collection_name, frozenset({"wiped.py"}), cancelled_stats
    )

    remaining = indexer.vector_store_client.get_pending_self_heal_reprocess_paths(
        collection_name
    )
    assert remaining == frozenset({"wiped.py"}), (
        "a cancelled run must NOT clear the durable sidecar entry -- the "
        "wipe was never actually confirmed reprocessed"
    )


def test_clear_proceeds_when_stats_not_cancelled(tmp_path):
    indexer = _make_indexer(tmp_path)
    collection_name = indexer.vector_store_client.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )
    collection_path = tmp_path / "repo" / ".code-indexer" / "index" / collection_name
    record_self_heal_reprocess_pending(collection_path, frozenset({"wiped.py"}))

    normal_stats = ProcessingStats()
    normal_stats.cancelled = False

    indexer._clear_self_heal_reprocess_paths_if_safe(
        collection_name, frozenset({"wiped.py"}), normal_stats
    )

    remaining = indexer.vector_store_client.get_pending_self_heal_reprocess_paths(
        collection_name
    )
    assert remaining == frozenset(), (
        "a successfully-completed (not cancelled) run must clear the "
        "durable sidecar entries it consulted"
    )
