"""Unit tests for analyze_graph's multi-repository `repository_alias` forms
-- Issue #1902 (Epic #1906, phase P9).

`xray_search`, `xray_explore`, `regex_search` and `list_files` all accept a
string, a string array, or a JSON-encoded string array for their repository
identifier. `analyze_graph` accepted only a bare string. This module proves
the reused parsing seam (`_parse_json_string_array` from `_utils.py`, the
SAME helper `handlers/xray.py` already uses) now accepts all three forms,
and that a genuine multi-repo request runs the existing single-repo pipeline
once PER alias -- never a merged/undifferentiated findings list, since dense
symbol ids are per-graph and would collide meaninglessly across repos.

Mocking strategy mirrors test_analyze_graph_handler.py exactly: only
`_resolve_repo_path` (the repo-alias -> real path boundary) is mocked, onto
REAL on-disk tmp_path repos. Everything downstream (validation, file
collection, compile, build-graph, analyze-graph) runs for REAL against the
actual compiled xray-cli binary for every test that reaches that pipeline --
no mocking of the analysis logic itself. Tests that must fail BEFORE repo
resolution (empty list, invalid element type, repo-count cap) assert
`_resolve_repo_path` is never called, and do not require the binary.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, cast
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.config_service import get_config_service
from code_indexer.xray.rust_backend import _XRAY_CLI_DEFAULT


def _require_xray_cli_binary() -> None:
    if not _XRAY_CLI_DEFAULT.exists():
        pytest.skip(
            f"xray-cli binary not built at {_XRAY_CLI_DEFAULT}; "
            "run 'cargo build --release' inside rust/ to enable this test."
        )


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="not-a-real-credential",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _import_handler():
    from code_indexer.server.mcp.handlers.xray_graph import handle_analyze_graph

    return handle_analyze_graph


def _evaluator_for(marker: str) -> str:
    """A minimal graph evaluator whose single finding's message embeds
    `marker` -- lets a test prove WHICH repo a finding came from without
    depending on cross-file resolution mechanics."""
    return f"""\
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {{
    Vec::new()
}}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {{
    let mut result = GraphResult::default();
    result.findings.push(ReduceFinding {{
        pattern: "repo_marker".to_string(),
        message: "{marker}".to_string(),
        involved: Vec::new(),
        signatures: Vec::new(),
    }});
    result
}}
"""


def _write_trivial_repo(root: Path, class_name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{class_name}.java").write_text(
        f"class {class_name} {{ void run() {{}} }}\n"
    )


VALID_PARAMS: Dict[str, Any] = {
    "repository_alias": "myrepo-global",
    "evaluator_code": _evaluator_for("single"),
}


# ---------------------------------------------------------------------------
# Fast, boundary-level tests -- fail before repo resolution, no binary needed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_list_alias_returns_repository_alias_required() -> None:
    """An empty array must be rejected with the SAME error code the empty
    bare-string case already uses -- never silently treated as "no repos,
    return empty success"."""
    handler = _import_handler()
    params = {**VALID_PARAMS, "repository_alias": []}
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path"
    ) as mock_resolve:
        result = await handler(params, _make_user())
    mock_resolve.assert_not_called()
    assert _parse_response(result)["error"] == "repository_alias_required"


@pytest.mark.asyncio
async def test_list_with_non_string_element_returns_invalid_params() -> None:
    """A list mixing a string with a non-string element must fail loudly
    as invalid_params, never coerced or silently dropped."""
    handler = _import_handler()
    params = {**VALID_PARAMS, "repository_alias": ["real-repo", 123]}
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path"
    ) as mock_resolve:
        result = await handler(params, _make_user())
    mock_resolve.assert_not_called()
    assert _parse_response(result)["error"] == "invalid_params"


