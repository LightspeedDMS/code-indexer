"""CRITICAL #1 (2026-07-23 code review, Codex): the vector_store instance
that actually PERFORMS the temporal search was never given the
TemporalShardResolver -- only a separately-constructed `resolver` object
was threaded into `execute_temporal_query_with_fusion` for discovery/pin
bookkeeping. `_get_collection_path()` gates resolution on
`self._temporal_shard_resolver` being set on the STORE INSTANCE, so the
disconnected store silently fell back to `base_path / collection_name`
(the legacy in-repo path) even after AC1 relocated the data.

This test is deliberately built to NOT be able to lie the same way the
prior "E2E" fusion test did (that test replaced `_query_single_provider`
and used a `MagicMock` vector store, so it could never have caught this).
Here:
  - A REAL SemanticQueryManager._execute_temporal_query call, through a
    REAL ConfigManager-backed Config, REAL BackendFactory-constructed
    FilesystemVectorStore, REAL HNSW build/search, REAL AliasManager.
  - The ONLY thing stubbed is the embedding-provider API boundary
    (_create_embedding_provider_for_collection) -- the external heavy
    collaborator this project's mocking hierarchy allows stubbing.
  - The legacy in-repo shard directory is DELETED before the query runs,
    so a pass can ONLY happen if the read genuinely came from the sister
    version, not a leftover local file.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from code_indexer.config import Config, ConfigManager, TemporalConfig, VoyageAIConfig
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.query.semantic_query_manager import SemanticQueryManager
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.services.temporal.temporal_relocation_trigger import (
    maybe_relocate_shard_to_sister_location,
)
from code_indexer.server.storage.postgres.temporal_child_wiring import (
    CIDX_SERVER_REFRESH_CONTEXT_ENV,
)

KNOWN_VECTOR = [0.11, 0.22, 0.33, 0.44]


class _FakeQueryEmbeddingProvider(EmbeddingProvider):
    """Minimal EmbeddingProvider stub -- the ONLY external collaborator
    (embedding API) this test stubs. Returns the SAME vector the document
    row was indexed with, so real HNSW cosine search finds an exact match.
    """

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ) -> List[float]:
        return list(KNOWN_VECTOR)

    def get_embeddings_batch(
        self, texts: List[str], model: Optional[str] = None
    ) -> List[List[float]]:
        return [list(KNOWN_VECTOR) for _ in texts]

    def get_embedding_with_metadata(
        self, text: str, model: Optional[str] = None
    ) -> EmbeddingResult:
        return EmbeddingResult(
            embedding=list(KNOWN_VECTOR), model="fake", provider="fake"
        )

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], model: Optional[str] = None
    ) -> BatchEmbeddingResult:
        return BatchEmbeddingResult(
            embeddings=[list(KNOWN_VECTOR) for _ in texts],
            model="fake",
            provider="fake",
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {"dimensions": len(KNOWN_VECTOR)}

    def get_provider_name(self) -> str:
        return "fake-provider"

    def get_current_model(self) -> str:
        return "voyage-code-3"

    def supports_batch_processing(self) -> bool:
        return False


def _write_local_shard_row(
    shard_dir: Path, hash_prefix: str, point_id: str, commit_hash: str
):
    shard_dir.mkdir(parents=True, exist_ok=True)
    # Realistic per-commit-aggregated row (matches build_chunk_payload's
    # exact shape, temporal_point_builder.py:50-67, plus the top-level
    # chunk_text sibling field temporal_indexer.py:1138-1145 writes) --
    # a minimal payload silently fails TemporalSearchService's
    # fail-fast chunk_text contract and its commit_timestamp time-range
    # filter, which a bare store.search() call (used by earlier-round
    # tests) never exercises.
    row = {
        "id": point_id,
        "vector": KNOWN_VECTOR,
        "chunk_text": "def authenticate(user): ...",
        "payload": {
            "type": "commit_chunk",
            "is_head": True,
            "commit_hash": commit_hash,
            "commit_timestamp": 1700000000,
            "commit_date": "2023-11-14",
            "author_name": "Test Author",
            "author_email": "test@example.com",
            "paths": ["src/auth.py"],
            "primary_path": "src/auth.py",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": 30,
            "project_id": "proj",
            "commit_message": "Add authenticate()",
        },
    }
    (shard_dir / f"vector_{hash_prefix}.json").write_text(json.dumps(row))


def _write_real_config(codebase_dir: Path) -> None:
    config = Config(codebase_dir=codebase_dir)
    config.embedding_provider = "voyage-ai"
    config.voyage_ai = VoyageAIConfig(model="voyage-code-3")
    config.temporal = TemporalConfig(
        embedders=["voyage-code-3"],
        active_embedder="voyage-code-3",
    )
    config_dir = codebase_dir / ".code-indexer"
    config_dir.mkdir(parents=True, exist_ok=True)
    ConfigManager(config_dir / "config.json").save(config)


def test_query_reads_sister_only_data_after_legacy_shard_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    monkeypatch.setenv("CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED", "1")

    golden_repos_dir = tmp_path / "golden-repos"
    codebase_dir = golden_repos_dir / "evolution"
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
    local_shard_dir = codebase_dir / ".code-indexer" / "index" / shard_name

    _write_real_config(codebase_dir)
    _write_local_shard_row(local_shard_dir, "aaaa1111", "proj:commit:c0:0", "c0")

    # AC1's real relocation trigger publishes this shard's data to the
    # sister location, exactly as it does during ordinary server refresh.
    maybe_relocate_shard_to_sister_location(
        codebase_dir=codebase_dir,
        shard_name=shard_name,
        local_shard_dir=local_shard_dir,
        new_commit_hashes=["c0"],
        vector_dim=len(KNOWN_VECTOR),
    )

    # Prove the read is NOT coming from a leftover local file: delete the
    # entire legacy in-repo index tree.
    shutil.rmtree(codebase_dir / ".code-indexer" / "index")

    activated_repos_dir = tmp_path / "activated-repos"
    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    manager.query_tracker = QueryTracker()
    manager.activated_repo_manager = MagicMock()
    manager.activated_repo_manager.activated_repos_dir = str(activated_repos_dir)

    with patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch"
        "._create_embedding_provider_for_collection",
        return_value=_FakeQueryEmbeddingProvider(),
    ):
        results = manager._execute_temporal_query(
            repo_path=codebase_dir,
            repository_alias="evolution",
            query_text="authenticate user",
            limit=5,
            min_score=None,
            time_range=None,
            time_range_all=True,
            golden_repo_alias="evolution",
        )

    assert len(results) == 1, (
        f"expected the relocated row back from the sister-only location "
        f"(legacy shard was deleted before the query ran), got {results}"
    )
    assert results[0].metadata["commit_hash"] == "c0"
