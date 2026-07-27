"""
Unit tests for MultiIndexQueryService.query_with_separate_kwargs (Bug #1480).

Bug #1480: the server front door (REST /api/query and MCP search_code) never
queried multimodal collections — only the CLI did, via MultiIndexQueryService.
Fixing this server-side requires the two collection queries (code vs
multimodal) to receive INDEPENDENT extra kwargs: the code-collection call must
keep the caller-supplied no_embedding_cache_shortcut value unchanged (Story
#1108 S4), while the multimodal-collection call must ALWAYS force
no_embedding_cache_shortcut=True (embedding-cache isolation — see CLAUDE.md
Bug #1480 section). The existing single-kwargs-dict `.query()` method forwards
the SAME kwargs to both calls, so it cannot express this. This test proves the
new `query_with_separate_kwargs()` method dispatches distinct kwargs per
collection while remaining behavior-preserving for merge/collection-detection.

Mocking pattern mirrors test_multi_index_query_service.py exactly (Mock()
vector_store/embedding_provider, not real infra — this is a unit test of the
dispatch mechanism, not an integration test).
"""

from unittest.mock import Mock

from code_indexer.services.multi_index_query_service import MultiIndexQueryService


def _make_project_root(tmp_path):
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    (project_dir / ".code-indexer").mkdir()
    return project_dir


def _make_multimodal_collection(project_root):
    multimodal_collection = (
        project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
    )
    multimodal_collection.mkdir(parents=True)
    (multimodal_collection / "collection_meta.json").write_text(
        '{"name": "voyage-multimodal-3"}'
    )


class TestQueryWithSeparateKwargs:
    """Test MultiIndexQueryService.query_with_separate_kwargs dispatch."""

    def test_separate_kwargs_dispatched_per_collection(self, tmp_path):
        """code_kwargs and multimodal_kwargs must reach their respective
        vector_store.search() calls independently — proving the two
        collection queries can carry genuinely different extra parameters
        (e.g. a cache-bypass flag that differs between code and multimodal).
        """
        project_root = _make_project_root(tmp_path)
        _make_multimodal_collection(project_root)

        mock_vector_store = Mock()
        mock_embedding_provider = Mock()

        captured_calls = []

        def search_side_effect(*args, **kwargs):
            captured_calls.append(kwargs)
            return ([], {})

        mock_vector_store.search.side_effect = search_side_effect

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider,
        )

        results, timing = service.query_with_separate_kwargs(
            query_text="test query",
            limit=10,
            collection_name="code_index",
            code_kwargs={"no_embedding_cache_shortcut": False, "ef": 50},
            multimodal_kwargs={"no_embedding_cache_shortcut": True, "ef": 50},
        )

        assert mock_vector_store.search.call_count == 2

        code_call = next(
            c for c in captured_calls if c.get("collection_name") == "code_index"
        )
        multimodal_call = next(
            c
            for c in captured_calls
            if c.get("collection_name") == "voyage-multimodal-3"
        )

        assert code_call["no_embedding_cache_shortcut"] is False
        assert multimodal_call["no_embedding_cache_shortcut"] is True

    def test_query_with_separate_kwargs_merges_results_from_both(self, tmp_path):
        """Final merged results must include entries from BOTH collection
        queries (proving this is a real fan-out + merge, not a passthrough
        of only one side)."""
        project_root = _make_project_root(tmp_path)
        _make_multimodal_collection(project_root)

        mock_vector_store = Mock()
        mock_embedding_provider = Mock()

        code_results = [
            {
                "id": "c1",
                "score": 0.9,
                "payload": {"path": "src/file1.py", "chunk_offset": 0},
            }
        ]
        multimodal_results = [
            {
                "id": "m1",
                "score": 0.85,
                "payload": {"path": "docs/guide.md", "chunk_offset": 0},
            }
        ]

        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name")
            if collection == "voyage-multimodal-3":
                return (multimodal_results, {})
            return (code_results, {})

        mock_vector_store.search.side_effect = search_side_effect

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider,
        )

        results, timing = service.query_with_separate_kwargs(
            query_text="test query",
            limit=10,
            collection_name="code_index",
            code_kwargs={},
            multimodal_kwargs={"no_embedding_cache_shortcut": True},
        )

        assert len(results) == 2
        paths = {r["payload"]["path"] for r in results}
        assert paths == {"src/file1.py", "docs/guide.md"}

    def test_query_with_separate_kwargs_defaults_to_empty_dicts(self, tmp_path):
        """code_kwargs/multimodal_kwargs default to None -> treated as {} —
        must not raise when omitted entirely."""
        project_root = _make_project_root(tmp_path)
        # No multimodal collection -> code-only path

        mock_vector_store = Mock()
        mock_vector_store.search.return_value = ([], {})
        mock_embedding_provider = Mock()

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider,
        )

        results, timing = service.query_with_separate_kwargs(
            query_text="test query",
            limit=10,
            collection_name="code_index",
        )

        assert results == []
        assert mock_vector_store.search.call_count == 1
