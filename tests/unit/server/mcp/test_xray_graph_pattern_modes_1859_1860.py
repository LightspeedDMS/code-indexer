"""Regression coverage for X-Ray graph pattern reuse and empty graph signals."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, cast
from unittest.mock import MagicMock, patch

import pytest
import yaml

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.xray.rust_backend import _XRAY_CLI_DEFAULT


GRAPH_EVALUATOR = """\
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let _ = THRESHOLD;
    result
}
"""

GRAPH_EVALUATOR_WITHOUT_PARAMS = GRAPH_EVALUATOR.replace("    let _ = THRESHOLD;\n", "")

LEGACY_EVALUATOR = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"


def _user() -> User:
    return User(
        username="testuser",
        password_hash="not-a-real-credential",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _data(result: Dict[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _write_pattern(
    cidx_meta: Path,
    *,
    name: str,
    code: str,
    execution_mode: Optional[str] = None,
) -> None:
    spec: Dict[str, Any] = {
        "name": name,
        "description": "Regression fixture",
        "language": "java",
        "evaluator_code": code,
    }
    if execution_mode is not None:
        spec["execution_mode"] = execution_mode
    if execution_mode == "graph":
        spec["parameters"] = [
            {
                "name": "THRESHOLD",
                "type": "usize",
                "default": 3,
                "description": "Graph evaluator threshold",
            }
        ]
    path = cidx_meta / "xray-patterns" / "__any__" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")


@pytest.mark.asyncio
async def test_legacy_pattern_rejected_by_analyze_graph(tmp_path: Path) -> None:
    """A mode-less existing pattern is legacy and cannot enter graph mode."""
    cidx_meta = tmp_path / "cidx-meta"
    _write_pattern(cidx_meta, name="legacy-only", code=LEGACY_EVALUATOR)

    from code_indexer.server.mcp.handlers.xray_graph import handle_analyze_graph

    params = {
        "repository_alias": "repo-global",
        "pattern_name": "legacy-only",
        "pattern_params": {},
    }
    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        patch(
            "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
            return_value=cidx_meta,
        ),
        patch("code_indexer.server.mcp.handlers.xray._seeds_ensured", True),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_xray_executor",
            return_value=executor,
        ),
    ):
        result = await handle_analyze_graph(params, _user())

    assert _data(result)["error"] == "pattern_mode_mismatch"


@pytest.mark.asyncio
async def test_graph_pattern_rejected_by_legacy_xray_search(tmp_path: Path) -> None:
    """A graph callback family cannot be executed by xray_search."""
    cidx_meta = tmp_path / "cidx-meta"
    _write_pattern(
        cidx_meta,
        name="graph-only",
        code=GRAPH_EVALUATOR_WITHOUT_PARAMS,
        execution_mode="graph",
    )

    from code_indexer.server.mcp.handlers.xray import handle_xray_search

    params = {
        "repository_alias": "repo-global",
        "pattern": "class",
        "pattern_name": "graph-only",
        "search_target": "content",
    }
    mock_bjm = MagicMock()
    mock_jt = MagicMock()
    mock_jt.register_job.return_value = MagicMock()
    pending_future: "asyncio.Future[Any]" = asyncio.Future()

    # Mirrors the established run_in_executor side-effect trick from
    # test_xray_pattern_handler.py::_xray_single_repo_env: the FIRST
    # run_in_executor call is always the pattern-resolution offload in the
    # real handler code path (_resolve_evaluator_code_off_loop) -- run it
    # for real so the test observes genuine pattern-lookup/mode-check
    # behavior. Any subsequent call is job submission; leaving it an
    # unresolved, never-awaited future means no real search-engine job ever
    # actually runs.
    call_count = {"n": 0}

    def _run_in_executor_side_effect(_executor: Any, func: Any, *args: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            real_result = func(*args)
            first_call_future: "asyncio.Future[Any]" = asyncio.Future()
            first_call_future.set_result(real_result)
            return first_call_future
        return pending_future

    loop_instance = MagicMock()
    loop_instance.run_in_executor.side_effect = _run_in_executor_side_effect

    with (
        patch(
            "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
            return_value=cidx_meta,
        ),
        patch("code_indexer.server.mcp.handlers.xray._seeds_ensured", True),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_xray_executor",
            return_value=MagicMock(),
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/fake/repo/path",
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
            return_value=mock_bjm,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_job_tracker",
            return_value=mock_jt,
        ),
        patch("asyncio.get_running_loop", return_value=loop_instance),
    ):
        result = await handle_xray_search(params, _user())

    assert _data(result)["error"] == "pattern_mode_mismatch"


@pytest.mark.asyncio
async def test_analyze_graph_pattern_and_evaluator_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    """C7 (#1858-#1861 remediation): the mutually_exclusive_params rule has
    exactly ONE authority -- the shared resolver (`xray.py::
    _resolve_evaluator_code`) -- not a parser-side duplicate. This exercises
    the real end-to-end `handle_analyze_graph` front door (not the parser in
    isolation) to prove that authority's rejection actually reaches it.
    """
    cidx_meta = tmp_path / "cidx-meta"

    from code_indexer.server.mcp.handlers.xray_graph import handle_analyze_graph

    params = {
        "repository_alias": "repo-global",
        "evaluator_code": GRAPH_EVALUATOR,
        "pattern_name": "graph-only",
    }
    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        patch(
            "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
            return_value=cidx_meta,
        ),
        patch("code_indexer.server.mcp.handlers.xray._seeds_ensured", True),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_xray_executor",
            return_value=executor,
        ),
    ):
        result = await handle_analyze_graph(params, _user())

    assert _data(result)["error"] == "mutually_exclusive_params"


@pytest.mark.asyncio
async def test_graph_pattern_name_and_params_resolve_and_run(tmp_path: Path) -> None:
    if not _XRAY_CLI_DEFAULT.exists():
        pytest.skip("xray-cli binary is required for graph execution")
    cidx_meta = tmp_path / "cidx-meta"
    _write_pattern(
        cidx_meta,
        name="graph-only",
        code=GRAPH_EVALUATOR,
        execution_mode="graph",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Main.java").write_text("class Main { void run() {} }\n", encoding="utf-8")

    from code_indexer.server.mcp.handlers.xray_graph import handle_analyze_graph

    params = {
        "repository_alias": "repo-global",
        "pattern_name": "graph-only",
        "pattern_params": {"THRESHOLD": 7},
        "timeout_seconds": 60,
    }
    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        patch(
            "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
            return_value=cidx_meta,
        ),
        patch("code_indexer.server.mcp.handlers.xray._seeds_ensured", True),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_xray_executor",
            return_value=executor,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
            return_value=str(repo),
        ),
    ):
        result = await handle_analyze_graph(params, _user())

    parsed = _data(result)
    assert parsed.get("ok") is True, parsed


@pytest.mark.asyncio
async def test_graph_non_java_input_reports_no_supported_files(tmp_path: Path) -> None:
    if not _XRAY_CLI_DEFAULT.exists():
        pytest.skip("xray-cli binary is required for graph execution")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    from code_indexer.server.mcp.handlers.xray_graph import handle_analyze_graph

    params = {
        "repository_alias": "repo-global",
        "evaluator_code": GRAPH_EVALUATOR_WITHOUT_PARAMS,
        "timeout_seconds": 60,
    }
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        return_value=str(repo),
    ):
        result = await handle_analyze_graph(params, _user())

    parsed = _data(result)
    assert parsed.get("ok") is True, parsed
    assert parsed["status"] == "no_supported_files"
    assert parsed["degradation"]["files_with_unsupported_language"] > 0


@pytest.mark.asyncio
async def test_graph_java_input_does_not_report_no_supported_files(
    tmp_path: Path,
) -> None:
    if not _XRAY_CLI_DEFAULT.exists():
        pytest.skip("xray-cli binary is required for graph execution")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Main.java").write_text("class Main { void run() {} }\n", encoding="utf-8")

    from code_indexer.server.mcp.handlers.xray_graph import handle_analyze_graph

    params = {
        "repository_alias": "repo-global",
        "evaluator_code": GRAPH_EVALUATOR_WITHOUT_PARAMS,
        "timeout_seconds": 60,
    }
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        return_value=str(repo),
    ):
        result = await handle_analyze_graph(params, _user())

    parsed = _data(result)
    assert parsed.get("ok") is True, parsed
    assert parsed["status"] != "no_supported_files"


async def _run_graph_analysis_over_files(
    tmp_path: Path, files: Dict[str, str]
) -> Dict[str, Any]:
    """Shared fixture/invocation helper for the `no_supported_files`
    detection tests below: writes `files` (relative-path -> content) into a
    fresh repo dir, then runs `handle_analyze_graph` with the parameter-
    free evaluator against it, returning the parsed MCP response body.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative_path, content in files.items():
        (repo / relative_path).write_text(content, encoding="utf-8")

    from code_indexer.server.mcp.handlers.xray_graph import handle_analyze_graph

    params = {
        "repository_alias": "repo-global",
        "evaluator_code": GRAPH_EVALUATOR_WITHOUT_PARAMS,
        "timeout_seconds": 60,
    }
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        return_value=str(repo),
    ):
        result = await handle_analyze_graph(params, _user())
    return _data(result)


