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
from typing import TYPE_CHECKING, Optional, cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole

if TYPE_CHECKING:
    from code_indexer.global_repos.write_lock_manager import WriteLockManager
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
    # Story #1404: production reads this via getattr(..., "index_floor_date", None).
    # Without an explicit value, MagicMock auto-creates a truthy child mock instead
    # of None, which breaks resolve_effective_floor_date()'s max() comparison.
    cfg.temporal_indexing_config.index_floor_date = None
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

    def test_add_index_temporal_with_providers_creates_single_job(
        self, admin_test_client
    ):
        """AC-2 (Bug #648/#3 corrected): When providers specified with temporal, submit ONE job.

        Bug #648/#3: The original AC-2 test asserted one job per provider (buggy behavior).
        The CLI (cidx index --index-commits) handles all providers in sequence internally.
        Submitting N concurrent jobs caused HNSW + SQLite race conditions corrupting the index.
        Fix: append all providers to config, then submit exactly ONE job with operation_type
        'provider_temporal_index_rebuild'.
        """
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/indexes", "POST")

        mock_bgm = MagicMock()
        mock_bgm.submit_job.return_value = "job-temporal-single"

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
        # Bug #648/#3 fix: ONE job, not N (one-per-provider was causing index corruption)
        assert mock_bgm.submit_job.call_count == 1
        submitted_op_type = mock_bgm.submit_job.call_args.kwargs.get("operation_type")
        assert submitted_op_type == "provider_temporal_index_rebuild"

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
        # Bug #1316: the route now resolves temporal_options authoritatively
        # via get_golden_repo(), not the raw golden_repos cache dict above --
        # configure it to mirror the same repo metadata.
        mock_grm.get_golden_repo.return_value = mock_repo_meta

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
# Bug #1316: per-provider temporal route must resolve temporal_options
# authoritatively (shared backend), not from the raw per-worker cache.
# ---------------------------------------------------------------------------


