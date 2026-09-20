"""Unit tests for Bug #1913: analyze_graph's multi-repo admission predicate
(`_check_multi_repo_timeout_budget`, `alias_count * timeout_seconds > 600`
refused up front) is replaced by a BETWEEN-ALIAS ADMISSION GATE inside
`_run_multi_repo_analyze_graph` -- the same 600s threshold
(`_MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS`), checked before STARTING each
alias against real elapsed wall clock (`t0 = time.monotonic()`), rather than
refusing a worst case that never materialises. P2-B (dual review): this is
deliberately NOT described as a "deadline" or a wall-clock bound anywhere in
this file -- the gate cannot bound an alias already in flight when it last
passed (queue wait outside the backend's own deadline, plus an unclocked
`os.walk` that can block forever on `hard` NFS). See the production
comment on `_MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS` for the full
accounting.

Also covers:
- A per-alias pipeline exception is recorded into `errors[]` (bounded via
  `_bound_repo_error_payload`) instead of 500-ing the whole request, so
  earlier aliases' completed results survive (P1: the recorded message is
  sanitized -- no server-internal detail such as a filesystem path -- and
  always carries the exception's type name, even when `str(exc)` is
  empty).
- Every `errors[]` entry carries a non-empty `message`, INCLUDING when the
  underlying failure is the real `_graph_error_result` shape (`error` as an
  `{error_type, error_message}` OBJECT with no top-level `message` of its
  own) -- P2-A.
- The three pre-existing bare-error-code top-level rejections
  (`auth_required`, `evaluator_code_required`, `repository_alias_required`)
  now also carry a `message` -- P2-C.

Mocking strategy: `_run_analyze_graph_pipeline` is replaced with an
`AsyncMock` for every test here (these are unit tests of the admission/
exception-guard LOGIC in `_run_multi_repo_analyze_graph` and its caller,
not of the real xray-cli pipeline -- that real-pipeline coverage already
exists in test_analyze_graph_multi_repo_1902.py). Where a clock must be
controlled, `time.monotonic` is patched at the seam
(`code_indexer.server.mcp.handlers.xray_graph.time.monotonic`) rather than
restructuring production code for testability, per Bug #1913's own
instruction -- driven off an OBSERVABLE TRIGGER (a shared counter the
pipeline mock's own side effect advances), never off the ordinal position
of a global `time.monotonic()` call, since patching it on the module also
replaces the asyncio event loop's own clock (P3-3, dual review).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, cast
from unittest.mock import AsyncMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.mcp.handlers.xray_graph import (
    _MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS,
)
from code_indexer.xray.rust_backend import _graph_error_result


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


CANNED_OK = {"ok": True, "status": "ran_ok", "findings": [], "refine": []}
EVALUATOR_CODE = (
    "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {\n"
    "    Vec::new()\n"
    "}\n"
    "fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> "
    "GraphResult {\n"
    "    GraphResult::default()\n"
    "}\n"
)


@pytest.mark.asyncio
async def test_request_admitted_under_old_rule_still_succeeds() -> None:
    """2 aliases at the default 120s timeout (240s theoretical total) was
    already admitted by the old `alias_count * timeout_seconds <= 600`
    predicate. Must still fully succeed under the elapsed-deadline
    replacement: both invariants hold (ok semantics, and
    len(results) + len(errors) == len(repositories))."""
    handler = _import_handler()
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
        new=AsyncMock(return_value=dict(CANNED_OK)),
    ) as mock_pipeline:
        params = {
            "repository_alias": ["repo-a", "repo-b"],
            "evaluator_code": EVALUATOR_CODE,
        }
        result = await handler(params, _make_user())
    assert mock_pipeline.call_count == 2
    parsed = _parse_response(result)
    assert parsed["mode"] == "multi_repo"
    assert parsed["ok"] is True
    assert parsed["errors"] == []
    assert set(parsed["results"].keys()) == {"repo-a", "repo-b"}
    assert len(parsed["results"]) + len(parsed["errors"]) == len(parsed["repositories"])


@pytest.mark.asyncio
async def test_request_old_rule_would_refuse_now_runs_instead() -> None:
    """10 aliases at the default 120s timeout = 1200s theoretical total: the
    OLD `_check_multi_repo_timeout_budget` predicate refused this up front
    with `multi_repo_timeout_budget_exceeded`, before resolving a single
    alias or calling the pipeline once. Bug #1913's fix admits the common
    case instead -- the request must actually RUN (the pipeline called once
    per alias), not be refused up front.

    Discriminating against the CURRENT (pre-fix) code: today this returns a
    single top-level `multi_repo_timeout_budget_exceeded` rejection and
    NEVER calls the pipeline at all (`mock_pipeline.call_count == 0`).
    """
    multi_search_limits_config = (
        get_config_service().get_config().multi_search_limits_config
    )
    assert multi_search_limits_config is not None
    cap = multi_search_limits_config.omni_max_repos_per_search
    alias_count = 10
    if alias_count > cap:
        pytest.skip(f"omni_max_repos_per_search={cap} too small for this test")

    handler = _import_handler()
    aliases = [f"repo-{i}" for i in range(alias_count)]
    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
        new=AsyncMock(return_value=dict(CANNED_OK)),
    ) as mock_pipeline:
        params = {
            "repository_alias": aliases,
            "evaluator_code": EVALUATOR_CODE,
            "timeout_seconds": 120,  # 10 * 120 = 1200s > 600s old ceiling
        }
        result = await handler(params, _make_user())

    assert mock_pipeline.call_count == alias_count, (
        "request was refused up front instead of running -- old admission "
        f"predicate still active (pipeline called {mock_pipeline.call_count} "
        f"times, expected {alias_count})"
    )
    parsed = _parse_response(result)
    assert parsed.get("error") != "multi_repo_timeout_budget_exceeded"
    assert parsed["mode"] == "multi_repo"
    assert parsed["ok"] is True
    assert set(parsed["results"].keys()) == set(aliases)
    assert len(parsed["results"]) + len(parsed["errors"]) == len(parsed["repositories"])


@pytest.mark.asyncio
async def test_deadline_crossed_mid_loop_yields_partial_results_and_deadline_errors() -> (
    None
):
    """Simulated slow aliases: the elapsed deadline trips BETWEEN alias 1
    and alias 2 of a 3-alias request. Alias 1 must have already completed
    and landed a real result; aliases 2 and 3 (never started) must each
    carry `multi_repo_deadline_exceeded` in `errors[]` -- not be silently
    dropped. Both invariants (ok semantics, count reconciliation) must
    still hold.

    Discriminating against the CURRENT (pre-fix) code: `_run_multi_repo_
    analyze_graph` today has NO elapsed-time tracking at all -- it always
    runs the pipeline for every alias regardless of `time.monotonic`, so
    patching the clock has zero effect on today's code and all 3 aliases
    get real (mocked-success) results with an empty `errors[]`.

    P3-3 (dual review): the fake clock is driven off an OBSERVABLE
    TRIGGER -- a shared counter that ONLY the first alias's own pipeline
    call advances -- rather than the ORDINAL POSITION of a fixed-length
    `time.monotonic()` call sequence. Patching `time.monotonic` on the
    module also replaces the asyncio event loop's own clock, so a
    positional sequence silently breaks if the loop (or a future
    production code path) calls `time.monotonic()` one extra time; a
    shared counter that only advances on a real, named trigger has no
    such assumption -- every incidental extra call just re-reads the same
    stable value.
    """
    handler = _import_handler()

    # clock["t"] starts unmodified: t0 = time.monotonic() (the very first
    # call inside _run_multi_repo_analyze_graph) reads it before anything
    # has advanced it, and the admission check for alias "repo-a" (index
    # 0) also reads it unmodified -- elapsed is 0, so "repo-a" is admitted
    # and its pipeline call runs. ONLY that pipeline call's own side
    # effect advances the clock past the ceiling, simulating "repo-a"
    # having taken long enough to cross it. The admission check for
    # "repo-b" (index 1) then observes the advanced clock and trips.
    clock = {"t": 1000.0}

    def _fake_monotonic() -> float:
        return clock["t"]

    async def _pipeline_side_effect(
        evaluator_code, repo_alias, include_patterns, exclude_patterns, timeout_seconds
    ):
        assert repo_alias == "repo-a", (
            "only the first alias should ever reach the pipeline in this "
            f"scenario, got {repo_alias!r}"
        )
        clock["t"] += _MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS
        return dict(CANNED_OK)

    with (
        patch(
            "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
            new=AsyncMock(side_effect=_pipeline_side_effect),
        ) as mock_pipeline,
        patch(
            "code_indexer.server.mcp.handlers.xray_graph.time.monotonic",
            side_effect=_fake_monotonic,
        ),
    ):
        params = {
            "repository_alias": ["repo-a", "repo-b", "repo-c"],
            "evaluator_code": EVALUATOR_CODE,
            "timeout_seconds": 120,
        }
        result = await handler(params, _make_user())

    parsed = _parse_response(result)
    assert mock_pipeline.call_count == 1, (
        "expected the pipeline to run only for the first alias before the "
        f"deadline tripped, got {mock_pipeline.call_count} calls"
    )
    assert parsed["mode"] == "multi_repo"
    assert parsed["ok"] is False
    assert set(parsed["results"].keys()) == {"repo-a"}
    assert len(parsed["errors"]) == 2
    deadline_errors = {
        entry["repository_alias"]: entry["error"] for entry in parsed["errors"]
    }
    assert deadline_errors == {
        "repo-b": "multi_repo_deadline_exceeded",
        "repo-c": "multi_repo_deadline_exceeded",
    }
    assert len(parsed["results"]) + len(parsed["errors"]) == len(parsed["repositories"])
    # The doc's served contract promises every multi-repo-only error entry
    # carries {repository_alias, error, message} -- a bare
    # {"error": "multi_repo_deadline_exceeded"} tells a caller nothing
    # about what happened or what to do next. Pin the message as present,
    # non-empty, and naming the actual ceiling value, so the contract is
    # enforced by a test rather than only described in prose.
    for entry in parsed["errors"]:
        message = entry.get("message")
        assert isinstance(message, str) and message.strip(), (
            f"expected a non-empty 'message' on deadline-exceeded entry "
            f"{entry!r}, describing what happened and how to recover"
        )
        assert str(_MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS) in message, (
            "expected the ceiling value "
            f"({_MULTI_REPO_TOTAL_TIMEOUT_CEILING_SECONDS}s) named in the "
            f"message, got: {message!r}"
        )


@pytest.mark.asyncio
async def test_per_alias_exception_recorded_in_errors_earlier_results_survive() -> None:
    """An exception raised by the per-alias pipeline call (e.g. `_resolve_
    repo_path`/thread-pool failure) must be caught and recorded into
    `errors[]` -- never propagate and 500 the whole request. Earlier
    aliases' completed real results must survive.

    Discriminating against the CURRENT (pre-fix) code: `_run_multi_repo_
    analyze_graph` today has no try/except around the pipeline call, so a
    raised exception propagates straight out of `handle_analyze_graph` --
    `await handler(...)` itself raises instead of returning a structured
    response.
    """
    handler = _import_handler()

    async def _pipeline_side_effect(
        evaluator_code, repo_alias, include_patterns, exclude_patterns, timeout_seconds
    ):
        if repo_alias == "repo-a":
            return dict(CANNED_OK)
        raise RuntimeError("boom: simulated resolver/thread-pool failure")

    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
        new=AsyncMock(side_effect=_pipeline_side_effect),
    ) as mock_pipeline:
        params = {
            "repository_alias": ["repo-a", "repo-b"],
            "evaluator_code": EVALUATOR_CODE,
        }
        result = await handler(params, _make_user())

    assert mock_pipeline.call_count == 2
    parsed = _parse_response(result)
    assert parsed["mode"] == "multi_repo"
    assert parsed["ok"] is False
    assert set(parsed["results"].keys()) == {"repo-a"}
    assert parsed["results"]["repo-a"]["ok"] is True
    assert len(parsed["errors"]) == 1
    assert parsed["errors"][0]["repository_alias"] == "repo-b"
    # P3-1 (dual review): pin the error CODE itself, not just the message
    # substring -- this string is a served contract; without this
    # assertion, renaming it would leave every test in this file green
    # while analyze_graph.md's documented enumeration went stale (the
    # exact mechanism behind the last two doc-drift defects in this file).
    assert parsed["errors"][0]["error"] == "multi_repo_pipeline_exception"
    # P1: the message is never the raw str(exc) -- it is sanitized and
    # prefixed with the exception's type name.
    assert (
        parsed["errors"][0]["message"]
        == "RuntimeError: boom: simulated resolver/thread-pool failure"
    )
    assert len(parsed["results"]) + len(parsed["errors"]) == len(parsed["repositories"])


@pytest.mark.asyncio
async def test_per_alias_exception_server_path_is_never_leaked_to_caller() -> None:
    """P1 (dual review): a `FileNotFoundError`/`OSError` raised inside
    `_resolve_repo_and_files` (the live exception surface this guard
    exists for) carries an ABSOLUTE SERVER PATH in its message. That
    detail must never reach a public MCP response -- the existing
    `RustNativeBackend` path already sanitizes every `error_message`
    through `_sanitize_error_message` before it leaves the process
    (`rust_backend.py:433`), and the per-alias exception guard must match
    that, not bypass it.
    """
    handler = _import_handler()
    leaking_path = "/home/serviceaccount/srv/example-internal-repo"

    async def _pipeline_side_effect(
        evaluator_code, repo_alias, include_patterns, exclude_patterns, timeout_seconds
    ):
        raise FileNotFoundError(
            f"[Errno 2] No such file or directory: '{leaking_path}'"
        )

    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
        new=AsyncMock(side_effect=_pipeline_side_effect),
    ):
        # A single-element repository_alias list collapses to the
        # single-repo path (established ergonomic normalization
        # elsewhere in this handler), which bypasses this exception
        # guard entirely -- 2 aliases keeps this on the multi-repo path.
        params = {
            "repository_alias": ["repo-a", "repo-b"],
            "evaluator_code": EVALUATOR_CODE,
        }
        result = await handler(params, _make_user())

    parsed = _parse_response(result)
    message = parsed["errors"][0]["message"]
    assert leaking_path not in message, (
        f"server-internal path leaked into public response message: {message!r}"
    )
    assert "<server-path>" in message
    assert "FileNotFoundError" in message


@pytest.mark.asyncio
async def test_per_alias_bare_exception_still_yields_nonempty_message_with_type_name() -> (
    None
):
    """P1 (dual review): `str(exc)` is EMPTY for a bare `RuntimeError()` or
    `KeyError()` -- verified directly. Before this fix a bare raise
    produced `"message": ""`, zero diagnostic content. The exception TYPE
    name must always be present, even when there is no detail text at
    all.
    """
    handler = _import_handler()

    async def _pipeline_side_effect(
        evaluator_code, repo_alias, include_patterns, exclude_patterns, timeout_seconds
    ):
        raise RuntimeError()

    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
        new=AsyncMock(side_effect=_pipeline_side_effect),
    ):
        # A single-element repository_alias list collapses to the
        # single-repo path and bypasses this exception guard entirely.
        params = {
            "repository_alias": ["repo-a", "repo-b"],
            "evaluator_code": EVALUATOR_CODE,
        }
        result = await handler(params, _make_user())

    parsed = _parse_response(result)
    message = parsed["errors"][0]["message"]
    assert isinstance(message, str) and message.strip(), (
        f"expected a non-empty message for a bare exception, got {message!r}"
    )
    assert message == "RuntimeError"


@pytest.mark.asyncio
async def test_real_graph_error_result_shape_gets_synthesized_message() -> None:
    """P2-A (dual review, both reviewers independently): `_graph_error_
    result` (rust_backend.py) -- the real compile/build/analyze failure
    shape, the single most likely real failure of this tool -- returns
    `{"error": {"error_type", "error_message"}, ...}` with NO top-level
    `message` key. Uses the REAL helper (not a hand-made dict, per
    instruction) so this test cannot silently drift from the actual
    production shape. Before the fix, `entry["message"]` raised
    `KeyError` for exactly this shape.
    """
    handler = _import_handler()
    real_failure = _graph_error_result(
        error_type="CompileError",
        error_message="compile failed: unresolved import `foo::bar`",
        build_status="ok",
    )
    assert "message" not in real_failure  # the real shape has no top-level message

    async def _pipeline_side_effect(
        evaluator_code, repo_alias, include_patterns, exclude_patterns, timeout_seconds
    ):
        if repo_alias == "repo-a":
            return dict(CANNED_OK)
        return dict(real_failure)

    with patch(
        "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
        new=AsyncMock(side_effect=_pipeline_side_effect),
    ):
        params = {
            "repository_alias": ["repo-a", "repo-b"],
            "evaluator_code": EVALUATOR_CODE,
        }
        result = await handler(params, _make_user())

    parsed = _parse_response(result)
    entry = next(e for e in parsed["errors"] if e["repository_alias"] == "repo-b")
    assert isinstance(entry["error"], dict), (
        "the real backend-failure shape's 'error' field is an "
        f"{{error_type, error_message}} OBJECT, got {type(entry['error'])}"
    )
    assert entry["error"]["error_type"] == "CompileError"
    message = entry.get("message")
    assert isinstance(message, str) and message.strip(), (
        f"expected a non-empty synthesized 'message', got {message!r}"
    )
    assert "unresolved import" in message


@pytest.mark.asyncio
async def test_top_level_bare_error_codes_now_carry_nonempty_message() -> None:
    """P2-C (coordinator-confirmed): `evaluator_code_required` (line 368),
    `repository_alias_required` (line 374), and `auth_required` (line
    861, pre-fix line numbers) all returned a bare `{"error": ...}` while
    the rewritten `analyze_graph.md` groups them under the documented
    `{error, message}` rejection shape. Chose to ADD messages rather than
    weaken the doc, per the coordinator's own framing that a uniform
    contract is worth more than the one-line saving.
    """
    handler = _import_handler()

    unauthenticated_result = _parse_response(
        await handler(
            {"repository_alias": "myrepo-global", "evaluator_code": EVALUATOR_CODE},
            None,
        )
    )
    assert unauthenticated_result["error"] == "auth_required"
    assert isinstance(unauthenticated_result.get("message"), str)
    assert unauthenticated_result["message"].strip()

    no_evaluator_result = _parse_response(
        await handler(
            {"repository_alias": "myrepo-global"},
            _make_user(),
        )
    )
    assert no_evaluator_result["error"] == "evaluator_code_required"
    assert isinstance(no_evaluator_result.get("message"), str)
    assert no_evaluator_result["message"].strip()

    no_alias_result = _parse_response(
        await handler(
            {"repository_alias": "", "evaluator_code": EVALUATOR_CODE},
            _make_user(),
        )
    )
    assert no_alias_result["error"] == "repository_alias_required"
    assert isinstance(no_alias_result.get("message"), str)
    assert no_alias_result["message"].strip()
