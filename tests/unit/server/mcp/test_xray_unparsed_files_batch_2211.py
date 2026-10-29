"""Bug #2211: files the engine cannot parse, through the real xray_search_batch door."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

from tests.unit.server.mcp.xray_door_env_support import REPO_ALIAS, xray_door_env

pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")

_NEVER_MATCH = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    "    let _ = node;\n"
    "    Vec::new()\n"
    "}\n"
)
_DOOMED_NAME = "b.py"


def _run_batch(
    tmp_path: Path, files: Dict[str, str], delete: bool = True
) -> Dict[str, Any]:
    from tests.unit.xray.conftest import require_xray_cli_binary

    require_xray_cli_binary()
    from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch
    from code_indexer.xray.rust_backend import RustNativeBackend
    from tests.unit.server.services.search_event_recording_harness import make_user

    repo = tmp_path / "repo"
    repo.mkdir()
    for name, content in files.items():
        (repo / name).write_text(content)
    doomed = repo / _DOOMED_NAME
    original_run_batch = RustNativeBackend.run_batch

    # ^ order unlink after Phase 1 and language detection, before run_batch spawns xray-cli; nothing else spawns between
    def _delete_then_run(self: Any, *args: Any, **kwargs: Any) -> Any:
        if delete:
            doomed.unlink()
        return original_run_batch(self, *args, **kwargs)

    seam = patch.object(
        RustNativeBackend, "run_batch", autospec=True, side_effect=_delete_then_run
    )
    with xray_door_env(tmp_path, repo) as env:
        with seam:
            response = handle_xray_search_batch(
                {
                    "repository_alias": [REPO_ALIAS],
                    "scans": [
                        {
                            "driver_regex": "[xy] =",
                            "evaluator_code": _NEVER_MATCH,
                            "search_target": "content",
                        }
                    ],
                },
                make_user("erin"),
            )
            body = json.loads(response["content"][0]["text"])
            assert "job_id" in body, body
            env.bjm.run_pending()
        assert not (delete and doomed.exists())
        return dict(env.bjm.results[body["job_id"]])


def test_batch_cell_with_partial_unparse_counts_as_run(tmp_path):
    from code_indexer.server.services.search_event_outcomes import (
        xray_batch_repository_rows,
        xray_result_outcome,
    )

    result = _run_batch(tmp_path, {"a.py": "x = 1\n", _DOOMED_NAME: "y = 2\n"})

    assert xray_result_outcome(result) is None, result
    assert xray_batch_repository_rows(result, [REPO_ALIAS]) == [(REPO_ALIAS, 0, None)]
    [warning] = [
        w for w in result.get("warnings") or [] if w["type"] == "unparsed_files"
    ]
    assert (warning["repository_alias"], warning["scan_index"], warning["count"]) == (
        REPO_ALIAS,
        0,
        1,
    )


_SMALL_READ_CEILING = 64


def test_batch_forwards_read_capped_warning_and_reports_cut(tmp_path):
    lines = "".join(f"x = {i}\n" for i in range(50))
    with patch(
        "code_indexer.global_repos.regex_search._MAX_READ_BYTES", _SMALL_READ_CEILING
    ):
        result = _run_batch(tmp_path, {"a.py": lines}, delete=False)

    tagged = [
        (w["type"], w["repository_alias"], w["scan_index"])
        for w in result.get("warnings") or []
    ]
    assert ("content_search_read_capped", REPO_ALIAS, 0) in tagged, result
    assert result["partial"] is True, result
    assert [c["reason"] for c in result["phase1_truncated_cells"]] == ["read_limit"]


def test_batch_cell_with_full_unparse_did_not_run(tmp_path):
    from code_indexer.server.services.search_event_outcomes import xray_result_outcome

    result = _run_batch(tmp_path, {_DOOMED_NAME: "y = 2\n"})

    assert xray_result_outcome(result) == "no_cell_ran", result
    [error] = result["evaluation_errors"]
    assert (error["file_path"], error["error_type"]) == ("", "UnparsedFiles")