class TestBug1316ProviderTemporalOptionsAuthoritativeRead:
    """POST .../indexes (temporal + providers) must not trust a stale/missing
    `golden_repo_manager.golden_repos` cache entry for temporal_options."""

    def test_stale_cache_does_not_override_fresh_authoritative_temporal_options(
        self, admin_test_client
    ):
        """Bug #1316: a cross-node temporal_options mutation must be visible.

        `golden_repo_manager.golden_repos["my-repo"]` simulates THIS worker's
        stale cache (still holding max_commits=100 from before another
        node/worker saved fresh options). `get_golden_repo()` simulates the
        shared backend's CURRENT authoritative row (max_commits=999). The
        route must forward the FRESH options, not the stale cached ones.
        """
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/indexes", "POST")

        mock_bgm = MagicMock()
        mock_bgm.submit_job.return_value = "job-temporal-fresh"

        stale_repo_meta = MagicMock(temporal_options={"max_commits": 100})
        fresh_repo_meta = MagicMock(temporal_options={"max_commits": 999})

        mock_grm = MagicMock()
        mock_grm.golden_repos = {"my-repo": stale_repo_meta}
        mock_grm.get_golden_repo.return_value = fresh_repo_meta

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
        assert submitted_kwargs.get("temporal_options") == {"max_commits": 999}, (
            "Bug #1316: must forward the FRESH authoritative temporal_options, "
            f"not the stale per-worker cache. Got: {submitted_kwargs.get('temporal_options')}"
        )

    def test_cache_miss_still_resolves_temporal_options_via_authoritative_read(
        self, admin_test_client
    ):
        """Bug #1316: a cache MISS (repo registered on another node) must
        still resolve temporal_options via the shared backend rather than
        silently submitting an empty options dict."""
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/indexes", "POST")

        mock_bgm = MagicMock()
        mock_bgm.submit_job.return_value = "job-temporal-miss"

        fresh_repo_meta = MagicMock(temporal_options={"since_date": "2024-01-01"})

        mock_grm = MagicMock()
        # Cache MISS: alias absent from the raw per-worker dict entirely.
        mock_grm.golden_repos = {}
        mock_grm.get_golden_repo.return_value = fresh_repo_meta

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
        assert submitted_kwargs.get("temporal_options") == {
            "since_date": "2024-01-01"
        }, (
            "Bug #1316: a cache MISS must still resolve temporal_options "
            f"authoritatively, not emit {{}}. Got: {submitted_kwargs.get('temporal_options')}"
        )


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
            patch(
                "code_indexer.server.mcp.handlers._utils._get_app_refresh_scheduler",
                return_value=None,
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
        assert "--since-date" in cmd
        since_idx = cmd.index("--since-date")
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
            patch(
                "code_indexer.server.mcp.handlers._utils._get_app_refresh_scheduler",
                return_value=None,
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


# ---------------------------------------------------------------------------
# Bug #648 fixes
# ---------------------------------------------------------------------------


class TestBug648SingleTemporalJob:
    """Bug #648 Fix #3: Only ONE temporal job submitted regardless of provider count.

    Previously N jobs (one per provider) were submitted concurrently, causing
    index corruption. The CLI already handles all providers internally.
    """

    def test_only_one_temporal_job_submitted_for_two_providers(self, admin_test_client):
        """Bug #3: Exactly one job submitted when temporal rebuild has 2 providers.

        The old code submitted 2 jobs (one per provider). The fix must submit
        exactly ONE job because the CLI handles all providers internally.
        """
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/indexes", "POST")

        mock_bgm = MagicMock()
        mock_bgm.submit_job.return_value = "job-temporal-single"

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
        # Bug #3 fix: exactly ONE job, not one per provider
        assert mock_bgm.submit_job.call_count == 1, (
            f"Expected 1 temporal job, got {mock_bgm.submit_job.call_count}. "
            "Each provider was running its own full CLI (race condition / corruption)."
        )


class TestBug648SingleSemanticJob:
    """Bug #648 Fix #4: Only ONE semantic job submitted regardless of provider count.

    The same per-provider loop exists for semantic indexing with a fixed
    operation_type='provider_index_add' causing silent conflict-detection drops
    for 2nd+ providers. Fix: submit one job, all providers appended to config first.
    """

    def test_only_one_semantic_job_submitted_for_two_providers(self, admin_test_client):
        """Bug #4: Exactly one job submitted when semantic rebuild has 2 providers.

        The old code submitted 2 jobs, but the fixed operation_type de-duplicated
        caused the 2nd to be silently dropped. Fix: submit exactly 1 job.
        """
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/indexes", "POST")

        mock_bgm = MagicMock()
        mock_bgm.submit_job.return_value = "job-semantic-single"

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
                "code_indexer.server.mcp.handlers._provider_index_job",
            ),
            _patch_closure(handler, "background_job_manager", mock_bgm),
            _patch_closure(handler, "golden_repo_manager", mock_grm),
        ):
            response = admin_test_client.post(
                "/api/admin/golden-repos/my-repo/indexes",
                json={
                    "index_types": ["semantic"],
                    "providers": ["voyage-ai", "cohere"],
                },
            )

        assert response.status_code == 202
        # Bug #4 fix: exactly ONE job, not one per provider
        assert mock_bgm.submit_job.call_count == 1, (
            f"Expected 1 semantic job, got {mock_bgm.submit_job.call_count}. "
            "Per-provider loop submits duplicates (2nd silently dropped or races)."
        )


class TestBug648EnableTemporalFlag:
    """Bug #648 Fix #1: enable_temporal flag set to True after _provider_temporal_index_job succeeds."""

    def test_enable_temporal_flag_set_after_provider_temporal_job_succeeds(
        self, tmp_path
    ):
        """Bug #1: After successful _provider_temporal_index_job, enable_temporal=True written to DB.

        Previously the flag was never set because 'temporal' was removed from
        remaining_index_types before the flag-setting call. Fix: set flag inside
        _provider_temporal_index_job after CLI succeeds.
        """
        initial_config = {
            "embedding_provider": "voyage-ai",
            "embedding_providers": ["voyage-ai", "cohere"],
        }
        repo_dir = _make_repo(tmp_path, initial_config)

        from code_indexer.server.mcp.handlers import _provider_temporal_index_job

        mock_grm = MagicMock()
        mock_grm._sqlite_backend.update_enable_temporal.return_value = True

        with (
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch("code_indexer.server.mcp.handlers.GlobalRegistry"),
            patch(
                "code_indexer.server.services.sqlite_log_handler.SQLiteLogHandler.emit"
            ),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _mock_server_config()
            mock_app_module.golden_repo_manager = mock_grm

            result = _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
                repo_alias="my-repo",
            )

        assert result.get("success") is True
        # Bug #1 fix: enable_temporal must be updated in the SQLite backend
        mock_grm._sqlite_backend.update_enable_temporal.assert_called_once_with(
            "my-repo", True
        )


