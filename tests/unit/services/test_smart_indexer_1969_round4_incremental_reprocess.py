"""Unit test for Bug #1969 Round 4, finding R3-F1 (P2 BLOCKING, the
critical one): the "file is now fully missing from the index, so it gets
reprocessed" safety argument Round 3 relied on is only true for
`cidx index --reconcile`. Default `cidx index` runs
`SmartIndexer._do_incremental_index`, which picks files ONLY from
git-diff-since-last-commit and mtime-modified-since-last-run -- it has no
concept of "this unchanged file lost all its indexed points" and would
never notice on its own.

This is a REAL end-to-end reproduction: a real git repository, a real
`FilesystemVectorStore`, and a real (non-mocked, fully implemented)
deterministic embedding provider -- driven through the actual
`SmartIndexer.smart_index()` production entry point, with NO mocking of
the code under test. Must fail (stable.py unsearchable after the
incremental run) before the fix, pass (stable.py searchable again, same
run) after.

Reproduction shape: index a file (`stable.py`) normally. Simulate the
Bug #1969 pre-existing corruption directly on disk -- duplicate its
ALREADY-INDEXED real vector_*.json record under a sibling shard path
(genuine duplicate point_id, real project_id/file_hash/unique_key the
production pipeline itself assigned) and truncate `id_index.bin` so it
fails to load (the exact bug-report shape). Then run a SECOND, completely
FRESH `SmartIndexer`/`FilesystemVectorStore` pair (sharing only the
on-disk collection + metadata file -- a real new `cidx index` process
never shares in-memory state with the prior one) that indexes ONE
unrelated new file (`trigger.py`). Processing `trigger.py` is what
triggers `_load_id_index(self_heal=True)`'s self-heal, which wipes
`stable.py`'s chunks -- entirely unrelated to this run's own git-diff
file selection.
"""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from code_indexer.config import Config
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 16
BYTE_MAX_VALUE = 255.0
VECTOR_SCALE = 2.0
VECTOR_OFFSET = 1.0
FAKE_MAX_TOKENS = 8192
SEARCH_RESULT_LIMIT = 10


