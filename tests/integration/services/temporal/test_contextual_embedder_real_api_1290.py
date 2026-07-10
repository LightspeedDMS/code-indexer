"""Integration test: full per-commit contextual pipeline against REAL VoyageAI.

Story #1290: proves the vertical slice end-to-end with no mocking of the code
under test -- a real git repo, real commit_aggregator + contextual_chunker,
and a real network call to POST /v1/contextualizedembeddings (voyage-context-4).

Skipped when VOYAGE_API_KEY is not set in the environment (e.g. sandboxed CI
without credentials) -- run manually with:
    VOYAGE_API_KEY=<key> PYTHONPATH=./src python3 -m pytest \
        tests/integration/services/temporal/test_contextual_embedder_real_api_1290.py -v
"""

import os
import subprocess
from pathlib import Path

import pytest

from src.code_indexer.config import Config
from src.code_indexer.services.temporal.commit_aggregator import (
    build_aggregated_document,
    get_file_changes,
)
from src.code_indexer.services.temporal.contextual_chunker import (
    chunk_aggregated_document,
)
from src.code_indexer.services.temporal.embedders.contextual import (
    ContextualTemporalEmbedder,
)
from src.code_indexer.services.temporal.models import CommitInfo

pytestmark = pytest.mark.skipif(
    not os.environ.get("VOYAGE_API_KEY"),
    reason="VOYAGE_API_KEY not set -- real-API integration test skipped",
)

_GIT_ENV_EXTRA = {
    "GIT_AUTHOR_NAME": "Test User",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test User",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(repo_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV_EXTRA},
    )


def _commit_info_for(repo_path: Path, commit_hash: str) -> CommitInfo:
    fmt = _git(
        repo_path, "log", "-1", "--format=%at%x00%an%x00%ae%x00%B%x00%P", commit_hash
    ).stdout
    parts = fmt.split("\x00")
    return CommitInfo(
        hash=commit_hash,
        timestamp=int(parts[0]),
        author_name=parts[1],
        author_email=parts[2],
        message=parts[3].strip(),
        parent_hashes=parts[4].strip(),
    )


@pytest.fixture
def multi_commit_repo(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(repo_path, "init", "-q")
    _git(repo_path, "config", "user.name", "Test User")
    _git(repo_path, "config", "user.email", "test@example.com")

    # Root commit: file large enough that a later small edit stays above
    # git's 50% rename-similarity threshold after the rename below.
    original_lines = [f"    step_{i}()\n" for i in range(60)]
    (repo_path / "auth.py").write_text(
        "def login(user, password):\n" + "".join(original_lines)
    )
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-q", "-m", "Initial commit: add login function")
    root_hash = _git(repo_path, "rev-parse", "HEAD").stdout.strip()

    # Normal commit: single-parent content modification.
    (repo_path / "auth.py").write_text(
        "def login(user, password):\n"
        "    if not user or not password:\n"
        "        raise ValueError('missing credentials')\n" + "".join(original_lines)
    )
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-q", "-m", "Validate credentials before checking them")
    normal_hash = _git(repo_path, "rev-parse", "HEAD").stdout.strip()

    # Rename-with-content-changes commit.
    _git(repo_path, "mv", "auth.py", "authentication.py")
    modified_lines = list(original_lines)
    modified_lines[10] = "    permission_check()\n"
    (repo_path / "authentication.py").write_text(
        "def login(user, password):\n"
        "    if not user or not password:\n"
        "        raise ValueError('missing credentials')\n" + "".join(modified_lines)
    )
    _git(repo_path, "add", ".")
    _git(
        repo_path,
        "commit",
        "-q",
        "-m",
        "Rename to authentication.py and add permission check",
    )
    rename_hash = _git(repo_path, "rev-parse", "HEAD").stdout.strip()

    return {
        "path": repo_path,
        "root_hash": root_hash,
        "normal_hash": normal_hash,
        "rename_hash": rename_hash,
    }


class TestRealContextualEmbedderPipeline:
    def test_root_commit_embeds_via_real_voyage_context_4(self, multi_commit_repo):
        repo_path = multi_commit_repo["path"]
        commit = _commit_info_for(repo_path, multi_commit_repo["root_hash"])

        changes = get_file_changes(repo_path, commit)
        doc = build_aggregated_document(commit, changes)
        chunks = chunk_aggregated_document(doc, chunk_chars=4096)
        assert len(chunks) == 1  # small commit -> single chunk

        embedder = ContextualTemporalEmbedder(Config())
        embeddings = embedder.embed_commit_chunks([c.text for c in chunks])

        assert len(embeddings) == len(chunks)
        assert len(embeddings[0]) == 1024
        assert all(isinstance(v, float) for v in embeddings[0])

    def test_normal_commit_embeds_via_real_voyage_context_4(self, multi_commit_repo):
        repo_path = multi_commit_repo["path"]
        commit = _commit_info_for(repo_path, multi_commit_repo["normal_hash"])

        changes = get_file_changes(repo_path, commit)
        doc = build_aggregated_document(commit, changes)
        chunks = chunk_aggregated_document(doc, chunk_chars=4096)

        embedder = ContextualTemporalEmbedder(Config())
        embeddings = embedder.embed_commit_chunks([c.text for c in chunks])

        assert len(embeddings) == len(chunks)
        for emb in embeddings:
            assert len(emb) == 1024

    def test_rename_with_changes_embeds_via_real_voyage_context_4(
        self, multi_commit_repo
    ):
        """AC24 end-to-end: rename-with-changes is not reduced to a head-only chunk."""
        repo_path = multi_commit_repo["path"]
        commit = _commit_info_for(repo_path, multi_commit_repo["rename_hash"])

        changes = get_file_changes(repo_path, commit)
        doc = build_aggregated_document(commit, changes)
        assert doc.file_paths == ["authentication.py"]
        assert "--- authentication.py ---" in doc.text

        chunks = chunk_aggregated_document(doc, chunk_chars=4096)
        assert len(chunks) >= 1
        # Not head-only: some chunk overlaps the renamed file's diff content.
        assert any(c.paths for c in chunks)

        embedder = ContextualTemporalEmbedder(Config())
        embeddings = embedder.embed_commit_chunks([c.text for c in chunks])

        assert len(embeddings) == len(chunks)
        for emb in embeddings:
            assert len(emb) == 1024

    def test_query_embedding_returns_1024_dim_vector(self):
        embedder = ContextualTemporalEmbedder(Config())
        vector = embedder.embed_query("validate credentials before login")
        assert len(vector) == 1024
        assert all(isinstance(v, float) for v in vector)
