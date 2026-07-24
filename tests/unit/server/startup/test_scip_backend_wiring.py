"""Unit tests for build_scip_generator (EVO-65021 scip backend wiring)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.scip.indexers.delegated import DelegatedIndexer
from code_indexer.scip.indexers.typescript import TypeScriptIndexer
from code_indexer.server.startup.scip_backend_wiring import build_scip_generator


def _local_config():
    return SimpleNamespace(scip_backend="local", workspace_mcp=None)


def _delegated_config():
    return SimpleNamespace(
        scip_backend="delegated",
        workspace_mcp=SimpleNamespace(
            endpoint="http://sandbox-mcp.internal/rpc", api_key="tok"
        ),
    )


class TestLocalDefault:
    def test_local_default_uses_local_indexers(self, tmp_path):
        gen = build_scip_generator(_local_config(), tmp_path, workspace_id="W")
        assert isinstance(gen._indexers["typescript"], TypeScriptIndexer)
        assert gen.workspace_id == "W"

    def test_missing_scip_backend_attr_defaults_local(self, tmp_path):
        gen = build_scip_generator(SimpleNamespace(), tmp_path)
        assert isinstance(gen._indexers["typescript"], TypeScriptIndexer)
        assert gen.workspace_id is None


class TestDelegated:
    @patch("requests.get")
    def test_delegated_wires_delegated_indexer(self, mock_get, tmp_path):
        mock_get.return_value = MagicMock(status_code=200)
        gen = build_scip_generator(_delegated_config(), tmp_path, workspace_id="W-9")
        assert isinstance(gen._indexers["typescript"], DelegatedIndexer)
        assert gen.workspace_id == "W-9"
        # health check hit sandbox-mcp /health
        assert mock_get.call_args.args[0].endswith("/health")

    @patch("requests.get")
    def test_delegated_health_unreachable_fails_loud(self, mock_get, tmp_path):
        mock_get.side_effect = RuntimeError("connection refused")
        with pytest.raises(RuntimeError, match="sandbox-mcp not reachable"):
            build_scip_generator(_delegated_config(), tmp_path, workspace_id="W")

    def test_delegated_missing_workspace_mcp_fails_loud(self, tmp_path):
        cfg = SimpleNamespace(scip_backend="delegated", workspace_mcp=None)
        with pytest.raises(RuntimeError, match="requires workspace_mcp.endpoint"):
            build_scip_generator(cfg, tmp_path, workspace_id="W")

    def test_delegated_empty_endpoint_fails_loud(self, tmp_path):
        cfg = SimpleNamespace(
            scip_backend="delegated",
            workspace_mcp=SimpleNamespace(endpoint="", api_key=""),
        )
        with pytest.raises(RuntimeError, match="requires workspace_mcp.endpoint"):
            build_scip_generator(cfg, tmp_path, workspace_id="W")
