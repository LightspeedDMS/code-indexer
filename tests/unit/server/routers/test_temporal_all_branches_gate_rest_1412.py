# ruff: noqa: F811
"""Unit tests for Story #1412 - REST golden-repo registration must reject
all_branches=true when the temporal_all_branches_enabled gate is off.

POST /api/admin/golden-repos.
"""

from unittest.mock import Mock, patch

from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
    admin_client,  # noqa: F401
)


def _make_gate_config(enabled: bool):
    mock_svc = Mock()
    mock_indexing = Mock()
    mock_indexing.temporal_all_branches_enabled = enabled
    mock_server_cfg = Mock()
    mock_server_cfg.indexing_config = mock_indexing
    mock_svc.get_config.return_value = mock_server_cfg
    return mock_svc


class TestAddGoldenRepoRestGateOffRejectsAllBranches:
    """AC2/Scenario 2: gate off + temporal_options.all_branches=true -> 400."""

    def test_gate_off_all_branches_true_returns_400(self, admin_client):
        handler = _find_route_handler("/api/admin/golden-repos", "POST")
        mock_grm = Mock()

        with (
            _patch_closure(handler, "golden_repo_manager", mock_grm),
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=_make_gate_config(False),
            ),
        ):
            response = admin_client.post(
                "/api/admin/golden-repos",
                json={
                    "repo_url": "git@github.com:org/repo.git",
                    "alias": "my-repo",
                    "temporal_options": {"all_branches": True},
                },
            )

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "temporal_all_branches_enabled" in detail
        mock_grm.add_golden_repo.assert_not_called()

    def test_gate_off_no_temporal_options_submits_normally(self, admin_client):
        handler = _find_route_handler("/api/admin/golden-repos", "POST")
        mock_grm = Mock()
        mock_grm.add_golden_repo.return_value = "job-123"

        with (
            _patch_closure(handler, "golden_repo_manager", mock_grm),
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=_make_gate_config(False),
            ),
        ):
            response = admin_client.post(
                "/api/admin/golden-repos",
                json={
                    "repo_url": "git@github.com:org/repo.git",
                    "alias": "my-repo",
                },
            )

        assert response.status_code == 202
        mock_grm.add_golden_repo.assert_called_once()


class TestAddGoldenRepoRestGateOnAcceptsAllBranches:
    """AC6/Scenario 6: gate on + temporal_options.all_branches=true -> accepted."""

    def test_gate_on_all_branches_true_submits_job(self, admin_client):
        handler = _find_route_handler("/api/admin/golden-repos", "POST")
        mock_grm = Mock()
        mock_grm.add_golden_repo.return_value = "job-456"

        with (
            _patch_closure(handler, "golden_repo_manager", mock_grm),
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=_make_gate_config(True),
            ),
        ):
            response = admin_client.post(
                "/api/admin/golden-repos",
                json={
                    "repo_url": "git@github.com:org/repo.git",
                    "alias": "my-repo",
                    "temporal_options": {"all_branches": True},
                },
            )

        assert response.status_code == 202
        data = response.json()
        assert data["job_id"] == "job-456"
        mock_grm.add_golden_repo.assert_called_once()
        call_kwargs = mock_grm.add_golden_repo.call_args.kwargs
        assert call_kwargs["temporal_options"]["all_branches"] is True