class TestBug648GlobalRegistryUpdate:
    """Bug #648 Codex review Finding #1: _set_enable_temporal_flag must update GlobalRegistry.

    The function only updated golden_repos_metadata but NOT the global_repos table
    via GlobalRegistry.  Mirror exactly the pattern from golden_repo_manager.py:2780-2812.
    """

    def test_set_enable_temporal_flag_also_updates_global_registry(self, tmp_path):
        """Finding #1: GlobalRegistry.update_enable_temporal called with '{alias}-global'.

        After _provider_temporal_index_job succeeds, _set_enable_temporal_flag must:
        1. Update golden_repos_metadata via grm._sqlite_backend (existing behaviour).
        2. ALSO update global_repos via GlobalRegistry._sqlite_backend with alias+'-global'.
        """
        initial_config = {
            "embedding_provider": "voyage-ai",
            "embedding_providers": ["voyage-ai"],
        }
        repo_dir = _make_repo(tmp_path, initial_config)

        from code_indexer.server.mcp.handlers import _provider_temporal_index_job

        mock_grm = MagicMock()
        mock_grm._sqlite_backend.update_enable_temporal.return_value = True
        mock_grm.data_dir = str(tmp_path)

        mock_global_registry_instance = MagicMock()
        mock_global_registry_instance._sqlite_backend = MagicMock()
        mock_global_registry_instance._sqlite_backend.update_enable_temporal.return_value = True

        with (
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch(
                "code_indexer.server.mcp.handlers.GlobalRegistry",
                return_value=mock_global_registry_instance,
            ),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _mock_server_config()
            mock_app_module.golden_repo_manager = mock_grm

            result = _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
                repo_alias="my-repo",
            )

        assert result.get("success") is True
        # Finding #1: GlobalRegistry backend must also be updated with alias + '-global'
        mock_global_registry_instance._sqlite_backend.update_enable_temporal.assert_called_once_with(
            "my-repo-global", True
        )

    def test_set_enable_temporal_flag_uses_module_logger(self, tmp_path, caplog):
        """Finding #2: _set_enable_temporal_flag uses module-level logger, not root logging.

        The function must emit log records via the 'code_indexer.server.mcp.handlers'
        logger (logger.info / logger.warning), not via the root logging.info /
        logging.warning calls which bypass structured logging configuration.
        """
        import logging as stdlib_logging

        initial_config = {
            "embedding_provider": "voyage-ai",
            "embedding_providers": ["voyage-ai"],
        }
        repo_dir = _make_repo(tmp_path, initial_config)

        from code_indexer.server.mcp.handlers import _provider_temporal_index_job

        mock_grm = MagicMock()
        mock_grm._sqlite_backend.update_enable_temporal.return_value = True
        mock_grm.data_dir = str(tmp_path)

        mock_global_registry_instance = MagicMock()
        mock_global_registry_instance._sqlite_backend = MagicMock()
        mock_global_registry_instance._sqlite_backend.update_enable_temporal.return_value = True

        with (
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch(
                "code_indexer.server.mcp.handlers.GlobalRegistry",
                return_value=mock_global_registry_instance,
            ),
            patch(
                "code_indexer.server.services.sqlite_log_handler.SQLiteLogHandler.emit"
            ),
            caplog.at_level(
                stdlib_logging.INFO, logger="code_indexer.server.mcp.handlers"
            ),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _mock_server_config()
            mock_app_module.golden_repo_manager = mock_grm

            _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
                repo_alias="my-repo",
            )

        # Finding #2 (positive): log records must come from the module-level logger
        # (handlers package split: logger may be handlers or handlers._legacy)
        handler_log_records = [
            r
            for r in caplog.records
            if r.name.startswith("code_indexer.server.mcp.handlers")
            and "enable_temporal" in r.message
        ]
        assert len(handler_log_records) >= 1, (
            "Expected at least one enable_temporal log record from the module-level "
            "'code_indexer.server.mcp.handlers' logger (or handlers._legacy submodule). "
            "Likely cause: _set_enable_temporal_flag still uses root logging.info/warning "
            "instead of logger.info/warning."
        )
        # Finding #2 (negative): root logger must NOT receive enable_temporal messages
        root_log_records = [
            r
            for r in caplog.records
            if r.name == "root" and "enable_temporal" in r.message
        ]
        assert not root_log_records, (
            "Root logger must not be used for enable_temporal messages. "
            "Replace logging.info/warning with logger.info/warning in _set_enable_temporal_flag."
        )


