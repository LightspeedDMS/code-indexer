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
    validate_scip_backend_config,
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


class TestFailLoudOnBadValue:
    """EVO-65021: an unrecognised scip_backend must RAISE, never silently
    downgrade to a local build (which would come up 'healthy' then fail every
    project with tool-not-found on the toolchain-less indexing pods)."""

    @pytest.mark.parametrize("bad", ["delegatd", "Delegated", "remote", "DELEGATED"])
    def test_unknown_value_fails_loud(self, tmp_path, bad):
        cfg = SimpleNamespace(scip_backend=bad, workspace_mcp=None)
        with pytest.raises(RuntimeError, match="Unsupported scip_backend"):
            build_scip_generator(cfg, tmp_path, workspace_id="W")

    def test_error_names_the_bad_value(self, tmp_path):
        cfg = SimpleNamespace(scip_backend="delegatd", workspace_mcp=None)
        with pytest.raises(RuntimeError, match="delegatd"):
            build_scip_generator(cfg, tmp_path)

    def test_no_longer_references_nonexistent_env_var(self, tmp_path):
        """The old diagnostic pointed at CIDX_SCIP_BACKEND, an env var read
        nowhere. The health-check error must not mention it."""
        import requests

        cfg = SimpleNamespace(
            scip_backend="delegated",
            workspace_mcp=SimpleNamespace(endpoint="http://sb", api_key="k"),
        )
        with patch("requests.get", side_effect=requests.RequestException("down")):
            with pytest.raises(RuntimeError) as exc:
                build_scip_generator(cfg, tmp_path, workspace_id="W")
        assert "CIDX_SCIP_BACKEND" not in str(exc.value)


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
    def test_delegated_threads_golden_alias_to_generator(self, mock_get, tmp_path):
        mock_get.return_value = MagicMock(status_code=200)
        gen = build_scip_generator(
            _delegated_config(), tmp_path, golden_alias="svc-bar"
        )
        assert isinstance(gen._indexers["typescript"], DelegatedIndexer)
        assert gen.golden_alias == "svc-bar"
        assert gen.workspace_id is None

    @patch("requests.get")
    def test_delegated_health_unreachable_fails_loud(self, mock_get, tmp_path):
        mock_get.side_effect = RuntimeError("connection refused")
        with pytest.raises(RuntimeError, match="sandbox-mcp not reachable"):
            build_scip_generator(_delegated_config(), tmp_path, workspace_id="W")

    def test_delegated_missing_workspace_mcp_fails_loud(self, tmp_path):
        cfg = SimpleNamespace(scip_backend="delegated", workspace_mcp=None)
        with pytest.raises(RuntimeError, match="requires scip_sandbox.endpoint"):
            build_scip_generator(cfg, tmp_path, workspace_id="W")

    def test_delegated_empty_endpoint_fails_loud(self, tmp_path):
        cfg = SimpleNamespace(
            scip_backend="delegated",
            workspace_mcp=SimpleNamespace(endpoint="", api_key=""),
        )
        with pytest.raises(RuntimeError, match="requires scip_sandbox.endpoint"):
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