@pytest.mark.asyncio
async def test_repo_count_cap_exceeded_rejected_before_resolution() -> None:
    """Reuses the SAME `omni_max_repos_per_search` config-driven cap
    xray_search's own omni fan-out enforces (Bug #894) -- no new setting
    introduced for analyze_graph's multi-repo path. Computes the cap
    dynamically so the test tracks whatever the server is actually
    configured with rather than hardcoding a value that could drift."""
    multi_search_limits_config = (
        get_config_service().get_config().multi_search_limits_config
    )
    assert multi_search_limits_config is not None
    cap = multi_search_limits_config.omni_max_repos_per_search
    handler = _import_handler()
    aliases = [f"repo-{i}" for i in range(cap + 1)]
    params = {**VALID_PARAMS, "repository_alias": aliases}
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path"
    ) as mock_resolve:
        result = await handler(params, _make_user())
    mock_resolve.assert_not_called()
    assert _parse_response(result)["error"] == "repo_count_cap_exceeded"


@pytest.mark.asyncio
async def test_malformed_json_like_string_treated_as_literal_alias_not_a_list() -> None:
    """A string that STARTS WITH '[' but is not valid JSON (e.g. a truncated
    array) must NOT be silently coerced into a list, and must not crash --
    it is passed through untouched as a literal (bogus) alias, exactly the
    same as `_parse_json_string_array`'s existing contract for xray_search.
    The unresolvable literal string must appear verbatim in the resulting
    repository_not_found error, proving no list branch silently swallowed
    or reinterpreted it.
    """
    handler = _import_handler()
    malformed = '["repo-a", "repo-b"'  # missing closing bracket/quote
    params = {**VALID_PARAMS, "repository_alias": malformed}
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        return_value=None,
    ) as mock_resolve:
        result = await handler(params, _make_user())
    mock_resolve.assert_called_once_with(malformed)
    parsed = _parse_response(result)
    assert parsed["error"] == "repository_not_found"
    assert malformed in parsed["message"]


@pytest.mark.asyncio
async def test_bare_string_alias_takes_single_repo_path_unchanged() -> None:
    """Regression guard: a bare string must still dispatch through the
    existing single-repo pipeline call (one positional alias, not a list),
    and the response must carry NO multi-repo wrapper ("mode"/"results")."""
    from unittest.mock import AsyncMock

    handler = _import_handler()
    canned = {
        "ok": True,
        "status": "ran_ok",
        "findings": [],
        "refine": [],
        "fact_graph_complete": True,
    }
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
        new=AsyncMock(return_value=canned),
    ) as mock_pipeline:
        result = await handler(dict(VALID_PARAMS), _make_user())
    mock_pipeline.assert_called_once()
    call_args = mock_pipeline.call_args.args
    assert call_args[1] == "myrepo-global"  # repo_alias positional, plain string
    parsed = _parse_response(result)
    assert "mode" not in parsed
    assert "results" not in parsed
    assert parsed["ok"] is True


@pytest.mark.asyncio
async def test_single_element_list_collapses_to_single_repo_path() -> None:
    """A single-element list (native or JSON-encoded) is ergonomic sugar for
    a bare string -- matches xray_search's own v10.4.5 Defect 5 convention
    -- and must produce the SAME unwrapped single-repo response shape, not
    the multi-repo {"mode": "multi_repo", "results": {...}} wrapper."""
    from unittest.mock import AsyncMock

    handler = _import_handler()
    canned = {
        "ok": True,
        "status": "ran_ok",
        "findings": [],
        "refine": [],
        "fact_graph_complete": True,
    }
    params = {**VALID_PARAMS, "repository_alias": ["myrepo-global"]}
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
        new=AsyncMock(return_value=canned),
    ) as mock_pipeline:
        result = await handler(params, _make_user())
    mock_pipeline.assert_called_once()
    call_args = mock_pipeline.call_args.args
    assert call_args[1] == "myrepo-global"
    parsed = _parse_response(result)
    assert "mode" not in parsed
    assert "results" not in parsed


