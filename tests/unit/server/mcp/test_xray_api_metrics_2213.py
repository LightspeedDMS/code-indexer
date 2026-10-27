from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import UserRole
from tests.unit.server.mcp.test_xray_multi_repo_bug1074 import (
    MULTI_REPO_EXPLORE_PARAMS,
    MULTI_REPO_SEARCH_PARAMS,
    _make_user,
    _parse_response,
    _patched_xray_env_multi,
)

_HANDLER_PARAMS = [
    ("handle_xray_search", "_search", MULTI_REPO_SEARCH_PARAMS),
    ("handle_xray_explore", "_explore", MULTI_REPO_EXPLORE_PARAMS),
]


@contextmanager
def _metrics_for(module_name: str) -> Generator[MagicMock, None, None]:
    with patch(
        f"code_indexer.server.mcp.handlers.xray.{module_name}.api_metrics_service"
    ) as mock_metrics:
        yield mock_metrics


async def _run(
    handler_name: str,
    module_name: str,
    params: Dict[str, Any],
    futures: int,
    resolve: Optional[Any] = None,
) -> "tuple[Dict[str, Any], MagicMock]":
    import code_indexer.server.mcp.handlers.xray as xray_mod

    with _patched_xray_env_multi(success_future_count=futures):
        with _metrics_for(module_name) as mock_metrics:
            if resolve is None:
                result = await getattr(xray_mod, handler_name)(
                    dict(params), _make_user(UserRole.NORMAL_USER)
                )
            else:
                with patch(
                    f"code_indexer.server.mcp.handlers.xray.{module_name}._resolve_repo_path",
                    side_effect=resolve,
                ):
                    result = await getattr(xray_mod, handler_name)(
                        dict(params), _make_user(UserRole.NORMAL_USER)
                    )
    return _parse_response(result), mock_metrics


class TestXraySearchHandlersCountXray:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name,module_name,params", _HANDLER_PARAMS)
    async def test_two_repositories_count_two(self, handler_name, module_name, params):
        body, mock_metrics = await _run(handler_name, module_name, params, futures=2)

        assert len(body["job_ids"]) == 2
        assert mock_metrics.increment_xray_search.call_count == 2
        for call in mock_metrics.increment_xray_search.call_args_list:
            assert call.kwargs == {"username": "testuser"}
        mock_metrics.increment_other_api_call.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name,module_name,params", _HANDLER_PARAMS)
    async def test_unresolvable_repository_is_not_counted(
        self, handler_name, module_name, params
    ):
        def _only_first(alias: str) -> Optional[str]:
            return "/fake/multi-repo/path" if alias == "myrepo-global" else None

        body, mock_metrics = await _run(
            handler_name, module_name, params, futures=1, resolve=_only_first
        )

        assert len(body["job_ids"]) == 1
        assert len(body["errors"]) == 1
        mock_metrics.increment_xray_search.assert_called_once_with(username="testuser")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name,module_name,params", _HANDLER_PARAMS)
    async def test_single_repository_counts_one(
        self, handler_name, module_name, params
    ):
        single = {**params, "repository_alias": "myrepo-global"}

        body, mock_metrics = await _run(handler_name, module_name, single, futures=1)

        assert "job_id" in body
        mock_metrics.increment_xray_search.assert_called_once_with(username="testuser")
        mock_metrics.increment_other_api_call.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name,module_name,params", _HANDLER_PARAMS)
    async def test_validation_refusal_is_not_counted(
        self, handler_name, module_name, params
    ):
        refused = {**params, "search_target": "nowhere"}

        body, mock_metrics = await _run(handler_name, module_name, refused, futures=0)

        assert body["error"] == "invalid_search_target"
        mock_metrics.increment_xray_search.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name,module_name,params", _HANDLER_PARAMS)
    async def test_unresolvable_single_repository_is_not_counted(
        self, handler_name, module_name, params
    ):
        single = {**params, "repository_alias": "missing-global"}

        body, mock_metrics = await _run(
            handler_name, module_name, single, futures=0, resolve=lambda _a: None
        )

        assert body["error"] == "repository_not_found"
        mock_metrics.increment_xray_search.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name,module_name,params", _HANDLER_PARAMS)
    async def test_accepted_search_whose_submission_fails_still_counts(
        self, handler_name, module_name, params
    ):
        import code_indexer.server.mcp.handlers.xray as xray_mod

        failing_tracker = MagicMock()
        failing_tracker.register_job.side_effect = RuntimeError("job store down")
        single = {**params, "repository_alias": "myrepo-global"}

        with _patched_xray_env_multi(
            mock_job_tracker=failing_tracker, success_future_count=0
        ):
            with _metrics_for(module_name) as mock_metrics:
                with pytest.raises(RuntimeError, match="job store down"):
                    await getattr(xray_mod, handler_name)(
                        single, _make_user(UserRole.NORMAL_USER)
                    )

        mock_metrics.increment_xray_search.assert_called_once_with(username="testuser")