class TestValidateScipBackendConfig:
    """EVO-65021: the eager startup config validation (fail-loud call site).

    Validates the STATIC config only — no sandbox-mcp health check — so a
    misconfiguration surfaces loudly at boot instead of being swallowed per-job
    by ``funnel_scip_generation``'s blanket fail-soft."""

    def test_local_config_passes(self):
        # None => no raise
        assert validate_scip_backend_config(_local_config()) is None

    def test_missing_attr_defaults_local_and_passes(self):
        assert validate_scip_backend_config(SimpleNamespace()) is None

    def test_delegated_with_endpoint_passes_without_health_check(self):
        # No requests.get patch: config validation must NOT reach out to
        # sandbox-mcp (config-only, reachability is a runtime fail-soft).
        with patch("requests.get", side_effect=AssertionError("must not be called")):
            assert validate_scip_backend_config(_delegated_config()) is None

    @pytest.mark.parametrize("bad", ["delegatd", "Delegated", "remote", "DELEGATED"])
    def test_unknown_value_raises(self, bad):
        cfg = SimpleNamespace(scip_backend=bad, workspace_mcp=None)
        with pytest.raises(RuntimeError, match="Unsupported scip_backend"):
            validate_scip_backend_config(cfg)

    def test_unknown_value_names_the_bad_value(self):
        cfg = SimpleNamespace(scip_backend="delegatd", workspace_mcp=None)
        with pytest.raises(RuntimeError, match="delegatd"):
            validate_scip_backend_config(cfg)

    def test_delegated_missing_workspace_mcp_raises(self):
        cfg = SimpleNamespace(scip_backend="delegated", workspace_mcp=None)
        with pytest.raises(RuntimeError, match="requires scip_sandbox.endpoint"):
            validate_scip_backend_config(cfg)

    def test_delegated_empty_endpoint_raises(self):
        cfg = SimpleNamespace(
            scip_backend="delegated",
            workspace_mcp=SimpleNamespace(endpoint="", api_key=""),
        )
        with pytest.raises(RuntimeError, match="requires scip_sandbox.endpoint"):
            validate_scip_backend_config(cfg)


class TestScipSandboxEndpointDecoupling:
    """EVO-65365: the delegated SCIP backend targets the STANDING sandbox-mcp via
    ``scip_sandbox`` (a different service than the workspace-mcp that
    ``workspace_mcp`` addresses for clone/git delegation). ``scip_sandbox`` wins
    when set; ``workspace_mcp`` is the backward-compat fallback."""

    @patch("requests.get")
    def test_scip_sandbox_endpoint_is_used_when_set(self, mock_get, tmp_path):
        mock_get.return_value = MagicMock(status_code=200)
        cfg = SimpleNamespace(
            scip_backend="delegated",
            scip_sandbox=SimpleNamespace(
                endpoint="http://sandbox-mcp.claude-code-indexer:8200",
                api_key="scip-tok",
            ),
            workspace_mcp=SimpleNamespace(
                endpoint="http://workspace-mcp.internal/workspace", api_key="ws-tok"
            ),
        )
        gen = build_scip_generator(cfg, tmp_path, golden_alias="svc")
        indexer = gen._indexers["typescript"]
        assert isinstance(indexer, DelegatedIndexer)
        # SCIP goes to sandbox-mcp, NOT the workspace-mcp endpoint.
        assert indexer._endpoint == "http://sandbox-mcp.claude-code-indexer:8200"
        assert indexer._api_key == "scip-tok"
        # The health check hit the sandbox-mcp endpoint.
        assert mock_get.call_args.args[0].startswith(
            "http://sandbox-mcp.claude-code-indexer:8200"
        )

    @patch("requests.get")
    def test_falls_back_to_workspace_mcp_when_scip_sandbox_absent(
        self, mock_get, tmp_path
    ):
        mock_get.return_value = MagicMock(status_code=200)
        cfg = SimpleNamespace(
            scip_backend="delegated",
            scip_sandbox=None,
            workspace_mcp=SimpleNamespace(
                endpoint="http://workspace-mcp.internal/workspace", api_key="ws-tok"
            ),
        )
        gen = build_scip_generator(cfg, tmp_path, golden_alias="svc")
        indexer = gen._indexers["typescript"]
        assert isinstance(indexer, DelegatedIndexer)
        assert indexer._endpoint == "http://workspace-mcp.internal/workspace"
        assert indexer._api_key == "ws-tok"

    @patch("requests.get")
    def test_empty_scip_sandbox_endpoint_falls_back_to_workspace_mcp(
        self, mock_get, tmp_path
    ):
        """A scip_sandbox with an EMPTY endpoint is not usable → fall back."""
        mock_get.return_value = MagicMock(status_code=200)
        cfg = SimpleNamespace(
            scip_backend="delegated",
            scip_sandbox=SimpleNamespace(endpoint="", api_key=""),
            workspace_mcp=SimpleNamespace(
                endpoint="http://workspace-mcp.internal/workspace", api_key="ws-tok"
            ),
        )
        gen = build_scip_generator(cfg, tmp_path, golden_alias="svc")
        assert gen._indexers["typescript"]._endpoint == (
            "http://workspace-mcp.internal/workspace"
        )

    def test_both_absent_fails_loud(self, tmp_path):
        cfg = SimpleNamespace(
            scip_backend="delegated", scip_sandbox=None, workspace_mcp=None
        )
        with pytest.raises(RuntimeError, match="requires scip_sandbox.endpoint"):
            build_scip_generator(cfg, tmp_path, golden_alias="svc")

    def test_validate_uses_scip_sandbox_when_set(self):
        cfg = SimpleNamespace(
            scip_backend="delegated",
            scip_sandbox=SimpleNamespace(endpoint="http://sb", api_key="k"),
            workspace_mcp=None,
        )
        # scip_sandbox satisfies the endpoint requirement even with no workspace_mcp.
        assert validate_scip_backend_config(cfg) is None

    def test_validate_both_absent_raises(self):
        cfg = SimpleNamespace(
            scip_backend="delegated", scip_sandbox=None, workspace_mcp=None
        )
        with pytest.raises(RuntimeError, match="requires scip_sandbox.endpoint"):
            validate_scip_backend_config(cfg)