# ---------------------------------------------------------------------------
# Real multi-repo dispatch -- exercises the actual xray-cli pipeline per
# alias, mocking only the alias -> path boundary (established convention).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_repo_list_runs_pipeline_per_repo_with_unambiguous_attribution(
    tmp_path: Path,
) -> None:
    """A genuine two-element list must run the real analysis TWICE (once per
    repo) and return each repo's findings under its OWN key -- never merged
    into one flat list, since dense symbol ids are per-graph and would
    collide meaninglessly across repos (Issue #1902 AC)."""
    _require_xray_cli_binary()
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    _write_trivial_repo(repo_a, "AlphaClass")
    _write_trivial_repo(repo_b, "BetaClass")

    alias_to_path = {"alpha-global": str(repo_a), "beta-global": str(repo_b)}
    handler = _import_handler()
    params = {
        "repository_alias": ["alpha-global", "beta-global"],
        "evaluator_code": _evaluator_for("cross_repo_probe"),
    }
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        side_effect=lambda alias: alias_to_path.get(alias),
    ):
        result = await handler(params, _make_user())

    parsed = _parse_response(result)
    assert parsed["mode"] == "multi_repo", f"expected multi_repo mode, got: {parsed}"
    assert parsed["ok"] is True
    assert parsed["errors"] == []
    assert set(parsed["results"].keys()) == {"alpha-global", "beta-global"}
    assert parsed["results"]["alpha-global"]["ok"] is True
    assert parsed["results"]["beta-global"]["ok"] is True
    # Each per-repo result carries its OWN findings -- no cross-contamination.
    assert parsed["results"]["alpha-global"]["findings"][0]["message"] == (
        "cross_repo_probe"
    )
    assert parsed["results"]["beta-global"]["findings"][0]["message"] == (
        "cross_repo_probe"
    )


@pytest.mark.asyncio
async def test_json_encoded_two_repo_list_matches_native_list_shape(
    tmp_path: Path,
) -> None:
    """Same two-repo scenario, but `repository_alias` is sent as a
    JSON-ENCODED string (the third accepted form) -- must produce the
    identical multi_repo response shape as the native-list form."""
    _require_xray_cli_binary()
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    _write_trivial_repo(repo_a, "AlphaClass")
    _write_trivial_repo(repo_b, "BetaClass")

    alias_to_path = {"alpha-global": str(repo_a), "beta-global": str(repo_b)}
    handler = _import_handler()
    params = {
        "repository_alias": json.dumps(["alpha-global", "beta-global"]),
        "evaluator_code": _evaluator_for("json_form_probe"),
    }
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        side_effect=lambda alias: alias_to_path.get(alias),
    ):
        result = await handler(params, _make_user())

    parsed = _parse_response(result)
    assert parsed["mode"] == "multi_repo"
    assert parsed["ok"] is True
    assert set(parsed["results"].keys()) == {"alpha-global", "beta-global"}


@pytest.mark.asyncio
async def test_mixed_valid_and_unknown_alias_list_reports_per_repo_error(
    tmp_path: Path,
) -> None:
    """One resolvable alias + one unknown alias: the valid repo's real
    analysis must still run and land in `results`, the unknown alias must
    land in `errors` (never silently dropped, never poisoning the valid
    repo's result), and the top-level `ok` must be False -- a caller must
    never read a partial multi-repo success as a clean bill of health."""
    _require_xray_cli_binary()
    repo_a = tmp_path / "repo_a"
    _write_trivial_repo(repo_a, "AlphaClass")

    alias_to_path = {"alpha-global": str(repo_a)}
    handler = _import_handler()
    params = {
        "repository_alias": ["alpha-global", "does-not-exist-global"],
        "evaluator_code": _evaluator_for("mixed_probe"),
    }
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
        side_effect=lambda alias: alias_to_path.get(alias),
    ):
        result = await handler(params, _make_user())

    parsed = _parse_response(result)
    assert parsed["mode"] == "multi_repo"
    assert parsed["ok"] is False
    assert set(parsed["results"].keys()) == {"alpha-global"}
    assert parsed["results"]["alpha-global"]["ok"] is True
    assert len(parsed["errors"]) == 1
    assert parsed["errors"][0]["repository_alias"] == "does-not-exist-global"
    assert parsed["errors"][0]["error"] == "repository_not_found"


