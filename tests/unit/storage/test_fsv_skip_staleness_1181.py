"""Tests for Bug #1181 Perf Fix #3: skip_staleness_check for immutable versioned snapshots.

Observable-behavior approach: rather than spying on _compute_file_hash (a private method
on the SUT), tests verify the EFFECT of skipping staleness: when the flag is True, even a
modified file is returned as NOT stale with current-file content (not git blob). When False,
the modification is detected and git blob content is served with is_stale=True.

This proves the hash computation path is bypassed without touching SUT internals.
"""

import subprocess
from typing import Any

import numpy as np
import pytest
from unittest.mock import Mock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_repo(tmp_path):
    """Create a git repo with one committed Python file; yield (repo_path, content)."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    original_content = "def foo():\n    return 42\n"
    test_file = tmp_path / "test.py"
    test_file.write_text(original_content)
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True
    )
    return tmp_path, original_content, test_file


@pytest.fixture()
def indexed_git_store(git_repo):
    """Return (store_default_flag, original_content) with one indexed point."""
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    repo_path, original_content, _test_file = git_repo
    store = FilesystemVectorStore(base_path=repo_path, project_root=repo_path)
    store.create_collection("test_coll", vector_size=1536)
    points = [
        {
            "id": "test_001",
            "vector": np.random.randn(1536).tolist(),
            "payload": {
                "path": "test.py",
                "line_start": 1,
                "line_end": 2,
                "content": original_content,
            },
        }
    ]
    store.begin_indexing("test_coll")
    store.upsert_points("test_coll", points)
    store.end_indexing("test_coll")
    return store, original_content


def _search(store: Any, content: str = "test") -> Any:
    """Run a search on test_coll and return results."""
    mock_provider = Mock()
    mock_provider.get_embedding.return_value = np.random.randn(1536).tolist()
    return store.search(
        query=content,
        embedding_provider=mock_provider,
        collection_name="test_coll",
        limit=1,
    )


# ---------------------------------------------------------------------------
# Flag attribute tests
# ---------------------------------------------------------------------------


class TestSkipStalenessCheckAttribute:
    """skip_staleness_check attribute: defaults and constructor kwarg."""

    def test_default_is_false(self, tmp_path):
        """Default value is False so CLI behavior is byte-identical."""
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path, project_root=tmp_path)
        assert store.skip_staleness_check is False

    def test_constructor_kwarg_sets_true(self, tmp_path):
        """Constructor kwarg skip_staleness_check=True is accepted and stored."""
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(
            base_path=tmp_path, project_root=tmp_path, skip_staleness_check=True
        )
        assert store.skip_staleness_check is True

    def test_attribute_settable_after_construction(self, tmp_path):
        """The attribute can be set post-construction (server wiring pattern)."""
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(base_path=tmp_path, project_root=tmp_path)
        store.skip_staleness_check = True
        assert store.skip_staleness_check is True


# ---------------------------------------------------------------------------
# Behavioral tests: observable effect of the flag
# ---------------------------------------------------------------------------


class TestSkipStalenessObservableBehavior:
    """Observable effects: what results look like when flag is True vs False."""

    def test_skip_true_modified_file_returns_current_content_not_stale(
        self, git_repo, indexed_git_store
    ):
        """GIVEN file modified after indexing AND skip_staleness_check=True
        WHEN search() is called
        THEN result is NOT stale and content is from current file (not git blob).

        This proves _compute_file_hash was skipped: if it ran, hash mismatch would
        return git blob (original) content with is_stale=True. Getting current
        content with is_stale=False proves the hash path was bypassed.
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        repo_path, original_content, test_file = git_repo
        # Re-create store with skip=True over the already-indexed collection
        store = FilesystemVectorStore(
            base_path=repo_path,
            project_root=repo_path,
            skip_staleness_check=True,
        )
        # Copy the collection from indexed_git_store fixture (same base_path)
        _ = indexed_git_store  # ensure indexing happened

        modified_content = "def foo():\n    return 99\n"
        test_file.write_text(modified_content)

        results = _search(store)

        assert len(results) == 1
        staleness = results[0]["staleness"]
        # Not stale — hash was never computed
        assert staleness["is_stale"] is False
        assert staleness["staleness_indicator"] is None
        assert staleness["hash_mismatch"] is False
        # Content is from current (modified) file, not the original git blob
        assert results[0]["payload"]["content"] == modified_content

    def test_skip_false_modified_file_detects_staleness(
        self, git_repo, indexed_git_store
    ):
        """GIVEN file modified after indexing AND skip_staleness_check=False (default)
        WHEN search() is called
        THEN staleness IS detected and git blob (original) content is served.

        Regression guard: mutable paths must still run full staleness detection.
        """
        _store, original_content = indexed_git_store
        _repo_path, _orig, test_file = git_repo

        test_file.write_text("def foo():\n    return 99\n")

        results = _search(_store)

        assert len(results) == 1
        staleness = results[0]["staleness"]
        assert staleness["is_stale"] is True
        assert staleness["staleness_indicator"] == "⚠️ Modified"
        assert staleness["hash_mismatch"] is True
        # Content is from git blob (original), not the modified file
        assert results[0]["payload"]["content"] == original_content

    def test_skip_true_unchanged_file_returns_fresh_content(
        self, git_repo, indexed_git_store
    ):
        """GIVEN unchanged file AND skip_staleness_check=True
        WHEN search() is called
        THEN content is current and not stale (same as skip=False for unchanged).
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        repo_path, original_content, _test_file = git_repo
        _ = indexed_git_store

        store = FilesystemVectorStore(
            base_path=repo_path,
            project_root=repo_path,
            skip_staleness_check=True,
        )

        results = _search(store)

        assert len(results) == 1
        assert results[0]["staleness"]["is_stale"] is False
        assert results[0]["payload"]["content"] == original_content

    def test_skip_true_deleted_file_still_uses_git_blob(
        self, git_repo, indexed_git_store
    ):
        """GIVEN file deleted after indexing AND skip_staleness_check=True
        WHEN search() is called
        THEN file-deleted path still fires (skip only applies to the file-exists branch).
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        repo_path, original_content, test_file = git_repo
        _ = indexed_git_store

        store = FilesystemVectorStore(
            base_path=repo_path,
            project_root=repo_path,
            skip_staleness_check=True,
        )
        test_file.unlink()

        results = _search(store)

        assert len(results) == 1
        staleness = results[0]["staleness"]
        # File is deleted — the deleted-branch runs regardless of the flag
        assert staleness["is_stale"] is True
        assert staleness["staleness_indicator"] == "🗑️ Deleted"

    def test_skip_true_non_git_result_unaffected(self, tmp_path):
        """GIVEN non-git result (chunk_text key) AND skip_staleness_check=True
        WHEN _get_chunk_content_with_staleness() is called
        THEN returns early (is_stale=False) — flag has no effect on non-git path.
        """
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        store = FilesystemVectorStore(
            base_path=tmp_path, project_root=tmp_path, skip_staleness_check=True
        )
        vector_data = {
            "chunk_text": "some content",
            "payload": {"path": "foo.py"},
        }

        result_content, staleness = store._get_chunk_content_with_staleness(vector_data)

        assert result_content == "some content"
        assert staleness["is_stale"] is False