@pytest.mark.asyncio
async def test_graph_mixed_unsupported_language_and_unrecognized_extension_reports_no_supported_files(
    tmp_path: Path,
) -> None:
    """Bug #1858-#1861 remediation C2: `files_with_unsupported_language` and
    `unreadable_or_unsupported_files` are disjoint per-file counters (a
    recognized-but-unimplemented-language file like `main.py` increments
    ONLY the former; a genuinely unrecognized extension like `README.md`
    increments ONLY the latter). The pre-fix condition
    (`files_with_unsupported_language == len(file_paths)`) therefore never
    fires once a repo contains BOTH kinds of file, even though every single
    candidate file is still unsupported and zero files ever reached a
    real extractor. This fixture reproduces that exact real-repo shape
    (#1860's own repro: files_with_unsupported_language=41,
    unreadable_or_unsupported_files=11, neither equal to the file count).
    """
    if not _XRAY_CLI_DEFAULT.exists():
        pytest.skip("xray-cli binary is required for graph execution")

    parsed = await _run_graph_analysis_over_files(
        tmp_path,
        {
            "main.py": "def run():\n    return 1\n",
            "README.md": "# not source code\n",
        },
    )

    assert parsed.get("ok") is True, parsed
    assert parsed["status"] == "no_supported_files", parsed


@pytest.mark.asyncio
async def test_graph_java_with_unrecognized_extension_file_does_not_report_no_supported_files(
    tmp_path: Path,
) -> None:
    """Negative counterpart to the mixed-unsupported test above: a repo
    with one genuinely supported (Java) file alongside an unrecognized-
    extension file (`README.md`) must NOT be mislabelled `no_supported_
    files` -- at least one candidate file did reach the extractor with a
    supported language.
    """
    if not _XRAY_CLI_DEFAULT.exists():
        pytest.skip("xray-cli binary is required for graph execution")

    parsed = await _run_graph_analysis_over_files(
        tmp_path,
        {
            "Main.java": "class Main { void run() {} }\n",
            "README.md": "# not source code\n",
        },
    )

    assert parsed.get("ok") is True, parsed
    assert parsed["status"] != "no_supported_files", parsed
