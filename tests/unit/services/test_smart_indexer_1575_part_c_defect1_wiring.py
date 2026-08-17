"""TDD test for Bug #1575 Part C review fix (Defect 1, dual-review
corroborated): SmartIndexer.process_files_incrementally's REAL production
call chain must not orphan branch-isolation context on a session that an
earlier, already-finalized end_indexing() call discarded.

Pre-fix, process_files_incrementally called
process_branch_changes_high_throughput(skip_branch_isolation=True) with no
way to defer that method's own unconditional finalization. Its `finally`
block called end_indexing() BEFORE hide_files_not_in_branch_thread_safe
(called afterward) ever registered branch context via
set_hnsw_branch_context() -- so the context landed on an already-finalized,
orphaned session nothing would ever consume, and the just-finished
end_indexing() rebuilt the HNSW graph UNFILTERED. A point that should be
hidden on a branch switch remains a ghost vector, reachable via a real HNSW
search.

This is a REAL end-to-end reproduction: a real git repository with two
branches, a real `FilesystemVectorStore`, and a real (non-mocked, fully
implemented) deterministic embedding provider -- driven through the actual
`SmartIndexer.process_files_incrementally()` production entry point, with
NO mocking of the code under test. Must fail (ghost vector found) before
the fix, pass (ghost vector excluded) after.
"""

import hashlib
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
    input text, rescaled into [-1, 1]. Deterministic -- the SAME text
    always produces the SAME vector -- and, with overwhelming probability,
    different texts produce different vectors. No network call.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [
        (digest[i % len(digest)] / BYTE_MAX_VALUE) * VECTOR_SCALE - VECTOR_OFFSET
        for i in range(VECTOR_DIM)
    ]


class DeterministicHashEmbeddingProvider(EmbeddingProvider):
    """Real, fully-working ``EmbeddingProvider`` implementation used in
    place of a mock. ``EmbeddingProvider`` is an ABC with 9 abstract
    methods (see ``services/embedding_provider.py``); every method
    implemented on this class (across this file's staged edits) is a
    genuine, required implementation of that contract -- this class IS a
    real collaborator dependency (analogous to a real VoyageAI/Cohere
    client), not a test double standing in for the class under test. All
    embedding computation delegates to the shared, real
    ``_deterministic_embedding`` function above.
    """

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
        """Duck-typed extra (not part of the ``EmbeddingProvider`` ABC, but
        implemented by every real provider e.g. ``VoyageAIClient``,
        ``CohereEmbeddingProvider``) that ``FileChunkingManager`` calls
        directly on the embedding provider during real chunking.
        """
        return FAKE_MAX_TOKENS


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a real git subcommand against ``repo`` (real subprocess, no
    mocking) -- used to build a real two-branch repository below.
    """
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _init_repo(repo: Path) -> str:
    """Initialize a real git repo with one committed base file, returning
    the initial commit SHA (used later to branch a clean "new-branch" from
    BEFORE old_only.py ever existed).
    """
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _run_git(repo, "config", "user.email", "test@test.com")
    _run_git(repo, "config", "user.name", "Test")
    # Real cidx-managed repos always gitignore .code-indexer/ (the vector
    # store's own index directory) -- without this, a later `git add .`
    # would accidentally stage collection_meta.json etc., and a subsequent
    # branch checkout would fail with "local changes would be overwritten".
    (repo / ".gitignore").write_text(".code-indexer/\n")
    (repo / "base.py").write_text("# base file present on every branch\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "initial")
    return _run_git(repo, "rev-parse", "HEAD").stdout.strip()


def _make_smart_indexer(repo: Path, metadata_path: Path) -> SmartIndexer:
    """Construct a REAL SmartIndexer wired to a REAL FilesystemVectorStore
    and the real DeterministicHashEmbeddingProvider defined above -- no
    mocking of the code under test or its storage/embedding collaborators.
    """
    config = Config(codebase_dir=str(repo))
    embedding_provider = DeterministicHashEmbeddingProvider()
    vector_store = FilesystemVectorStore(base_path=repo / ".code-indexer" / "index")
    vector_store.ensure_provider_aware_collection(config, embedding_provider)
    return SmartIndexer(
        config=config,
        embedding_provider=embedding_provider,
        vector_store_client=vector_store,
        metadata_path=metadata_path,
    )


def test_process_files_incrementally_branch_switch_hides_old_branch_ghost_vector(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    initial_commit = _init_repo(repo)

    metadata_path = tmp_path / "metadata.json"
    indexer = _make_smart_indexer(repo, metadata_path)

    # --- On "old-branch": index a file that exists ONLY on this branch. ---
    _run_git(repo, "checkout", "-b", "old-branch")
    old_only_content = "# old_only real content unique to old-branch\n"
    (repo / "old_only.py").write_text(old_only_content)
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "add old_only.py")

    indexer.process_files_incrementally(file_paths=["old_only.py"])

    collection_name = indexer.vector_store_client.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )

    old_only_vector = _deterministic_embedding(old_only_content)

    # Sanity: the real HNSW graph really contains old_only.py's content
    # before the branch switch.
    baseline_results = indexer.vector_store_client.search(
        query="unused",
        embedding_provider=indexer.embedding_provider,
        collection_name=collection_name,
        precomputed_query_vector=old_only_vector,
        limit=SEARCH_RESULT_LIMIT,
    )
    baseline_paths = {r["payload"].get("path") for r in baseline_results}
    assert "old_only.py" in baseline_paths, (
        "test setup invalid: old_only.py not found in baseline HNSW search "
        f"(got paths: {baseline_paths})"
    )

    # --- Switch to "new-branch", created from the ORIGINAL commit (before
    # old_only.py ever existed) -- a real `git checkout` removes
    # old_only.py from disk since it is absent from new-branch's history. ---
    _run_git(repo, "checkout", "-b", "new-branch", initial_commit)
    assert not (repo / "old_only.py").exists(), (
        "test setup invalid: old_only.py should not exist on new-branch "
        "after a real git checkout"
    )
    new_only_content = "# new_only real content unique to new-branch\n"
    (repo / "new_only.py").write_text(new_only_content)
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "add new_only.py")

    indexer.process_files_incrementally(file_paths=["new_only.py"])

    # REAL HNSW search: old_only.py's content must no longer be reachable
    # once the branch switch's isolation cycle has hidden it -- proving the
    # branch-isolation context registered by
    # hide_files_not_in_branch_thread_safe() was actually consumed by the
    # SAME refresh's finalization, not orphaned on a session an earlier
    # end_indexing() call already discarded.
    post_switch_results = indexer.vector_store_client.search(
        query="unused",
        embedding_provider=indexer.embedding_provider,
        collection_name=collection_name,
        precomputed_query_vector=old_only_vector,
        limit=SEARCH_RESULT_LIMIT,
    )
    post_switch_paths = {r["payload"].get("path") for r in post_switch_results}
    assert "old_only.py" not in post_switch_paths, (
        "ghost vector: old_only.py's content is still reachable via a real "
        "HNSW search after switching to new-branch (which never contained "
        f"it) -- got paths: {post_switch_paths}"
    )
