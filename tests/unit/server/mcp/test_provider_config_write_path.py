"""Tests verifying write operations use base clone path, not versioned snapshot (Bug #625, Fix 3/7/8).

Verifies:
1. bulk_add_provider_index writes config to BASE CLONE, not versioned snapshot (W1)
2. manage_provider_indexes action="add" writes config to base clone (W5)
3. manage_provider_indexes action="remove" calls _remove_provider_from_config on base clone (W2)
4. manage_provider_indexes action="status" uses base clone path (A5)
5. bulk_add_provider_index returns error when background_job_manager is None (Fix 8)
"""

import json
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# ProviderIndexService is imported inside functions via local import, so we must
# patch at its source module path.
_PROVIDER_INDEX_SERVICE_PATH = (
    "code_indexer.server.services.provider_index_service.ProviderIndexService"
)


def _unwrap_mcp(result: dict) -> dict:
    """Unwrap the MCP response envelope to get the inner JSON payload.

    MCP handlers return {"content": [{"type": "text", "text": "<json>"}]}.
    This helper parses and returns the inner dict.
    """
    content = result.get("content", [])
    if content and isinstance(content, list):
        text = content[0].get("text", "{}")
        return json.loads(text)
    return result


def _make_golden_repos_structure(tmp_path: Path) -> tuple[Path, Path]:
    """Create golden-repos/{alias}/ base clone + .versioned/{alias}/v_ts/ snapshot."""
    golden_repos_dir = tmp_path / "golden-repos"

    base_clone = golden_repos_dir / "my-repo"
    base_clone.mkdir(parents=True)
    (base_clone / ".code-indexer").mkdir()
    (base_clone / ".code-indexer" / "config.json").write_text(
        json.dumps(
            {"embedding_provider": "voyage-ai", "embedding_providers": ["voyage-ai"]}
        )
    )

    versioned = golden_repos_dir / ".versioned" / "my-repo" / "v_1772136021"
    versioned.mkdir(parents=True)
    (versioned / ".code-indexer").mkdir()
    (versioned / ".code-indexer" / "config.json").write_text(
        json.dumps(
            {"embedding_provider": "voyage-ai", "embedding_providers": ["voyage-ai"]}
        )
    )

    return base_clone, versioned


def _make_mock_service():
    """Return a mock ProviderIndexService."""
    svc = MagicMock()
    svc.validate_provider.return_value = None  # no error
    svc.list_providers.return_value = [{"name": "cohere"}]
    svc.get_provider_index_status.return_value = {"cohere": {"exists": False}}
    svc.remove_provider_index.return_value = {
        "removed": True,
        "collection_name": "cohere-col",
        "message": "removed",
    }
    return svc


class TestBulkAddWritesConfigToBaseClone:
    """bulk_add_provider_index (W1) must write config to base clone, not versioned snapshot."""

    def test_bulk_add_writes_config_to_base_clone_not_versioned(self, tmp_path):
        """After bulk_add, embedding_providers updated in BASE CLONE config.json."""
        base_clone, versioned = _make_golden_repos_structure(tmp_path)
        mock_svc = _make_mock_service()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-123"
        mock_user = MagicMock()
        mock_user.username = "admin"

        from code_indexer.server.mcp.handlers import bulk_add_provider_index

        fake_app_module = types.SimpleNamespace(background_job_manager=mock_bjm)

        with (
            patch(
                _PROVIDER_INDEX_SERVICE_PATH,
                return_value=mock_svc,
            ),
            patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=[{"alias_name": "my-repo", "category": ""}],
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
                return_value=str(versioned),
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_base_clone",
                return_value=str(base_clone),
            ),
            patch(
                "code_indexer.server.mcp.handlers.app_module",
                new=fake_app_module,
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service"),
        ):
            result = bulk_add_provider_index({"provider": "cohere"}, mock_user)

        payload = _unwrap_mcp(result)
        assert payload.get("success") is True
        assert "error" not in payload

        # The critical assertion: base clone config was updated
        base_config = json.loads(
            (base_clone / ".code-indexer" / "config.json").read_text()
        )
        assert "cohere" in base_config.get("embedding_providers", []), (
            f"Expected cohere in base clone config, got: {base_config}"
        )

        # Versioned snapshot config must NOT be modified
        versioned_config = json.loads(
            (versioned / ".code-indexer" / "config.json").read_text()
        )
        assert "cohere" not in versioned_config.get("embedding_providers", []), (
            f"Versioned snapshot should not have been modified: {versioned_config}"
        )

    def test_bulk_add_none_guard_returns_error(self):
        """bulk_add_provider_index returns error when background_job_manager is None."""
        mock_svc = _make_mock_service()
        mock_user = MagicMock()
        mock_user.username = "admin"

        from code_indexer.server.mcp.handlers import bulk_add_provider_index

        fake_app_module = types.SimpleNamespace(background_job_manager=None)

        with (
            patch(
                _PROVIDER_INDEX_SERVICE_PATH,
                return_value=mock_svc,
            ),
            patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=[],
            ),
            patch(
                "code_indexer.server.mcp.handlers.app_module",
                new=fake_app_module,
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service"),
        ):
            result = bulk_add_provider_index({"provider": "cohere"}, mock_user)

        payload = _unwrap_mcp(result)
        assert "error" in payload