class TestGoldenRepoAllowDegenerate:
    """EVO-65365: golden-repo builds opt into the sandbox allow_degenerate path."""

    @patch("requests.get")
    def test_golden_repo_generator_has_allow_degenerate_true(self, mock_get, tmp_path):
        mock_get.return_value = MagicMock(status_code=200)
        mgr = _FakeManager()
        gen = build_scip_generator_for_golden_repo(
            _delegated_config(),
            tmp_path,
            golden_repo_manager=mgr,
            alias="svc-foo",
        )
        assert gen.allow_degenerate is True

    def test_non_golden_build_scip_generator_defaults_false(self, tmp_path):
        gen = build_scip_generator(_local_config(), tmp_path, workspace_id="W")
        assert gen.allow_degenerate is False


class TestGoldenRepoBridge:
    """EVO-65365 Option C (seam #3): delegated golden repos are addressed BY ALIAS.

    The old workspace_id resolution (and its fail-soft None-skip) is gone — a
    golden repo now has no workspace_id to resolve, so the manager is never
    consulted for addressing and every delegated golden repo is addressable.
    """

    def test_local_backend_addresses_neither(self, tmp_path):
        mgr = _FakeManager(raises=True)  # would raise if consulted
        gen = build_scip_generator_for_golden_repo(
            _local_config(),
            tmp_path,
            golden_repo_manager=mgr,
            alias="svc-foo",
        )
        assert isinstance(gen._indexers["typescript"], TypeScriptIndexer)
        assert gen.workspace_id is None
        assert gen.golden_alias is None
        # local backend never consults the manager
        assert mgr.calls == []

    @patch("requests.get")
    def test_delegated_addresses_by_golden_alias(self, mock_get, tmp_path):
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
        # The delegated build is addressed by the bare golden alias, NOT a
        # workspace_id — and the manager is NOT consulted for one.
        assert gen.golden_alias == "svc-foo"
        assert gen.workspace_id is None
        assert mgr.calls == []

    @patch("requests.get")
    def test_delegated_no_workspace_id_no_longer_skips(self, mock_get, tmp_path):
        """Option C: a golden repo with no workspace_id is STILL built (by alias).

        Under the old seam #3 a manager that raised WorkspaceIdNotRetainedError
        made this return None (skip SCIP). That fail-soft dead-end is removed —
        the repo is addressed by alias and a real generator comes back.
        """
        mock_get.return_value = MagicMock(status_code=200)
        mgr = _FakeManager(raises=True)  # would raise if it were still consulted
        gen = build_scip_generator_for_golden_repo(
            _delegated_config(),
            tmp_path,
            golden_repo_manager=mgr,
            alias="legacy-repo",
        )
        assert gen is not None
        assert isinstance(gen._indexers["typescript"], DelegatedIndexer)
        assert gen.golden_alias == "legacy-repo"
        assert gen.workspace_id is None
        # No workspace_id resolution is attempted at all.
        assert mgr.calls == []
        # The delegated backend is wired, so the health check DID run.
        assert mock_get.call_args.args[0].endswith("/health")
