"""
Unit tests for Bug #598: embedding_purpose="query" must be passed during search.

Cohere maps embedding_purpose to input_type in the API:
  "query" -> "search_query"
  "document" -> "search_document"

Without embedding_purpose="query", every query defaults to "search_document"
which produces incorrect similarity scores for Cohere embeddings.

VoyageAI accepts embedding_purpose but ignores it, so this fix is safe for all
providers.

Tests verify:
1. FilesystemVectorStore.search() passes embedding_purpose="query" to get_embedding()
   via a behavioral test exercising the real code path
2. CLI query function's git-aware search path (~line 5969) passes embedding_purpose="query"
3. CLI query function's non-git search path (~line 6029) passes embedding_purpose="query"
"""

import ast
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestFilesystemVectorStorePassesEmbeddingPurpose:
    """Behavioral test: FilesystemVectorStore.search() must pass embedding_purpose='query'."""

    def _create_minimal_collection(
        self, base_path: Path, collection_name: str, vector_size: int = 64
    ) -> Path:
        """Create a minimal collection directory with collection_meta.json."""
        collection_path = base_path / collection_name
        collection_path.mkdir(parents=True)
        meta = {"vector_size": vector_size, "distance": "cosine"}
        with open(collection_path / "collection_meta.json", "w") as f:
            json.dump(meta, f)
        return collection_path

    def test_search_passes_embedding_purpose_query(self):
        """FilesystemVectorStore.search() must call get_embedding with embedding_purpose='query'."""
        from src.code_indexer.storage.filesystem_vector_store import (
            FilesystemVectorStore,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir)
            collection_name = "test_coll"
            self._create_minimal_collection(base_path, collection_name)

            store = FilesystemVectorStore(base_path)

            mock_provider = MagicMock()
            mock_provider.get_embedding.return_value = [0.1] * 64

            # Patch HNSWIndexManager at its source module (lazily imported inside search())
            mock_hnsw_manager = MagicMock()
            mock_hnsw_manager.is_stale.return_value = False
            mock_hnsw_index = MagicMock()
            mock_hnsw_manager.load_index.return_value = mock_hnsw_index
            # Return empty results so search completes without real payloads
            mock_hnsw_manager.query.return_value = ([], [])

            # Patch IDIndexManager at its source module (lazily imported inside search())
            with patch(
                "src.code_indexer.storage.hnsw_index_manager.HNSWIndexManager",
                return_value=mock_hnsw_manager,
            ):
                with patch(
                    "src.code_indexer.storage.id_index_manager.IDIndexManager"
                ) as mock_id_cls:
                    mock_id_manager = MagicMock()
                    mock_id_manager.load_index.return_value = {}
                    mock_id_cls.return_value = mock_id_manager

                    store.search(
                        query="find authentication code",
                        embedding_provider=mock_provider,
                        collection_name=collection_name,
                        limit=5,
                    )

            # Assert get_embedding was called with embedding_purpose="query"
            assert mock_provider.get_embedding.called, (
                "get_embedding must be called during search"
            )
            call_args = mock_provider.get_embedding.call_args
            kwargs = call_args.kwargs
            assert kwargs.get("embedding_purpose") == "query", (
                f"get_embedding must be called with embedding_purpose='query' during search, "
                f"but was called with args={call_args.args!r}, kwargs={kwargs!r}"
            )


class TestCLIQueryFunctionPassesEmbeddingPurpose:
    """AST tests: both get_embedding calls inside the cli.py 'query' function use embedding_purpose='query'."""

    def _get_get_embedding_calls_in_query_func(
        self,
    ) -> tuple[list[int], list[int]]:
        """
        Parse cli.py and return lines of get_embedding() calls inside the 'query' function,
        separated into (calls_with_query_purpose, calls_without_purpose).
        """
        cli_path = Path("src/code_indexer/cli.py")
        source = cli_path.read_text()
        tree = ast.parse(source)

        # Find the top-level 'query' FunctionDef node
        query_func_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "query":
                query_func_node = node
                break

        assert query_func_node is not None, "Could not find 'query' function in cli.py"

        with_purpose: list[int] = []
        without_purpose: list[int] = []

        for node in ast.walk(query_func_node):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_embedding"
            ):
                continue
            has_query_purpose = any(
                kw.arg == "embedding_purpose"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == "query"
                for kw in node.keywords
            )
            if has_query_purpose:
                with_purpose.append(node.lineno)
            else:
                without_purpose.append(node.lineno)

        return with_purpose, without_purpose

    def test_git_aware_path_passes_embedding_purpose_query(self):
        """CLI git-aware search path (~line 5969) must call get_embedding with embedding_purpose='query'."""
        with_purpose, without_purpose = self._get_get_embedding_calls_in_query_func()

        assert len(with_purpose) >= 1, (
            "The 'query' function in cli.py must have at least 1 get_embedding() call "
            "with embedding_purpose='query' (git-aware path). "
            f"Found {len(with_purpose)} with purpose='query' at lines: {with_purpose}. "
            f"Calls without embedding_purpose at lines: {without_purpose}"
        )

    def test_both_query_paths_pass_embedding_purpose_query(self):
        """Both CLI search paths (~lines 5969 and 6029) must pass embedding_purpose='query'."""
        with_purpose, without_purpose = self._get_get_embedding_calls_in_query_func()

        assert len(with_purpose) >= 2, (
            "The 'query' function in cli.py must have at least 2 get_embedding() calls "
            "with embedding_purpose='query', covering both the git-aware path (~line 5969) "
            "and the non-git path (~line 6029). "
            f"Found only {len(with_purpose)} at lines: {with_purpose}. "
            f"Calls without embedding_purpose at lines: {without_purpose}"
        )