# ---------------------------------------------------------------------------
# Reviewer remediation round (Issue #1902 P9 review, blocking P2 + P3s):
# the timeout-division arithmetic was measurably wrong (silently shrinks the
# per-repo budget below what a fleet-scale repo needs, while the aggregate
# still exceeds a single-repo call's own promised bound above N=12 at the
# default timeout); the evaluator pre-flight ran once PER alias instead of
# once per request; duplicate aliases were never deduplicated before the
# repo-count cap; an empty-string list element bypassed the same rejection
# a bare empty string gets; and a per-alias error entry spread an unbounded
# result (a compile error's full stderr) N times into `errors[]`.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_seconds_passed_full_not_divided_per_alias() -> None:
    """Issue #1902 P9 review, P2: `timeout_seconds` is a PER-REPOSITORY
    budget, matching `xray_search`'s own multi-repo contract (handlers/
    xray.py 914-1072, which resolves timeout_seconds ONCE and hands the
    FULL value to every alias's job) -- it must never be divided across
    the alias count. 2 aliases * 200s = 400s stays under the aggregate
    ceiling, so this must reach the pipeline call with 200 preserved for
    BOTH aliases, not floor(200/2)=100."""
    from unittest.mock import AsyncMock

    handler = _import_handler()
    canned = {"ok": True, "status": "ran_ok", "findings": [], "refine": []}
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
        new=AsyncMock(return_value=canned),
    ) as mock_pipeline:
        params = {
            "repository_alias": ["repo-a", "repo-b"],
            "evaluator_code": _evaluator_for("timeout_probe"),
            "timeout_seconds": 200,
        }
        result = await handler(params, _make_user())
    assert mock_pipeline.call_count == 2
    for call in mock_pipeline.call_args_list:
        assert call.args[4] == 200, f"timeout_seconds was divided: {call.args}"
    parsed = _parse_response(result)
    assert parsed["ok"] is True


# Bug #1913 removed `_check_multi_repo_timeout_budget` (the up-front
# `alias_count * timeout_seconds > 600` admission predicate this module
# used to test here as `test_total_timeout_budget_exceeding_ceiling_
# rejected_up_front`) in favor of an ELAPSED-WALL-CLOCK deadline enforced
# per-alias inside `_run_multi_repo_analyze_graph`. See
# tests/unit/server/mcp/test_analyze_graph_deadline_1913.py for the
# replacement coverage -- including
# `test_request_old_rule_would_refuse_now_runs_instead`, which asserts the
# OPPOSITE of what this deleted test asserted: the same 6-alias/120s
# request that used to be refused up front now actually RUNS.


@pytest.mark.asyncio
async def test_duplicate_aliases_deduped_before_analysis() -> None:
    """Issue #1902 P9 review, P3: `["dup", "dup", "other"]` must analyze
    "dup" exactly ONCE, not twice -- `len(results)` must equal
    `len(repositories)` so a caller can use that as a completeness check,
    and `_enforce_repo_count_cap`'s own docstring already documents that it
    expects a POST-DEDUP list."""
    from unittest.mock import AsyncMock

    handler = _import_handler()
    canned = {"ok": True, "status": "ran_ok", "findings": [], "refine": []}
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
        new=AsyncMock(return_value=canned),
    ) as mock_pipeline:
        params = {
            "repository_alias": ["dup", "dup", "other"],
            "evaluator_code": _evaluator_for("dedup_probe"),
        }
        result = await handler(params, _make_user())
    assert mock_pipeline.call_count == 2
    called_aliases = [call.args[1] for call in mock_pipeline.call_args_list]
    assert called_aliases == ["dup", "other"]  # order-preserving
    parsed = _parse_response(result)
    assert parsed["repositories"] == ["dup", "other"]
    assert set(parsed["results"].keys()) == {"dup", "other"}
    assert len(parsed["results"]) == len(parsed["repositories"])