_BATCH = "code_indexer.server.mcp.handlers.xray_batch"
_BATCH_SCANS = [
    {
        "driver_regex": r"def ",
        "evaluator_code": MULTI_REPO_SEARCH_PARAMS["evaluator_code"],
    }
]


@contextmanager
def _batch_env(resolve: Any, bjm: MagicMock) -> Generator[MagicMock, None, None]:
    from pathlib import Path

    access = MagicMock()
    access.is_admin_user.return_value = True
    with (
        patch(f"{_BATCH}._get_access_filtering_service", return_value=access),
        patch(f"{_BATCH}._resolve_repo_path", side_effect=resolve),
        patch(f"{_BATCH}._get_arm_and_grm", return_value=(None, None)),
        patch(f"{_BATCH}._get_background_job_manager", return_value=bjm),
        patch(f"{_BATCH}._get_cidx_meta_path", return_value=Path("/cidx-meta")),
        patch(f"{_BATCH}.api_metrics_service") as mock_metrics,
    ):
        yield mock_metrics


def _run_batch(
    params: Dict[str, Any], resolve: Any
) -> "tuple[Dict[str, Any], MagicMock, MagicMock]":
    from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

    bjm = MagicMock()
    bjm.submit_job.return_value = "batch-job"
    with _batch_env(resolve, bjm) as mock_metrics:
        result = handle_xray_search_batch(params, _make_user(UserRole.NORMAL_USER))
    return _parse_response(result), mock_metrics, bjm


class TestXraySearchBatchCountsXray:
    def test_three_resolved_repositories_count_three(self):
        params = {
            "repository_alias": ["a-global", "b-global", "c-global"],
            "scans": _BATCH_SCANS,
        }

        body, mock_metrics, bjm = _run_batch(params, resolve=lambda _a: "/repos/x")

        assert body == {"job_id": "batch-job"}
        bjm.submit_job.assert_called_once()
        assert mock_metrics.increment_xray_search.call_count == 3
        for call in mock_metrics.increment_xray_search.call_args_list:
            assert call.kwargs == {"username": "testuser"}
        mock_metrics.increment_other_api_call.assert_not_called()

    def test_unresolvable_repository_is_not_counted(self):
        params = {
            "repository_alias": ["a-global", "gone-global"],
            "scans": _BATCH_SCANS,
        }

        body, mock_metrics, _bjm = _run_batch(
            params, resolve=lambda a: "/repos/a" if a == "a-global" else None
        )

        assert body == {"job_id": "batch-job"}
        mock_metrics.increment_xray_search.assert_called_once_with(username="testuser")

    def test_validation_refusal_is_not_counted(self):
        params = {"repository_alias": ["a-global"], "scans": []}

        body, mock_metrics, bjm = _run_batch(params, resolve=lambda _a: "/repos/a")

        assert body["error"] == "scans_required"
        bjm.submit_job.assert_not_called()
        mock_metrics.increment_xray_search.assert_not_called()

    def test_no_repository_resolved_is_not_counted(self):
        params = {"repository_alias": ["gone-global"], "scans": _BATCH_SCANS}

        body, mock_metrics, _bjm = _run_batch(params, resolve=lambda _a: None)

        assert body["error"] == "no_repositories_resolved"
        mock_metrics.increment_xray_search.assert_not_called()

    def test_accepted_batch_whose_submission_fails_still_counts(self):
        from code_indexer.server.mcp.handlers.xray_batch import (
            handle_xray_search_batch,
        )

        params = {
            "repository_alias": ["a-global", "b-global", "c-global"],
            "scans": _BATCH_SCANS,
        }
        bjm = MagicMock()
        bjm.submit_job.side_effect = RuntimeError("job store down")

        with _batch_env(lambda _a: "/repos/x", bjm) as mock_metrics:
            with pytest.raises(RuntimeError, match="job store down"):
                handle_xray_search_batch(params, _make_user(UserRole.NORMAL_USER))

        assert mock_metrics.increment_xray_search.call_count == 3
        for call in mock_metrics.increment_xray_search.call_args_list:
            assert call.kwargs == {"username": "testuser"}