def _deterministic_embedding(text: str) -> List[float]:
    """Real (non-mocked) local embedding function: SHA-256 digest of the
    input text, rescaled into [-1, 1]. Deterministic, no network call."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [
        (digest[i % len(digest)] / BYTE_MAX_VALUE) * VECTOR_SCALE - VECTOR_OFFSET
        for i in range(VECTOR_DIM)
    ]


class DeterministicHashEmbeddingProvider(EmbeddingProvider):
    """Real, fully-working ``EmbeddingProvider`` implementation used in
    place of a mock -- a genuine collaborator dependency, not a test
    double standing in for the class under test."""

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ) -> List[float]:
        return _deterministic_embedding(text)

    def get_embeddings_batch(
        self, texts: List[str], model: Optional[str] = None
    ) -> List[List[float]]:
        return [_deterministic_embedding(t) for t in texts]

    def get_embedding_with_metadata(
        self, text: str, model: Optional[str] = None
    ) -> EmbeddingResult:
        return EmbeddingResult(
            embedding=_deterministic_embedding(text), model=self.get_current_model()
        )

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], model: Optional[str] = None
    ) -> BatchEmbeddingResult:
        return BatchEmbeddingResult(
            embeddings=[_deterministic_embedding(t) for t in texts],
            model=self.get_current_model(),
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, int]:
        return {"dimensions": VECTOR_DIM, "max_tokens": FAKE_MAX_TOKENS}

    def get_provider_name(self) -> str:
        return "deterministic-test-provider"

    def get_current_model(self) -> str:
        return "deterministic-test-model"

    def supports_batch_processing(self) -> bool:
        return True

    def _get_model_token_limit(self) -> int:
        return FAKE_MAX_TOKENS


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _run_git(repo, "config", "user.email", "test@test.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".code-indexer/\n")


def _make_smart_indexer(repo: Path, metadata_path: Path) -> SmartIndexer:
    """Construct a REAL, FRESH SmartIndexer wired to a REAL, FRESH
    FilesystemVectorStore -- no in-memory state shared with any prior
    instance, matching how a real `cidx index` invocation is always a
    brand-new process."""
    config = Config(codebase_dir=repo)
    embedding_provider = DeterministicHashEmbeddingProvider()
    vector_store = FilesystemVectorStore(base_path=repo / ".code-indexer" / "index")
    vector_store.ensure_provider_aware_collection(config, embedding_provider)
    return SmartIndexer(
        config=config,
        embedding_provider=embedding_provider,
        vector_store_client=vector_store,
        metadata_path=metadata_path,
    )


def _duplicate_indexed_record(collection_path: Path, target_rel_path: str) -> None:
    """Find EVERY real, already-indexed vector_*.json record for
    `target_rel_path` and create a genuine on-disk duplicate point_id for
    each -- same "id" (and therefore the exact project_id/file_hash/
    unique_key the REAL production pipeline assigned), just a mutated
    vector under a sibling directory. This reproduces the exact Bug #1969
    collision shape (two vector_*.json files, same point_id, different
    content) without needing to reconstruct the identity scheme by hand.
    """
    for json_file in list(collection_path.rglob("vector_*.json")):
        record = json.loads(json_file.read_text())
        payload = record.get("payload", {})
        if payload.get("path") != target_rel_path:
            continue
        duplicate = dict(record)
        duplicate["vector"] = [-v for v in record["vector"]]
        point_id = record["id"]
        dup_dir = collection_path / "round4_dup_test" / point_id[:4]
        dup_dir.mkdir(parents=True, exist_ok=True)
        (dup_dir / json_file.name).write_text(json.dumps(duplicate))


def _corrupt_id_index_bin(collection_path: Path) -> None:
    (collection_path / "id_index.bin").write_bytes(b"\xff\xff\xff\xff")


def test_incremental_run_reprocesses_self_heal_wiped_file_same_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    stable_content = "# stable file content untouched across runs\n"
    (repo / "stable.py").write_text(stable_content)
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "initial: add stable.py")

    metadata_path = tmp_path / "metadata.json"

    # --- Run 1: real full index (first-ever run). ---
    indexer1 = _make_smart_indexer(repo, metadata_path)
    stats1 = indexer1.smart_index()
    assert stats1.files_processed >= 1, "setup invalid: stable.py was not indexed"

    collection_name = indexer1.vector_store_client.resolve_collection_name(
        indexer1.config, indexer1.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name

    stable_vector = _deterministic_embedding(stable_content)
    baseline_results = indexer1.vector_store_client.search(
        query="unused",
        embedding_provider=indexer1.embedding_provider,
        collection_name=collection_name,
        precomputed_query_vector=stable_vector,
        limit=SEARCH_RESULT_LIMIT,
    )
    baseline_paths = {r["payload"].get("path") for r in baseline_results}
    assert "stable.py" in baseline_paths, (
        "test setup invalid: stable.py not found in baseline HNSW search "
        f"(got paths: {baseline_paths})"
    )

    # --- Simulate the Bug #1969 pre-existing corruption directly on disk:
    # a genuine duplicate point_id for stable.py's REAL indexed record,
    # plus a corrupt/unreadable id_index.bin (the exact bug-report shape:
    # AC30 cannot resolve a per-chunk winner). ---
    _duplicate_indexed_record(collection_path, "stable.py")
    _corrupt_id_index_bin(collection_path)

    # --- Run 2: a completely FRESH SmartIndexer/FilesystemVectorStore
    # pair (no shared in-memory state -- a real new `cidx index` process)
    # indexes ONE unrelated new file. stable.py is untouched (same mtime,
    # same git blob) -- this run's OWN git-diff/mtime file selection would
    # never pick it. Processing trigger.py's upsert is what triggers
    # _load_id_index(self_heal=True)'s self-heal, wiping stable.py's
    # chunks as a side effect. ---
    (repo / "trigger.py").write_text("# unrelated new file that triggers indexing\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "add trigger.py")

    indexer2 = _make_smart_indexer(repo, metadata_path)
    # safety_buffer_seconds=0: smart_index()'s default 60s safety buffer
    # would otherwise make stable.py's mtime look "recently modified"
    # purely because this test runs in well under 60 seconds end to end --
    # masking the exact git-diff/mtime file-selection gap this test must
    # reproduce (a REAL `cidx index` run, minutes or hours after the prior
    # one, would never have this artifact).
    indexer2.smart_index(safety_buffer_seconds=0)

    # REAL HNSW search: stable.py's content must be searchable again in
    # THIS SAME run -- not "eventually", not "only via --reconcile". If
    # this fails, stable.py's content has become silently, permanently
    # unsearchable -- the exact failure mode this whole fix exists to
    # prevent.
    post_run_results = indexer2.vector_store_client.search(
        query="unused",
        embedding_provider=indexer2.embedding_provider,
        collection_name=collection_name,
        precomputed_query_vector=stable_vector,
        limit=SEARCH_RESULT_LIMIT,
    )
    post_run_paths = {r["payload"].get("path") for r in post_run_results}
    assert "stable.py" in post_run_paths, (
        "stable.py's content did not become searchable again in the same "
        "incremental run that self-healed its corrupted duplicate -- it "
        "was wiped by the self-heal but never reprocessed, because this "
        "run's git-diff/mtime file selection never chose it (only "
        "trigger.py was selected). This is the exact silent, permanent "
        f"search-coverage gap R3-F1 exists to prevent. Got paths: "
        f"{post_run_paths}"
    )
