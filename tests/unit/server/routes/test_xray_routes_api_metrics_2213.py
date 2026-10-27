from __future__ import annotations

from contextlib import contextmanager
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.services.api_metrics_service import api_metrics_service
from tests.unit.server.routes.test_xray_routes import (
    NORMAL_USER,
    VALID_BATCH_BODY,
    VALID_BODY,
    _patch_batch_arm_grm,
    _patch_batch_bjm,
    _patch_batch_cidx_meta,
    _patch_batch_repo_found,
    _patch_bjm,
    _patch_repo_found,
    _patch_repo_not_found,
)

_THREE_ALIASES = ["a-global", "b-global", "c-global"]


def _admin_bypass() -> MagicMock:
    access = MagicMock()
    access.is_admin_user.return_value = True
    return access


@pytest.fixture(autouse=True)
def _batch_access_bypass():
    with patch(
        "code_indexer.server.mcp.handlers.xray_batch._get_access_filtering_service",
        return_value=_admin_bypass(),
    ):
        yield


@pytest.fixture
def xray_client() -> Generator[TestClient, None, None]:
    from code_indexer.server.routes.xray_routes import router as xray_router

    test_app = FastAPI()
    test_app.include_router(xray_router)
    test_app.state.access_filtering_service = _admin_bypass()
    test_app.dependency_overrides[get_current_user] = lambda: NORMAL_USER
    yield TestClient(test_app, raise_server_exceptions=True)
    test_app.dependency_overrides.clear()


# ^ why patch.object on the singleton, not one module's name; it sees route and handler calls together => double counts show
@contextmanager
def _xray_counter() -> Generator[MagicMock, None, None]:
    with (
        patch.object(api_metrics_service, "increment_xray_search") as counter,
        patch.object(api_metrics_service, "increment_other_api_call") as other,
    ):
        counter.other_api = other
        yield counter


class TestRestXraySearchCounts:
    def test_accepted_search_counts_one(self, xray_client):
        bjm_patch, _bjm = _patch_bjm("job-1")
        with _xray_counter() as counter, _patch_repo_found(), bjm_patch:
            resp = xray_client.post("/api/xray/search", json=VALID_BODY)

        assert resp.status_code == 202
        counter.assert_called_once_with(username="testuser")
        counter.other_api.assert_not_called()

    def test_validation_refusal_counts_none(self, xray_client):
        bjm_patch, _bjm = _patch_bjm("job-1")
        with _xray_counter() as counter, _patch_repo_found(), bjm_patch:
            resp = xray_client.post(
                "/api/xray/search", json={**VALID_BODY, "search_target": "nowhere"}
            )

        assert resp.status_code == 422
        counter.assert_not_called()

    def test_unknown_repository_counts_none(self, xray_client):
        bjm_patch, _bjm = _patch_bjm("job-1")
        with _xray_counter() as counter, _patch_repo_not_found(), bjm_patch:
            resp = xray_client.post("/api/xray/search", json=VALID_BODY)

        assert resp.status_code == 404
        counter.assert_not_called()

    def test_accepted_search_whose_submission_fails_still_counts(self, xray_client):
        bjm_patch, bjm = _patch_bjm("job-1")
        bjm.submit_job.side_effect = RuntimeError("job store down")
        with _xray_counter() as counter, _patch_repo_found(), bjm_patch:
            with pytest.raises(RuntimeError, match="job store down"):
                xray_client.post("/api/xray/search", json=VALID_BODY)

        counter.assert_called_once_with(username="testuser")


class TestRestXrayBatchCounts:
    def test_batch_over_three_repositories_counts_three_not_six(self, xray_client):
        body = {**VALID_BATCH_BODY, "repository_alias": _THREE_ALIASES}
        with (
            _xray_counter() as counter,
            _patch_batch_repo_found(),
            _patch_batch_arm_grm(),
            _patch_batch_bjm("batch-1")[0],
            _patch_batch_cidx_meta(),
        ):
            resp = xray_client.post("/api/xray/search/batch", json=body)

        assert resp.status_code == 202
        assert counter.call_count == 3
        for call in counter.call_args_list:
            assert call.kwargs == {"username": "testuser"}
        counter.other_api.assert_not_called()

    def test_accepted_batch_whose_submission_fails_still_counts(self, xray_client):
        body = {**VALID_BATCH_BODY, "repository_alias": _THREE_ALIASES}
        bjm_patch, bjm = _patch_batch_bjm("batch-1")
        bjm.submit_job.side_effect = RuntimeError("job store down")
        with (
            _xray_counter() as counter,
            _patch_batch_repo_found(),
            _patch_batch_arm_grm(),
            bjm_patch,
            _patch_batch_cidx_meta(),
        ):
            with pytest.raises(RuntimeError, match="job store down"):
                xray_client.post("/api/xray/search/batch", json=body)

        assert counter.call_count == 3
        for call in counter.call_args_list:
            assert call.kwargs == {"username": "testuser"}