# ---------------------------------------------------------------------------
# Server wiring tests
# ---------------------------------------------------------------------------


class TestServerWiringSkipStaleness:
    """FilesystemBackend.get_vector_store_client() sets flag based on immutability predicate.

    The guard `if self.hnsw_index_cache is not None:` gates server-mode imports to keep
    the CLI startup path free of server modules. Tests must pass a mock hnsw_index_cache
    to trigger server mode and exercise the skip_staleness wiring.
    """

    def test_immutable_versioned_path_sets_flag_true(self, tmp_path):
        """GIVEN FilesystemBackend (server mode) with a canonical .versioned/{alias}/v_{ts}
        project_root WHEN get_vector_store_client() is called
        THEN the returned FSV has skip_staleness_check=True.
        """
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        versioned_root = tmp_path / ".versioned" / "alias1" / "v_20250101"
        versioned_root.mkdir(parents=True)

        # Pass a non-None hnsw_index_cache to activate server mode in the backend.
        # The value only needs to be truthy — the actual cache object is not used here.
        backend = FilesystemBackend(
            project_root=versioned_root, hnsw_index_cache=Mock()
        )
        fsv = backend.get_vector_store_client()

        assert fsv.skip_staleness_check is True

    def test_mutable_base_clone_path_leaves_flag_false(self, tmp_path):
        """GIVEN FilesystemBackend (server mode) with a mutable base-clone project_root
        WHEN get_vector_store_client() is called
        THEN the returned FSV has skip_staleness_check=False.
        """
        from code_indexer.backends.filesystem_backend import FilesystemBackend

        mutable_root = tmp_path / "golden_repos" / "my-repo"
        mutable_root.mkdir(parents=True)

        backend = FilesystemBackend(project_root=mutable_root, hnsw_index_cache=Mock())
        fsv = backend.get_vector_store_client()

        assert fsv.skip_staleness_check is False

    @pytest.mark.parametrize(
        "path_suffix, expected_immutable",
        [
            (".versioned/my-repo/v_12345678", True),
            (".versioned/my-repo/v_99999999/.code-indexer/config.json", True),
            ("golden_repos/my-repo", False),
            ("golden_repos/activated-repos/my-repo", False),
            ("", False),
        ],
    )
    def test_is_immutable_predicate_covers_canonical_cases(
        self, tmp_path, path_suffix, expected_immutable
    ):
        """is_immutable_versioned_snapshot recognizes canonical patterns correctly."""
        from code_indexer.server.services.query_path_cache import (
            is_immutable_versioned_snapshot,
        )

        if path_suffix:
            path = str(tmp_path / path_suffix)
        else:
            path = ""

        assert is_immutable_versioned_snapshot(path) == expected_immutable