@pytest.mark.asyncio
async def test_empty_string_element_in_list_returns_repository_alias_required() -> None:
    """Issue #1902 P9 review, P3: `[""]` (or an empty string anywhere in the
    list) is the SAME user error a bare empty string `""` already rejects
    with `repository_alias_required` -- it must not silently enter the
    multi-repo path and surface later as a per-alias `repository_not_found`,
    a different error code/shape for the identical mistake."""
    handler = _import_handler()
    params = {**VALID_PARAMS, "repository_alias": [""]}
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path"
    ) as mock_resolve:
        result = await handler(params, _make_user())
    mock_resolve.assert_not_called()
    assert _parse_response(result)["error"] == "repository_alias_required"

    params_mixed = {**VALID_PARAMS, "repository_alias": ["real-repo", ""]}
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path"
    ) as mock_resolve_mixed:
        result_mixed = await handler(params_mixed, _make_user())
    mock_resolve_mixed.assert_not_called()
    assert _parse_response(result_mixed)["error"] == "repository_alias_required"


@pytest.mark.asyncio
async def test_evaluator_validation_hoisted_to_single_top_level_error() -> None:
    """Issue #1902 P9 review, P3: a forbidden-construct evaluator with
    multiple aliases must produce ONE top-level
    `xray_evaluator_validation_failed` error -- never one identical error
    PER alias -- and must never resolve any alias or run the pipeline at
    all (mirrors `xray_search`'s own multi-repo pre-flight at
    handlers/xray.py:944, which validates ONCE before the per-alias
    submission loop)."""
    unsafe_evaluator = (
        "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {\n"
        "    unsafe {}\n"
        "    Vec::new()\n"
        "}\n"
        "fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> "
        "GraphResult {\n"
        "    GraphResult::default()\n"
        "}\n"
    )
    handler = _import_handler()
    params = {
        "repository_alias": ["repo-a", "repo-b", "repo-c"],
        "evaluator_code": unsafe_evaluator,
    }
    with (
        patch(
            "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path"
        ) as mock_resolve,
        patch(
            "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline"
        ) as mock_pipeline,
    ):
        result = await handler(params, _make_user())
    mock_resolve.assert_not_called()
    mock_pipeline.assert_not_called()
    parsed = _parse_response(result)
    assert parsed["error"] == "xray_evaluator_validation_failed"
    assert "unsafe" in parsed["message"].lower()
    assert "errors" not in parsed  # a single flat error, not a per-alias list
    assert "mode" not in parsed


@pytest.mark.asyncio
async def test_bounded_error_payload_truncates_and_preserves_repository_alias() -> None:
    """Issue #1902 P9 review, P3: a per-alias failure entry must never
    spread an unbounded result (a real compile error's full stderr, plus
    always-empty findings/refine/degradation fields) verbatim N times into
    `errors[]` -- it must go through an explicit field allowlist with
    truncation, and `repository_alias` must reflect the real alias even if
    the underlying result carried its own colliding key."""
    from unittest.mock import AsyncMock

    huge_stderr = "x" * 50_000
    canned_error = {
        "ok": False,
        "error": {"error_type": "CompileError", "error_message": huge_stderr},
        "status": None,
        "findings": [],
        "refine": [],
        "fact_graph_complete": None,
        "build_status": "compile_failed",
        "degradation": None,
        "cached": False,
        "compile_ms": 0,
        "repository_alias": "SHOULD_NOT_WIN",
    }
    handler = _import_handler()
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
        new=AsyncMock(return_value=canned_error),
    ):
        params = {
            "repository_alias": ["repo-a"],
            "evaluator_code": _evaluator_for("bounded_probe"),
        }
        result = await handler(
            {**params, "repository_alias": ["repo-a", "repo-b"]}, _make_user()
        )
    parsed = _parse_response(result)
    assert parsed["ok"] is False
    assert len(parsed["errors"]) == 2
    for entry in parsed["errors"]:
        assert entry["repository_alias"] in ("repo-a", "repo-b")
        assert entry["repository_alias"] != "SHOULD_NOT_WIN"
        # Fields the pipeline never returns here (findings/refine/degradation/
        # cached/compile_ms/fact_graph_complete/ok) must not be echoed back.
        assert "findings" not in entry
        assert "refine" not in entry
        assert "degradation" not in entry
        assert "cached" not in entry
        assert "compile_ms" not in entry
        err_message = entry["error"]["error_message"]
        assert len(err_message) < len(huge_stderr)