class TestBug1373AliasSuffixMismatch:
    """Bug #1373: _set_enable_temporal_flag must normalize an already
    '-global'-suffixed repo_alias instead of using it as-is.

    _provider_temporal_index_job is invoked with the route-level alias,
    which is already the '-global' form when the admin targets the global
    repo (inline_admin_ops.py temporal job submission). Before the fix,
    calling with "evolution-global" caused:
      - golden_repos_metadata update with alias="evolution-global" (0 rows
        matched -- that table is keyed by the BARE alias "evolution")
      - global_repos update with alias_name="evolution-global-global" (0
        rows matched -- double-suffixed, matches nothing)
    Both failures silently no-op, permanently misreporting enable_temporal
    as False even though the temporal index was built successfully.
    """

    def test_set_enable_temporal_flag_normalizes_already_suffixed_alias(self):
        """Calling with an already '-global'-suffixed alias must update
        golden_repos_metadata with the BARE alias and global_repos with the
        SINGLE '-global'-suffixed alias (never bare, never double-suffixed)."""
        from code_indexer.server.mcp.handlers.repos import (
            _set_enable_temporal_flag,
        )

        mock_grm = MagicMock()
        mock_grm._sqlite_backend.update_enable_temporal.return_value = True
        mock_grm.data_dir = "/tmp/bug-1373-fake-data-dir"
        stale_repo_meta = MagicMock(enable_temporal=False)
        mock_grm.golden_repos = {"evolution": stale_repo_meta}
        # Bug #1481: the cache entry is now REPLACED wholesale with the
        # fresh object returned by get_golden_repo() (reflecting the
        # backend write that just succeeded above), not patched in place.
        fresh_repo_meta = MagicMock(enable_temporal=True)
        mock_grm.get_golden_repo.return_value = fresh_repo_meta

        mock_global_registry_instance = MagicMock()
        mock_global_registry_instance._sqlite_backend = MagicMock()
        mock_global_registry_instance._sqlite_backend.update_enable_temporal.return_value = True

        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch(
                "code_indexer.server.mcp.handlers.GlobalRegistry",
                return_value=mock_global_registry_instance,
            ),
        ):
            mock_app_module.golden_repo_manager = mock_grm

            _set_enable_temporal_flag("evolution-global")

        # golden_repos_metadata is keyed by the BARE alias -- must receive
        # "evolution", never the suffixed input "evolution-global".
        mock_grm._sqlite_backend.update_enable_temporal.assert_called_once_with(
            "evolution", True
        )
        # global_repos is keyed by the '-global'-suffixed alias -- must
        # receive exactly "evolution-global", never "evolution-global-global".
        mock_global_registry_instance._sqlite_backend.update_enable_temporal.assert_called_once_with(
            "evolution-global", True
        )
        # In-memory cache (keyed bare) must reflect the update too -- full-object
        # replacement via get_golden_repo(), not the old single-field mutation
        # of whatever was already cached (Bug #1481). Assert identity against
        # the fresh object get_golden_repo() returns, not merely the boolean
        # field -- a bare field-mutation fix would also satisfy a field-only
        # assertion, but would NOT satisfy this identity check.
        assert (
            mock_grm.golden_repos["evolution"] is mock_grm.get_golden_repo.return_value
        )
        assert mock_grm.golden_repos["evolution"].enable_temporal is True


class TestBug1481CrossNodeCacheColdRefresh:
    """Bug #1481: _set_enable_temporal_flag must refresh this worker's cache
    via the authoritative get_golden_repo() read, not read-and-patch a
    single field on whatever happens to already be cached locally.

    Cross-node/cross-worker scenario: the authoritative backend write
    (grm._sqlite_backend.update_enable_temporal) just succeeded, but this
    worker's own `golden_repos` cache never held this alias (e.g. another
    node/worker registered or last touched it). The old code read
    `grm.golden_repos.get(bare_alias)` -- None here -- and silently did
    nothing, leaving this worker's cache permanently cold despite having
    fresh, correct data in hand from get_golden_repo().
    """

    def test_cross_node_cache_populated_after_fix_bug1481(self):
        """RED (pre-fix): fails because grm.golden_repos stays empty.
        GREEN (post-fix): grm.golden_repos[bare_alias] is populated with the
        fresh object from get_golden_repo(), with enable_temporal=True.
        """
        from code_indexer.server.mcp.handlers.repos import (
            _set_enable_temporal_flag,
        )

        mock_grm = MagicMock()
        mock_grm._sqlite_backend.update_enable_temporal.return_value = True
        mock_grm.golden_repos = {}  # cross-node cold cache -- never cached

        fresh_repo_meta = MagicMock(enable_temporal=True)
        mock_grm.get_golden_repo.return_value = fresh_repo_meta

        mock_global_registry_instance = MagicMock()
        mock_global_registry_instance._sqlite_backend = MagicMock()
        mock_global_registry_instance._sqlite_backend.update_enable_temporal.return_value = True

        bare_alias = "cross-node-repo"

        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch(
                "code_indexer.server.mcp.handlers.GlobalRegistry",
                return_value=mock_global_registry_instance,
            ),
        ):
            mock_app_module.golden_repo_manager = mock_grm

            _set_enable_temporal_flag(bare_alias)

        assert bare_alias in mock_grm.golden_repos, (
            "pre-fix read-and-patch pattern leaves the cold cache empty "
            "despite fresh, correct data being available via get_golden_repo()"
        )
        assert mock_grm.golden_repos[bare_alias].enable_temporal is True
        assert mock_grm.golden_repos[bare_alias] is fresh_repo_meta


