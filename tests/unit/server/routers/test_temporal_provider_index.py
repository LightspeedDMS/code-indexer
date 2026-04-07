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
