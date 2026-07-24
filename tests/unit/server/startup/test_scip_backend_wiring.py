"""Unit tests for build_scip_generator (EVO-65021 scip backend wiring)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.scip.indexers.delegated import DelegatedIndexer
from code_indexer.scip.indexers.typescript import TypeScriptIndexer
from code_indexer.server.repositories.golden_repo_manager import (
    WorkspaceIdNotRetainedError,
)
from code_indexer.server.startup.scip_backend_wiring import (
    build_scip_generator,
    build_scip_generator_for_golden_repo,
)


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


class _FakeManager:
    """Minimal stand-in exposing only resolve_workspace_id (seam #3)."""

    def __init__(self, *, workspace_id=None, raises=False):
        self._workspace_id = workspace_id
        self._raises = raises
        self.calls = []

    def resolve_workspace_id(self, alias):
        self.calls.append(alias)
        if self._raises:
            raise WorkspaceIdNotRetainedError(f"no workspace_id for '{alias}'")
        return self._workspace_id


class TestGoldenRepoBridge:
    """EVO-65021 seam #3: resolve_workspace_id -> build_scip_generator."""

    def test_local_backend_ignores_workspace_id_lookup(self, tmp_path):
        mgr = _FakeManager(raises=True)  # would raise if consulted
        gen = build_scip_generator_for_golden_repo(
            _local_config(),
            tmp_path,
            golden_repo_manager=mgr,
            alias="svc-foo",
        )
        assert isinstance(gen._indexers["typescript"], TypeScriptIndexer)
        assert gen.workspace_id is None
        # local backend never resolves a workspace_id
        assert mgr.calls == []

    @patch("requests.get")
    def test_delegated_resolves_and_names_real_workspace(self, mock_get, tmp_path):
        mock_get.return_value = MagicMock(status_code=200)
        mgr = _FakeManager(workspace_id="ws-real-1234")
        gen = build_scip_generator_for_golden_repo(
            _delegated_config(),
            tmp_path,
            golden_repo_manager=mgr,
            alias="svc-foo",
        )
        assert gen is not None
        assert isinstance(gen._indexers["typescript"], DelegatedIndexer)
        # the delegated build names the REAL resolved workspace W
        assert gen.workspace_id == "ws-real-1234"
        assert mgr.calls == ["svc-foo"]

    @patch("requests.get")
    def test_delegated_unretained_workspace_id_fail_soft_returns_none(
        self, mock_get, tmp_path
    ):
        mgr = _FakeManager(raises=True)
        gen = build_scip_generator_for_golden_repo(
            _delegated_config(),
            tmp_path,
            golden_repo_manager=mgr,
            alias="legacy-repo",
        )
        # Fail-soft: skip SCIP for this repo rather than building against sb-None.
        assert gen is None
        assert mgr.calls == ["legacy-repo"]
        # No build/health check is attempted once the workspace_id is missing.
        mock_get.assert_not_called()
