"""Unit tests for ScipBackendFactory (EVO-65021)."""

from types import SimpleNamespace

import pytest

from code_indexer.scip.indexers.base import SCIPIndexer
from code_indexer.scip.indexers.delegated import DelegatedIndexer
from code_indexer.scip.indexers.factory import ScipBackendFactory
from code_indexer.scip.indexers.java import JavaIndexer
from code_indexer.scip.indexers.typescript import TypeScriptIndexer


def _wsm_config():
    return SimpleNamespace(endpoint="http://sandbox-mcp/rpc", api_key="k")


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


class TestDelegatedMap:
    def test_delegated_map_all_languages_share_one_instance(self, tmp_path):
        m = ScipBackendFactory.create_indexer_map(
            "delegated", repo_root=tmp_path, workspace_mcp_config=_wsm_config()
        )
        assert set(m) == {"java", "kotlin", "typescript", "python", "csharp", "go"}
        instances = set(id(v) for v in m.values())
        assert len(instances) == 1  # one shared DelegatedIndexer
        assert all(isinstance(v, DelegatedIndexer) for v in m.values())

    def test_delegated_requires_config(self, tmp_path):
        with pytest.raises(ValueError, match="workspace_mcp_config is required"):
            ScipBackendFactory.create_indexer_map("delegated", repo_root=tmp_path)

    def test_delegated_requires_repo_root(self):
        with pytest.raises(ValueError, match="repo_root is required"):
            ScipBackendFactory.create_indexer_map(
                "delegated", workspace_mcp_config=_wsm_config()
            )


class TestInvalid:
    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unsupported scip_backend"):
            ScipBackendFactory.create_indexer_map("bogus")