class TestManageProviderIndexesWritePath:
    """manage_provider_indexes add/remove must use base clone for write operations."""

    def test_manage_add_writes_config_to_base_clone(self, tmp_path):
        """action='add' must write embedding_providers to base clone, not versioned snapshot."""
        base_clone, versioned = _make_golden_repos_structure(tmp_path)
        mock_svc = _make_mock_service()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-456"
        mock_user = MagicMock()
        mock_user.username = "admin"

        from code_indexer.server.mcp.handlers import manage_provider_indexes

        fake_app_module = types.SimpleNamespace(background_job_manager=mock_bjm)

        with (
            patch(
                _PROVIDER_INDEX_SERVICE_PATH,
                return_value=mock_svc,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
                return_value=str(versioned),
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_base_clone",
                return_value=str(base_clone),
            ),
            patch(
                "code_indexer.server.mcp.handlers.app_module",
                new=fake_app_module,
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service"),
        ):
            result = manage_provider_indexes(
                {
                    "action": "add",
                    "provider": "cohere",
                    "repository_alias": "my-repo-global",
                },
                mock_user,
            )

        payload = _unwrap_mcp(result)
        assert payload.get("success") is True
        assert "error" not in payload

        base_config = json.loads(
            (base_clone / ".code-indexer" / "config.json").read_text()
        )
        assert "cohere" in base_config.get("embedding_providers", []), (
            f"Expected cohere in base clone config after manage add, got: {base_config}"
        )

        # Versioned snapshot config must NOT be modified
        versioned_config = json.loads(
            (versioned / ".code-indexer" / "config.json").read_text()
        )
        assert "cohere" not in versioned_config.get("embedding_providers", []), (
            f"Versioned snapshot should not have been modified: {versioned_config}"
        )

    def test_remove_updates_config_on_base_clone(self, tmp_path):
        """action='remove' must call _remove_provider_from_config on base clone path."""
        base_clone, versioned = _make_golden_repos_structure(tmp_path)
        # Pre-populate cohere in both configs
        for p in [base_clone, versioned]:
            cfg_path = p / ".code-indexer" / "config.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "embedding_provider": "voyage-ai",
                        "embedding_providers": ["voyage-ai", "cohere"],
                    }
                )
            )

        mock_svc = _make_mock_service()
        mock_user = MagicMock()
        mock_user.username = "admin"

        from code_indexer.server.mcp.handlers import manage_provider_indexes

        with (
            patch(
                _PROVIDER_INDEX_SERVICE_PATH,
                return_value=mock_svc,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
                return_value=str(versioned),
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_base_clone",
                return_value=str(base_clone),
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service"),
        ):
            result = manage_provider_indexes(
                {
                    "action": "remove",
                    "provider": "cohere",
                    "repository_alias": "my-repo-global",
                },
                mock_user,
            )

        payload_remove = _unwrap_mcp(result)
        assert payload_remove.get("success") is True
        assert "error" not in payload_remove

        base_config = json.loads(
            (base_clone / ".code-indexer" / "config.json").read_text()
        )
        assert "cohere" not in base_config.get("embedding_providers", []), (
            f"Expected cohere removed from base clone config, got: {base_config}"
        )

        # Versioned snapshot config still has cohere (we only modify base clone)
        versioned_config = json.loads(
            (versioned / ".code-indexer" / "config.json").read_text()
        )
        assert "cohere" in versioned_config.get("embedding_providers", []), (
            f"Versioned snapshot should not have been modified by remove: {versioned_config}"
        )

    def test_status_uses_base_clone_or_fallback_to_versioned(self, tmp_path):
        """action='status' uses base clone when available, falls back to versioned path."""
        base_clone, versioned = _make_golden_repos_structure(tmp_path)
        mock_svc = _make_mock_service()
        mock_svc.get_provider_index_status.return_value = {
            "voyage-ai": {"exists": True},
            "cohere": {"exists": False},
        }
        mock_user = MagicMock()
        mock_user.username = "admin"

        from code_indexer.server.mcp.handlers import manage_provider_indexes

        with (
            patch(
                _PROVIDER_INDEX_SERVICE_PATH,
                return_value=mock_svc,
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
                return_value=str(versioned),
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_base_clone",
                return_value=str(base_clone),
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service"),
        ):
            result = manage_provider_indexes(
                {
                    "action": "status",
                    "repository_alias": "my-repo-global",
                },
                mock_user,
            )

        payload_status = _unwrap_mcp(result)
        assert payload_status.get("success") is True
        assert "error" not in payload_status
        assert "provider_indexes" in payload_status
        # Verify get_provider_index_status was called with the base clone path
        call_args = mock_svc.get_provider_index_status.call_args
        assert call_args is not None
        path_used = call_args[0][0]
        assert ".versioned" not in path_used, (
            f"Status should use base clone path, not versioned: {path_used}"
        )


class TestManageProviderIndexesBaseCloneNoneGuard:
    """manage_provider_indexes add/recreate must fail fast when base clone is unresolvable (Critical 1, Bug #625)."""

    def _make_mock_service(self):
        return _make_mock_service()

    def test_add_returns_error_when_base_clone_none(self):
        """action='add' must return error when _resolve_golden_repo_base_clone returns None."""
        mock_svc = self._make_mock_service()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-999"
        mock_user = MagicMock()
        mock_user.username = "admin"

        from code_indexer.server.mcp.handlers import manage_provider_indexes

        fake_app_module = types.SimpleNamespace(background_job_manager=mock_bjm)

        with (
            patch(_PROVIDER_INDEX_SERVICE_PATH, return_value=mock_svc),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
                return_value="/data/.versioned/ghost-repo/v_123",
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_base_clone",
                return_value=None,
            ),
            patch("code_indexer.server.mcp.handlers.app_module", new=fake_app_module),
            patch("code_indexer.server.mcp.handlers.get_config_service"),
        ):
            result = manage_provider_indexes(
                {
                    "action": "add",
                    "provider": "cohere",
                    "repository_alias": "ghost-repo-global",
                },
                mock_user,
            )

        payload = _unwrap_mcp(result)
        assert "error" in payload, (
            f"Expected error when base clone unresolvable for 'add', got: {payload}"
        )
        assert payload.get("success") is not True
        # Job must NOT have been submitted.
        mock_bjm.submit_job.assert_not_called()

    def test_recreate_returns_error_when_base_clone_none(self):
        """action='recreate' must return error when _resolve_golden_repo_base_clone returns None."""
        mock_svc = self._make_mock_service()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-999"
        mock_user = MagicMock()
        mock_user.username = "admin"

        from code_indexer.server.mcp.handlers import manage_provider_indexes

        fake_app_module = types.SimpleNamespace(background_job_manager=mock_bjm)

        with (
            patch(_PROVIDER_INDEX_SERVICE_PATH, return_value=mock_svc),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
                return_value="/data/.versioned/ghost-repo/v_123",
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_base_clone",
                return_value=None,
            ),
            patch("code_indexer.server.mcp.handlers.app_module", new=fake_app_module),
            patch("code_indexer.server.mcp.handlers.get_config_service"),
        ):
            result = manage_provider_indexes(
                {
                    "action": "recreate",
                    "provider": "cohere",
                    "repository_alias": "ghost-repo-global",
                },
                mock_user,
            )

        payload = _unwrap_mcp(result)
        assert "error" in payload, (
            f"Expected error when base clone unresolvable for 'recreate', got: {payload}"
        )
        assert payload.get("success") is not True
        mock_bjm.submit_job.assert_not_called()


