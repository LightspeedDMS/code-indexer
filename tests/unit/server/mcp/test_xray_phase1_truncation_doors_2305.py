from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator
from unittest.mock import patch

import pytest

from tests.unit.server.mcp.xray_door_env_support import (
    REPO_ALIAS,
    DoorEnv,
    xray_door_env,
)
from tests.unit.server.services.search_event_recording_harness import (
    ALWAYS_MATCH_EVALUATOR,
    make_user,
)

pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

_FILE_COUNT = 30
_SMALL_READ_CEILING = 2048


def _write_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(_FILE_COUNT):
        body = "".join(f"value_{j} = {i}\n" for j in range(20))
        (repo / f"m{i:03d}.py").write_text(body)
    return repo


@pytest.fixture
def door_env(tmp_path) -> Iterator[DoorEnv]:
    from tests.unit.xray.conftest import require_xray_cli_binary

    require_xray_cli_binary()
    with patch(
        "code_indexer.global_repos.regex_search._MAX_READ_BYTES", _SMALL_READ_CEILING
    ):
        with xray_door_env(tmp_path, _write_repo(tmp_path)) as env:
            yield env


def _assert_cut(result: Dict[str, Any]) -> None:
    assert result.get("partial") is True, result
    assert result.get("phase1_truncated") is True, result
    assert result.get("phase1_truncation_reason") == "read_limit", result
    assert result["files_total"] is None, result
    assert 0 < result["phase1_candidate_files"] < _FILE_COUNT, result


_SEARCH_ARGS = {
    "repository_alias": REPO_ALIAS,
    "pattern": "value_",
    "evaluator_code": ALWAYS_MATCH_EVALUATOR,
    "search_target": "content",
    "await_seconds": 30,
}


def test_mcp_xray_search_reports_cut(door_env):
    from code_indexer.server.mcp.handlers.xray import handle_xray_search

    _assert_cut(door_env.mcp(handle_xray_search, _SEARCH_ARGS, "alice"))


def test_mcp_xray_explore_reports_cut(door_env):
    from code_indexer.server.mcp.handlers.xray import handle_xray_explore

    _assert_cut(door_env.mcp(handle_xray_explore, _SEARCH_ARGS, "carol"))


def test_rest_xray_search_reports_cut(door_env):
    _assert_cut(
        door_env.rest_search(
            {
                "repository_alias": REPO_ALIAS,
                "driver_regex": "value_",
                "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                "search_target": "content",
            },
            "bob",
        )
    )


def test_batch_reports_cut_cell(door_env):
    from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

    response = handle_xray_search_batch(
        {
            "repository_alias": [REPO_ALIAS],
            "scans": [
                {
                    "driver_regex": "value_",
                    "evaluator_code": ALWAYS_MATCH_EVALUATOR,
                    "search_target": "content",
                }
            ],
        },
        make_user("erin"),
    )
    body = json.loads(response["content"][0]["text"])
    assert "job_id" in body, body
    door_env.bjm.run_pending()
    result = dict(door_env.bjm.results[body["job_id"]])

    assert result["partial"] is True, result
    assert result.get("phase1_truncated") is True, result
    assert result.get("phase1_truncated_cells") == [
        {
            "repository_alias": REPO_ALIAS,
            "scan_index": 0,
            "reason": "read_limit",
        }
    ], result
