"""
Unit tests for Bug #1496: MultiIndexQueryService must not silently fall back
to other-collection results when the PRIMARY code collection exists on disk
but its vector-store query fails to load (e.g. HNSW index missing/corrupt).

Distinction under test:
1. Collection genuinely ABSENT (never indexed) -> skipping it and returning
   the other collection's results is CORRECT, pre-existing behavior. This
   must keep working (guard test).
2. Collection PRESENT but its index fails to LOAD (LocalIndexNotFoundError
   raised by the real FilesystemVectorStore.search()) -> this is a real
   failure and MUST surface loudly, never be swallowed into a WARNING log
   plus a silently-partial "success" result (Messi #2 Anti-Fallback, #13
   Anti-Silent-Failure).
"""

import json

import pytest
from unittest.mock import Mock

from code_indexer.services.multi_index_query_service import MultiIndexQueryService
from code_indexer.config import VOYAGE_MULTIMODAL_MODEL
from code_indexer.storage.filesystem_vector_store import LocalIndexNotFoundError


def _create_code_collection_dir(project_dir):
    """Create an on-disk code_index collection directory with a real-shaped
    collection_meta.json, proving the code collection genuinely EXISTS on
    disk in every scenario these tests exercise (only the multimodal
    collection's presence/absence varies between the two test classes)."""
    code_dir = project_dir / ".code-indexer" / "index" / "code_index"
    code_dir.mkdir(parents=True)
    (code_dir / "collection_meta.json").write_text(json.dumps({"vector_size": 1024}))
    return code_dir


@pytest.fixture
def project_root_with_multimodal(tmp_path):
    """Project root where BOTH the code collection and a multimodal
    collection genuinely EXIST on disk."""
    project_dir = tmp_path / "test_project_with_multimodal"
    project_dir.mkdir()
    index_dir = project_dir / ".code-indexer" / "index"
    index_dir.mkdir(parents=True)
    _create_code_collection_dir(project_dir)
    multimodal_dir = index_dir / VOYAGE_MULTIMODAL_MODEL
    multimodal_dir.mkdir(parents=True)
    return project_dir


@pytest.fixture
def project_root_without_multimodal(tmp_path):
    """Project root where the code collection EXISTS on disk but NO
    multimodal collection was ever indexed (genuinely absent)."""
    project_dir = tmp_path / "test_project_no_multimodal"
    project_dir.mkdir()
    index_dir = project_dir / ".code-indexer" / "index"
    index_dir.mkdir(parents=True)
    _create_code_collection_dir(project_dir)
    return project_dir


@pytest.fixture
def mock_embedding_provider():
    provider = Mock()
    provider.embed_query = Mock(return_value=[0.0])
    return provider


class TestBug1496PresentButBrokenCodeIndexFailsLoud:
    """Discriminating RED: a present-but-unloadable code collection must
    fail loud, not be silently masked by a working multimodal collection."""

    def test_present_but_broken_code_index_fails_loud_not_silent_fallback(
        self, project_root_with_multimodal, mock_embedding_provider
    ):
        """Code collection EXISTS on disk but search() raises the real
        LocalIndexNotFoundError (HNSW index failed to load), while the
        multimodal collection returns real results. The query MUST raise
        loud instead of returning a merged/partial result set drawn only
        from the multimodal collection.

        Before the fix: the broad `except Exception` at the aggregation
        loop logs a WARNING and treats this as a mere timeout, continuing
        to merge in the multimodal-only results with no exception raised
        -- exactly the silent fallback Bug #1496 reports.
        """

        def fake_search(**kwargs):
            collection_name = kwargs["collection_name"]
            if collection_name == "code_index":
                raise LocalIndexNotFoundError(
                    f"HNSW index not found for collection '{collection_name}'. "
                    f"Run: cidx index --rebuild-index"
                )
            # Multimodal collection query succeeds with real results.
            return (
                [
                    {
                        "score": 0.91,
                        "payload": {"path": "docs/guide.md", "chunk_offset": 0},
                        "text": "unrelated multimodal match",
                    }
                ],
                {"elapsed_ms": 2.0},
            )

        mock_vector_store = Mock()
        mock_vector_store.search = Mock(side_effect=fake_search)

        service = MultiIndexQueryService(
            project_root=project_root_with_multimodal,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider,
        )

        with pytest.raises(LocalIndexNotFoundError):
            service.query(
                query_text="authentication logic",
                limit=5,
                collection_name="code_index",
            )


class TestBug1496GenuinelyAbsentCollectionStillSkipped:
    """Guard: a genuinely-absent collection must still be silently skipped
    (legitimate partial querying), proving the fix did not over-correct."""

    def test_genuinely_absent_multimodal_collection_still_skipped_normally(
        self, project_root_without_multimodal, mock_embedding_provider
    ):
        """Code collection EXISTS and its query succeeds; no multimodal
        collection was ever indexed (directory absent). The code
        collection's real results must still be returned normally, with
        no exception raised and no multimodal query even attempted.

        Must pass BOTH before and after the fix -- this is the legitimate
        absent-collection skip behavior Bug #1496 explicitly says must
        keep working.
        """
        expected_code_results = [
            {
                "score": 0.95,
                "payload": {"path": "src/auth.py", "chunk_offset": 10},
                "text": "def authenticate(user): ...",
            }
        ]

        def fake_search(**kwargs):
            collection_name = kwargs["collection_name"]
            assert collection_name == "code_index", (
                "multimodal collection must never be queried when genuinely absent"
            )
            return (list(expected_code_results), {"elapsed_ms": 1.5})

        mock_vector_store = Mock()
        mock_vector_store.search = Mock(side_effect=fake_search)

        service = MultiIndexQueryService(
            project_root=project_root_without_multimodal,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider,
        )

        results, timing = service.query(
            query_text="authentication logic",
            limit=5,
            collection_name="code_index",
        )

        assert timing["has_multimodal"] is False
        assert len(results) == 1
        assert results[0]["payload"]["path"] == "src/auth.py"