class TestBug648OrphanedSnapshotCleanup:
    """Bug #648 Fix #6: Orphaned snapshot dirs cleaned up when swap_alias raises ValueError."""

    def test_orphaned_snapshot_deleted_when_swap_alias_fails(self, tmp_path):
        """Bug #6: If swap_alias raises ValueError (old_target mismatch), new snapshot is deleted.

        When N concurrent jobs start with the same old_snapshot_path, only the
        first swap succeeds. Jobs 2..N get ValueError. Their new snapshot dirs
        must be cleaned up to prevent disk leak.
        """
        from code_indexer.server.mcp.handlers import _post_provider_index_snapshot

        # Create a fake new snapshot directory that would be orphaned
        new_snapshot_dir = tmp_path / "new_snapshot_v_12345"
        new_snapshot_dir.mkdir()
        (new_snapshot_dir / "some_index_file.json").write_text("{}")

        # Verify it exists before the test
        assert new_snapshot_dir.exists()

        mock_scheduler = MagicMock()
        mock_scheduler._create_snapshot.return_value = str(new_snapshot_dir)
        mock_scheduler.alias_manager.swap_alias.side_effect = ValueError(
            "current_target mismatch: expected old target '/old/path'"
        )

        with patch(
            "code_indexer.server.mcp.handlers._get_app_refresh_scheduler",
            return_value=mock_scheduler,
        ):
            # This should NOT raise; it should log warning and clean up
            _post_provider_index_snapshot(
                repo_alias="my-repo-global",
                base_clone_path="/some/base/clone",
                old_snapshot_path="/old/path",
            )

        # Bug #6 fix: orphaned new snapshot dir must be deleted
        assert not new_snapshot_dir.exists(), (
            "Orphaned snapshot directory was not cleaned up after swap_alias ValueError. "
            "This causes disk leak on every multi-provider rebuild."
        )


def _run_provider_temporal_job_with_lock_scheduler(
    tmp_path,
    *,
    acquire_return_value: bool,
    run_popen_side_effect=None,
    repo_alias: str = "my-repo",
    capture: Optional[dict] = None,
):
    """Shared helper for the write-lock-coordination tests below: builds a
    repo dir + mock scheduler, runs _provider_temporal_index_job with the
    scheduler's acquire_write_lock() forced to *acquire_return_value*.

    *capture* (optional), when supplied, is populated with "scheduler",
    "run_popen", and "call_order" BEFORE the job is invoked -- so callers
    that expect the job to raise can still inspect these afterward, e.g. to
    verify release_write_lock fired via a finally on the SAME failed
    invocation rather than a fresh, unrelated one. Returns the job's result
    dict (only reached when the job does not raise).
    """
    from code_indexer.server.mcp.handlers import _provider_temporal_index_job

    initial_config = {
        "embedding_provider": "voyage-ai",
        "embedding_providers": ["voyage-ai"],
    }
    repo_dir = _make_repo(tmp_path, initial_config)

    call_order = []
    mock_scheduler = MagicMock()

    def _acquire(*args, **kwargs):
        call_order.append("acquire")
        return acquire_return_value

    def _release(*args, **kwargs):
        call_order.append("release")

    mock_scheduler.acquire_write_lock.side_effect = _acquire
    mock_scheduler.release_write_lock.side_effect = _release

    def _default_run_popen(command, **kwargs):
        call_order.append("run_popen")

    with (
        patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg_svc,
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=run_popen_side_effect or _default_run_popen,
        ) as mock_run_popen,
        patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(10, 5),
        ),
        patch(
            "code_indexer.server.mcp.handlers._utils._get_app_refresh_scheduler",
            return_value=mock_scheduler,
        ),
        patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
        patch("code_indexer.server.mcp.handlers.GlobalRegistry"),
    ):
        mock_cfg_svc.return_value.get_config.return_value = _mock_server_config()
        mock_grm = MagicMock()
        mock_grm._sqlite_backend.update_enable_temporal.return_value = True
        mock_app_module.golden_repo_manager = mock_grm

        if capture is not None:
            capture["scheduler"] = mock_scheduler
            capture["run_popen"] = mock_run_popen
            capture["call_order"] = call_order

        result = _provider_temporal_index_job(
            repo_path=str(repo_dir),
            provider_name="voyage-ai",
            repo_alias=repo_alias,
        )

    return result, mock_scheduler, mock_run_popen, call_order


