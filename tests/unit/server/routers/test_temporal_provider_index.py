"""
Unit tests for Story #641: Temporal indexing with provider selection.

AC-1 (HTML/JS checkbox wiring) is a structural UI change — not testable as a
backend unit test. Covered by visual inspection and E2E tests.

AC-2: Server route handles providers for temporal index type (3 tests)
AC-3: Background job _provider_temporal_index_job builds correct command (2 tests)
AC-4: AddIndexRequest.providers description mentions temporal (1 test)
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, initial_config: dict) -> Path:
    """Create a minimal repo directory with .code-indexer/config.json."""
    repo_dir = tmp_path / "golden-repos" / "my-repo"
    repo_dir.mkdir(parents=True)
    ci_dir = repo_dir / ".code-indexer"
    ci_dir.mkdir()
    (ci_dir / "config.json").write_text(json.dumps(initial_config))
    return repo_dir


def _mock_server_config(voyage_key="voyage-key", cohere_key=None) -> MagicMock:
    """Return a minimal mock server config with API keys."""
    cfg = MagicMock()
    cfg.voyageai_api_key = voyage_key
    cfg.cohere_api_key = cohere_key
    return cfg


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_test_client():
    """TestClient with admin user dependency override, cleaned up after each test."""
    admin = User(
        username="testadmin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# AC-2: Route handles providers for temporal
# ---------------------------------------------------------------------------


class TestAddIndexTemporalWithProviders:
    """POST /api/admin/golden-repos/{alias}/indexes with temporal + providers."""

    def test_add_index_temporal_with_providers_creates_per_provider_jobs(
        self, admin_test_client
    ):
        """AC-2: When providers specified with temporal, submit per-provider jobs.

        Verifies that background_job_manager.submit_job is called once per
        provider with operation_type='provider_temporal_index_add'.
        """
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/indexes", "POST")

        mock_bgm = MagicMock()
        mock_bgm.submit_job.side_effect = ["job-temporal-voyage", "job-temporal-cohere"]

        mock_grm = MagicMock()
        mock_grm.golden_repos = {"my-repo": MagicMock(temporal_options=None)}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
                return_value="/some/repo/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_base_clone",
                return_value="/some/base/clone",
            ),
            patch(
                "code_indexer.server.mcp.handlers._append_provider_to_config",
            ),
            patch(
                "code_indexer.server.mcp.handlers._provider_temporal_index_job",
            ),
            _patch_closure(handler, "background_job_manager", mock_bgm),
            _patch_closure(handler, "golden_repo_manager", mock_grm),
        ):
            response = admin_test_client.post(
                "/api/admin/golden-repos/my-repo/indexes",
                json={
                    "index_types": ["temporal"],
                    "providers": ["voyage-ai", "cohere"],
                },
            )

        assert response.status_code == 202
        assert mock_bgm.submit_job.call_count == 2
        call_args_list = mock_bgm.submit_job.call_args_list
        operation_types = [c.kwargs.get("operation_type") for c in call_args_list]
        providers_in_request = ["voyage-ai", "cohere"]
        expected = [f"provider_temporal_index_add:{p}" for p in providers_in_request]
        assert operation_types == expected

    def test_add_index_temporal_without_providers_uses_generic_job(
        self, admin_test_client
    ):
        """AC-2: When no providers, temporal uses generic add_indexes_to_golden_repo.

        Verifies that background_job_manager.submit_job is NOT called and
        golden_repo_manager.add_indexes_to_golden_repo IS called with 'temporal'.
        """
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/indexes", "POST")

        mock_bgm = MagicMock()

        mock_grm = MagicMock()
        mock_grm.golden_repos = {"my-repo": MagicMock(temporal_options=None)}
        mock_grm.add_indexes_to_golden_repo.return_value = "job-temporal-generic"

        with (
            _patch_closure(handler, "background_job_manager", mock_bgm),
            _patch_closure(handler, "golden_repo_manager", mock_grm),
        ):
            response = admin_test_client.post(
                "/api/admin/golden-repos/my-repo/indexes",
                json={"index_types": ["temporal"]},
            )

        assert response.status_code == 202
        mock_bgm.submit_job.assert_not_called()
        mock_grm.add_indexes_to_golden_repo.assert_called_once()

    def test_temporal_options_passed_via_kwargs(self, admin_test_client):
        """AC-2/3: temporal_options from golden repo metadata forwarded to submit_job.

        Verifies the route reads temporal_options from golden_repo_manager and
        passes them as 'temporal_options' kwarg to background_job_manager.submit_job.
        """
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/indexes", "POST")

        mock_bgm = MagicMock()
        mock_bgm.submit_job.return_value = "job-temporal-opts"

        temporal_opts = {"diff_context": 3, "max_commits": 100, "all_branches": True}
        mock_repo_meta = MagicMock()
        mock_repo_meta.temporal_options = temporal_opts

        mock_grm = MagicMock()
        mock_grm.golden_repos = {"my-repo": mock_repo_meta}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
                return_value="/some/repo/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_base_clone",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers._append_provider_to_config",
            ),
            patch(
                "code_indexer.server.mcp.handlers._provider_temporal_index_job",
            ),
            _patch_closure(handler, "background_job_manager", mock_bgm),
            _patch_closure(handler, "golden_repo_manager", mock_grm),
        ):
            response = admin_test_client.post(
                "/api/admin/golden-repos/my-repo/indexes",
                json={"index_types": ["temporal"], "providers": ["voyage-ai"]},
            )

        assert response.status_code == 202
        mock_bgm.submit_job.assert_called_once()
        submitted_kwargs = mock_bgm.submit_job.call_args.kwargs
        assert submitted_kwargs.get("temporal_options") == temporal_opts


# ---------------------------------------------------------------------------
# AC-3: _provider_temporal_index_job builds correct command
# ---------------------------------------------------------------------------


class TestProviderTemporalIndexJobCommand:
    """_provider_temporal_index_job must build cidx index --index-commits command."""

    def test_provider_temporal_index_job_builds_correct_command(self, tmp_path):
        """AC-3: Command includes --index-commits and all temporal option flags."""
        initial_config = {
            "embedding_provider": "voyage-ai",
            "embedding_providers": ["voyage-ai"],
        }
        repo_dir = _make_repo(tmp_path, initial_config)

        from code_indexer.server.mcp.handlers import _provider_temporal_index_job

        captured_cmds = []

        def fake_run_popen(command, **kwargs):
            captured_cmds.append(command)

        temporal_opts = {
            "diff_context": 7,
            "all_branches": True,
            "max_commits": 50,
            "since_date": "2024-01-01",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=fake_run_popen,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _mock_server_config()

            _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
                temporal_options=temporal_opts,
            )

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "--index-commits" in cmd
        assert "--diff-context" in cmd
        diff_idx = cmd.index("--diff-context")
        assert cmd[diff_idx + 1] == "7"
        assert "--all-branches" in cmd
        assert "--max-commits" in cmd
        max_idx = cmd.index("--max-commits")
        assert cmd[max_idx + 1] == "50"
        assert "--since" in cmd
        since_idx = cmd.index("--since")
        assert cmd[since_idx + 1] == "2024-01-01"

    def test_provider_temporal_index_job_command_no_temporal_options(self, tmp_path):
        """AC-3: Without temporal_options, command has --index-commits but no option flags."""
        initial_config = {"embedding_provider": "voyage-ai"}
        repo_dir = _make_repo(tmp_path, initial_config)

        from code_indexer.server.mcp.handlers import _provider_temporal_index_job

        captured_cmds = []

        def fake_run_popen(command, **kwargs):
            captured_cmds.append(command)

        with (
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=fake_run_popen,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _mock_server_config()

            _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
            )

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "--index-commits" in cmd
        assert "--diff-context" not in cmd
        assert "--all-branches" not in cmd
        assert "--max-commits" not in cmd
        assert "--since" not in cmd


# ---------------------------------------------------------------------------
# AC-4: AddIndexRequest.providers description includes temporal
# ---------------------------------------------------------------------------


class TestProvidersFieldDescriptionIncludesTemporal:
    """providers field description must mention temporal (Story #641)."""

    def test_providers_field_description_includes_temporal(self):
        """AC-4: AddIndexRequest.providers description references Story #641 or temporal."""
        from code_indexer.server.models.jobs import AddIndexRequest

        schema = AddIndexRequest.model_json_schema()
        providers_description = (
            schema.get("properties", {}).get("providers", {}).get("description", "")
        )
        assert (
            "temporal" in providers_description.lower()
            or "641" in providers_description
        )