class TestProviderIndexJobBaseCloneMissing:
    """_provider_index_job must return error when versioned snapshot has no base clone (Critical 2, Bug #625)."""

    def test_provider_index_job_returns_error_when_base_clone_missing(self, tmp_path):
        """When repo_path is a versioned snapshot and the base clone dir does not exist
        on disk, the job must return {'success': False, 'error': ...} without running cidx.

        Critical 2 (Bug #625): the previous fall-through bug let actual_path stay as
        the versioned snapshot path and cidx index ran against an immutable dir.
        The fix adds an explicit error return when base_clone.exists() is False.
        """
        # Create only the versioned snapshot dir — base clone is deliberately absent.
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        versioned = golden_repos_dir / ".versioned" / "ghost-repo" / "v_1772136021"
        versioned.mkdir(parents=True)
        (versioned / ".code-indexer").mkdir()
        (versioned / ".code-indexer" / "config.json").write_text(
            '{"embedding_provider":"voyage-ai","embedding_providers":["voyage-ai","cohere"]}'
        )
        # golden_repos_dir / "ghost-repo" (the base clone) does NOT exist.

        from code_indexer.server.mcp.handlers import _provider_index_job

        mock_config = MagicMock()
        mock_config.cohere_api_key = "test-key"

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_get_cfg:
            mock_get_cfg.return_value.get_config.return_value = mock_config
            result = _provider_index_job(
                repo_path=str(versioned),
                provider_name="cohere",
                clear=False,
            )

        assert result.get("success") is False, (
            f"Expected success=False when base clone missing, got: {result}"
        )
        assert "error" in result, (
            f"Expected 'error' key in result when base clone missing, got: {result}"
        )
        # The error message must mention base clone or immutable.
        error_msg = result["error"]
        assert any(
            keyword in error_msg.lower()
            for keyword in ("base clone", "immutable", "versioned", "not found")
        ), f"Error message not descriptive enough: {error_msg}"