class TestProviderTemporalIndexJobWriteLockCoordination:
    """Cross-process race investigation (deferred from Bug #1580): the
    TemporalLegacyMigrationScheduler's mover.py relocates legacy temporal
    shards under RefreshScheduler.write_lock_manager's per-alias write lock
    (locking.guarded_by_refresh_lock). _provider_temporal_index_job spawns
    `cidx index --index-commits` for the SAME golden repo alias -- which
    runs Bug #1528's consolidate_legacy_temporal_shards() against the exact
    shard directory tree mover.py reads/writes -- WITHOUT ever acquiring
    that write lock. add_indexes_to_golden_repo (golden_repo_manager.py) and
    _execute_refresh (refresh_scheduler.py) already hold this lock for
    their entire temporal-indexing duration; _provider_temporal_index_job is
    the one sibling that does not, closing that gap here.
    """

    def test_write_lock_acquired_before_subprocess_and_released_after_success(
        self, tmp_path
    ):
        """The lock must be acquired BEFORE the subprocess spawns and
        released AFTER it completes successfully -- proving the whole
        `cidx index --index-commits` run (including Bug #1528's
        pre-index consolidate_legacy_temporal_shards) happens under
        exclusive ownership of the alias's write lock, mirroring
        add_indexes_to_golden_repo's own acquire/finally-release pattern.
        """
        result, mock_scheduler, mock_run_popen, call_order = (
            _run_provider_temporal_job_with_lock_scheduler(
                tmp_path, acquire_return_value=True
            )
        )

        assert result.get("success") is True
        mock_run_popen.assert_called_once()
        assert call_order == ["acquire", "run_popen", "release"], (
            f"expected acquire -> subprocess -> release ordering, got {call_order}"
        )
        acquire_alias = mock_scheduler.acquire_write_lock.call_args.args[0]
        release_alias = mock_scheduler.release_write_lock.call_args.args[0]
        assert acquire_alias == release_alias == "my-repo"

    def test_skips_subprocess_when_write_lock_already_held(self, tmp_path):
        """When another writer already holds the alias's write lock (e.g. a
        concurrent temporal-legacy-migration pass), _provider_temporal_index_job
        must return a failure result WITHOUT ever spawning the `cidx index`
        subprocess, and must never call release (nothing was acquired).
        """
        result, mock_scheduler, mock_run_popen, call_order = (
            _run_provider_temporal_job_with_lock_scheduler(
                tmp_path, acquire_return_value=False
            )
        )

        assert result.get("success") is False
        mock_run_popen.assert_not_called()
        mock_scheduler.release_write_lock.assert_not_called()
        assert call_order == ["acquire"]

    def test_lock_still_released_when_subprocess_raises(self, tmp_path):
        """A subprocess-side exception must not leak the write lock forever
        -- release must happen via a guaranteed `finally` on the SAME failed
        invocation, exactly like add_indexes_to_golden_repo's own
        try/finally around the lock.
        """

        def _raising_run_popen(command, **kwargs):
            raise RuntimeError("simulated subprocess failure")

        capture: dict = {}
        with pytest.raises(RuntimeError):
            _run_provider_temporal_job_with_lock_scheduler(
                tmp_path,
                acquire_return_value=True,
                run_popen_side_effect=_raising_run_popen,
                capture=capture,
            )

        assert capture["call_order"] == ["acquire", "release"], (
            "expected the lock to be released via a guaranteed finally "
            f"even though the subprocess raised; got {capture['call_order']}"
        )
        capture["scheduler"].release_write_lock.assert_called_once()
        released_alias = capture["scheduler"].release_write_lock.call_args.args[0]
        assert released_alias == "my-repo"

    def test_normalizes_global_suffixed_alias_before_acquiring_lock(self, tmp_path):
        """The write lock is keyed by the BARE alias (mover.py/refresh_scheduler
        convention) -- a route-level '-global'-suffixed alias (Bug #1373's
        documented calling shape) must be normalized before acquiring, or the
        lock would never actually collide with mover.py's bare-alias lock."""
        _, mock_scheduler, _, _ = _run_provider_temporal_job_with_lock_scheduler(
            tmp_path, acquire_return_value=False, repo_alias="my-repo-global"
        )

        acquired_alias = mock_scheduler.acquire_write_lock.call_args.args[0]
        assert acquired_alias == "my-repo", (
            "write lock must be acquired against the BARE alias so it "
            "collides with mover.py's own bare-alias lock; got "
            f"{acquired_alias!r}"
        )

    def test_proceeds_without_lock_when_no_scheduler_available(self, tmp_path):
        """Byte-identical-when-unavailable: mirrors add_indexes_to_golden_repo's
        own degrade-gracefully convention -- when no RefreshScheduler is wired
        (e.g. a deployment context without one), the job must still run rather
        than fail closed, exactly like every other pre-existing caller of
        _get_app_refresh_scheduler() in this module."""
        initial_config = {
            "embedding_provider": "voyage-ai",
            "embedding_providers": ["voyage-ai"],
        }
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
            patch(
                "code_indexer.server.mcp.handlers._utils._get_app_refresh_scheduler",
                return_value=None,
            ),
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch("code_indexer.server.mcp.handlers.GlobalRegistry"),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _mock_server_config()
            mock_grm = MagicMock()
            mock_grm._sqlite_backend.update_enable_temporal.return_value = True
            mock_app_module.golden_repo_manager = mock_grm

            result = _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="voyage-ai",
                repo_alias="my-repo",
            )

        assert result.get("success") is True
        assert len(captured_cmds) == 1


