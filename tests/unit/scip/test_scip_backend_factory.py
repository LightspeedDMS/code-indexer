"""Unit tests for ScipBackendFactory (issue-65021)."""

import pytest

from cidx_scip.indexers.base import SCIPIndexer
from cidx_scip.indexers.factory import ScipBackendFactory
from cidx_scip.indexers.java import JavaIndexer
from cidx_scip.indexers.typescript import TypeScriptIndexer


class TestLocalMap:
    def test_local_map_languages(self):
        m = ScipBackendFactory.create_local_indexer_map()
        assert set(m) == {"java", "kotlin", "typescript", "python", "csharp", "go"}
        assert all(isinstance(v, SCIPIndexer) for v in m.values())

    def test_local_map_types(self):
        m = ScipBackendFactory.create_local_indexer_map()
        assert isinstance(m["typescript"], TypeScriptIndexer)
        assert isinstance(m["java"], JavaIndexer)
        assert isinstance(m["kotlin"], JavaIndexer)

    def test_create_indexer_map_local(self):
        m = ScipBackendFactory.create_indexer_map("local")
        assert isinstance(m["typescript"], TypeScriptIndexer)


class TestInvalid:
    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unsupported scip_backend"):
            ScipBackendFactory.create_indexer_map("bogus")

    def test_removed_delegated_backend_raises(self, tmp_path):
        # sandbox_scip's backend is gone; the factory must not build it.
        with pytest.raises(ValueError, match="Unsupported scip_backend"):
            ScipBackendFactory.create_indexer_map("delegated", repo_root=tmp_path)