def _make_real_write_lock_fake_scheduler(write_lock_manager: "WriteLockManager"):
    """A fake scheduler backed by a REAL WriteLockManager, playing BOTH
    roles needed to prove genuine mutual exclusion through the SAME on-disk
    lock file: mover.py's locking.guarded_by_refresh_lock surface
    (write_lock_manager attribute, check_refresh_not_in_progress) and
    _provider_temporal_index_job's own surface (acquire_write_lock/
    release_write_lock)."""
    from types import SimpleNamespace

    def _acquire(alias: str, owner_name: str, ttl_seconds=None) -> bool:
        if ttl_seconds is not None:
            return cast(
                bool,
                write_lock_manager.acquire(
                    alias, owner_name=owner_name, ttl_seconds=ttl_seconds
                ),
            )
        return cast(bool, write_lock_manager.acquire(alias, owner_name=owner_name))

    def _release(alias: str, owner_name: str, owner_token=None) -> bool:
        released = cast(
            bool,
            write_lock_manager.release(
                alias, owner_name=owner_name, owner_token=owner_token
            ),
        )
        if not released:
            raise AssertionError(
                f"fake scheduler: release_write_lock({alias!r}) returned "
                "False -- owner mismatch, lock was not actually held"
            )
        return released

    return SimpleNamespace(
        write_lock_manager=write_lock_manager,
        check_refresh_not_in_progress=lambda alias: None,
        acquire_write_lock=_acquire,
        release_write_lock=_release,
    )


class TestProviderTemporalIndexJobRealWriteLockConcurrency:
    """Real (non-mocked) file-based WriteLockManager concurrency proof:
    mover.py's TemporalLegacyMigrationScheduler holds the alias's write lock
    via locking.guarded_by_refresh_lock while _provider_temporal_index_job
    attempts the same alias -- proving genuine mutual exclusion through the
    SAME on-disk lock file, not two mocks that merely look consistent.
    """

    def test_real_write_lock_held_by_mover_blocks_provider_temporal_job(self, tmp_path):
        from code_indexer.global_repos.write_lock_manager import WriteLockManager
        from code_indexer.server.services.temporal_legacy_migration.locking import (
            guarded_by_refresh_lock,
        )
        from code_indexer.server.mcp.handlers import _provider_temporal_index_job

        real_lock_manager = WriteLockManager(tmp_path / "golden-repos")
        repo_dir = _make_repo(
            tmp_path,
            {
                "embedding_provider": "voyage-ai",
                "embedding_providers": ["voyage-ai"],
            },
        )
        fake_scheduler = _make_real_write_lock_fake_scheduler(real_lock_manager)

        with guarded_by_refresh_lock(fake_scheduler, "my-repo"):
            with (
                patch(
                    "code_indexer.server.mcp.handlers.get_config_service"
                ) as mock_cfg_svc,
                patch(
                    "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                ) as mock_run_popen,
                patch(
                    "code_indexer.server.mcp.handlers._utils._get_app_refresh_scheduler",
                    return_value=fake_scheduler,
                ),
            ):
                mock_cfg_svc.return_value.get_config.return_value = (
                    _mock_server_config()
                )
                result = _provider_temporal_index_job(
                    repo_path=str(repo_dir),
                    provider_name="voyage-ai",
                    repo_alias="my-repo",
                )

        assert result.get("success") is False, (
            "the real, file-based write lock mover.py holds must genuinely "
            "block _provider_temporal_index_job from proceeding"
        )
        mock_run_popen.assert_not_called()
        assert real_lock_manager.is_locked("my-repo") is False


class TestFinding1PodPullMetadataMissingRepoAlias:
    """Round 3, Finding 1: repo_alias must survive the pod-pull metadata
    round trip from submission (inline_admin_ops.py) to reconstruction
    (lifespan.py's _pp_provider_temporal / IndexJobClaimLoop).

    Round 2 made golden_repo_write_lock_guard raise ValueError whenever a
    RefreshScheduler is wired but the alias passed to it is blank. The
    submission route never put repo_alias into the persisted `metadata`
    dict, so a different cluster node reconstructing this job purely from
    that dict -- exactly what _pp_provider_temporal does via
    `_provider_temporal_index_job(**metadata, progress_callback=cb)` --
    would receive repo_alias="" and hit that ValueError even though the
    alias was real and valid at submission time.
    """

    def test_repo_alias_survives_metadata_round_trip_to_locking_guard(
        self, tmp_path, admin_test_client
    ):
        """End-to-end round trip: real route builds `metadata`, then the
        real reconstruction one-liner from lifespan.py feeds it into the
        real _provider_temporal_index_job + real golden_repo_write_lock_guard.
        Must not raise, and must acquire the lock under the correct bare
        alias -- proving repo_alias survived the persisted-metadata trip.
        """
        repo_dir = _make_repo(
            tmp_path,
            {
                "embedding_provider": "voyage-ai",
                "embedding_providers": ["voyage-ai"],
            },
        )

        # --- Step 1: capture the REAL metadata dict the route builds ---
        handler = _find_route_handler("/api/admin/golden-repos/{alias}/indexes", "POST")

        mock_bgm = MagicMock()
        mock_bgm.submit_job.return_value = "job-roundtrip"

        mock_grm = MagicMock()
        mock_grm.golden_repos = {"my-repo": MagicMock(temporal_options={})}
        mock_grm.get_golden_repo.return_value = MagicMock(temporal_options={})

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
                return_value=str(repo_dir),
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
        captured_metadata = dict(mock_bgm.submit_job.call_args.kwargs.get("metadata"))
        assert captured_metadata.get("repo_alias") == "my-repo", (
            "the persisted pod-pull metadata dict must carry repo_alias so a "
            "different cluster node can reconstruct this job -- got: "
            f"{captured_metadata!r}"
        )

        # --- Step 2: run that metadata through the REAL reconstruction ---
        # This is lifespan.py's _pp_provider_temporal one-liner, verbatim:
        #   return _provider_temporal_index_job(**md, progress_callback=cb)
        from code_indexer.server.mcp.handlers import (
            _provider_temporal_index_job as real_job,
        )

        def _pp_provider_temporal_mirror(md, progress_callback):
            return real_job(**md, progress_callback=progress_callback)

        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True
        mock_scheduler.release_write_lock.return_value = True

        with (
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_cfg_svc,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            ) as mock_run_popen,
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(1, 1),
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils._get_app_refresh_scheduler",
                return_value=mock_scheduler,
            ),
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch("code_indexer.server.mcp.handlers.GlobalRegistry"),
        ):
            mock_cfg_svc.return_value.get_config.return_value = _mock_server_config()
            fake_grm = MagicMock()
            fake_grm._sqlite_backend.update_enable_temporal.return_value = True
            mock_app_module.golden_repo_manager = fake_grm

            result = _pp_provider_temporal_mirror(
                captured_metadata, progress_callback=None
            )

        assert result.get("success") is True, (
            f"reconstructed job must succeed, got: {result!r}"
        )
        mock_run_popen.assert_called_once()
        mock_scheduler.acquire_write_lock.assert_called_once()
        acquired_alias = mock_scheduler.acquire_write_lock.call_args.args[0]
        assert acquired_alias == "my-repo", (
            "the reconstructed job must acquire the write lock under the "
            f"correct bare alias, got {acquired_alias!r}"
        )
