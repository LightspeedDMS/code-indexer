"""Story #1491 AC6 code-review follow-up: expand the async-dispatch
wait_for exemption beyond regex_search to every other async-dispatched
MCP tool with a structural reason a fixed cap could kill legitimate,
non-hung work.

Classification performed by reading each handler's real implementation
(see also the accompanying commit message):

CLASS (b) -- structurally at risk, exempted here:
  - xray_search / xray_explore (mcp/handlers/xray.py): the single-repo
    path accepts a client-supplied, server-validated `await_seconds`
    (range [0, _AWAIT_SECONDS_MAX=45.0]) and genuinely awaits the
    background job inline for up to that long before returning
    {"job_id": ...}. default_handler_timeout_seconds is Web-UI
    configurable down to 10s (config_manager.py validates [10, 300]) --
    an operator lowering it for unrelated reasons would truncate a
    legitimate, server-approved 45s xray wait, discarding the job_id the
    client needs to poll. The multi-repo (list alias) path submits jobs
    without any inline wait at all and is unaffected either way.
  - gh_actions_search_logs / gitlab_ci_search_logs / ci_search_logs
    (mcp/handlers/cicd.py): each delegates to a client
    (GitHubActionsClient.search_logs / GitLabCIClient.search_logs) that
    fetches the job list for ONE run/pipeline, then loops SEQUENTIALLY
    over every job making one more HTTP GET (+ regex scan) per job --
    structurally identical to _omni_regex_search's sequential fan-out
    bug. A workflow run/pipeline with dozens of matrix-build jobs has a
    legitimate cumulative runtime that can exceed 60s.

CLASS (a) -- genuinely safe under the 60s default, no action needed:
  handle_gh_actions_list_runs, handle_gh_actions_get_run,
  handle_gh_actions_get_job_logs, handle_gh_actions_retry_run,
  handle_gh_actions_cancel_run, handle_gitlab_ci_list_pipelines,
  handle_gitlab_ci_get_pipeline, handle_gitlab_ci_get_job_logs,
  handle_gitlab_ci_retry_pipeline, handle_gitlab_ci_cancel_pipeline,
  handle_ci_list_runs, handle_ci_get_run, handle_ci_get_job_logs,
  handle_ci_cancel_run, handle_ci_retry_run -- every one of these makes
  exactly ONE bounded external API call (list/get/action), with no
  per-item loop and no client-approved inline-wait mechanism. Verified
  by reading each function body: no `for`/`while` loop over
  jobs/pipelines/runs, and the underlying client methods (list_runs,
  get_run, get_job_logs, retry_run, cancel_run, etc.) each issue exactly
  one httpx call (tenacity-retried, bounded backoff), never a
  per-sub-item fan-out.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import _invoke_handler

_EXEMPT_TOOL_NAMES = [
    "xray_search",
    "xray_explore",
    "gh_actions_search_logs",
    "gitlab_ci_search_logs",
    "ci_search_logs",
]

_TINY_DEADLINE_SECONDS = 0.05
_SLOW_HANDLER_SLEEP_SECONDS = 0.2

# xray's own client-approved inline-wait bound (mirrors
# mcp/handlers/xray.py's _AWAIT_SECONDS_MAX) and a configured
# default_handler_timeout_seconds an operator could legitimately set
# (config_manager.py validates the field's range as [10, 300]).
_XRAY_AWAIT_SECONDS_MAX = 45.0
_LOW_OPERATOR_CONFIGURED_DEFAULT_TIMEOUT_SECONDS = 10

# CI/CD sequential per-job loop simulation constants.
_CICD_SINGLE_JOB_FLOOR_SECONDS = 0.05
_CICD_JOB_COUNT = 4
_CICD_SINGLE_JOB_DERIVED_DEADLINE_SECONDS = _CICD_SINGLE_JOB_FLOOR_SECONDS + 0.02


def _make_user() -> User:
    return User(
        username="test_user",
        password_hash="irrelevant",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


@pytest.mark.parametrize("tool_name", _EXEMPT_TOOL_NAMES)
@pytest.mark.asyncio
async def test_tool_is_exempt_from_ac6_async_deadline(tool_name: str) -> None:
    """Each of the 5 newly-classified tools must bypass the AC6
    wait_for wrapper entirely -- an arbitrarily small timeout_seconds
    must have ZERO effect when dispatched under that tool_name."""

    async def slow_handler(arguments, user):
        await asyncio.sleep(_SLOW_HANDLER_SLEEP_SECONDS)
        return {"success": True}

    user = _make_user()
    sig = inspect.signature(slow_handler)

    result = await _invoke_handler(
        handler=slow_handler,
        arguments={},
        user=user,
        session_state=None,
        sig=sig,
        is_async=True,
        timeout_seconds=_TINY_DEADLINE_SECONDS,
        tool_name=tool_name,
    )

    assert result == {"success": True}, (
        f"{tool_name} must be exempt from the AC6 async deadline -- a "
        "slow handler must complete normally regardless of "
        "timeout_seconds."
    )


@pytest.mark.asyncio
async def test_xray_bounded_await_seconds_survives_a_low_operator_configured_default() -> (
    None
):
    """Discriminating test: xray_search's client-approved inline wait can
    legitimately take up to _AWAIT_SECONDS_MAX (45s). An operator-
    configured default_handler_timeout_seconds as low as 10s (a value
    the Web UI genuinely permits) must NOT truncate this wait."""

    async def xray_style_handler(arguments, user):
        # Simulates the bounded, client-approved inline wait -- scaled
        # down proportionally so the test itself runs in milliseconds
        # while preserving "well above the low configured default".
        await asyncio.sleep(_XRAY_AWAIT_SECONDS_MAX / 1000)
        return {"job_id": "fake-job-id"}

    user = _make_user()
    sig = inspect.signature(xray_style_handler)

    result = await _invoke_handler(
        handler=xray_style_handler,
        arguments={},
        user=user,
        session_state=None,
        sig=sig,
        is_async=True,
        timeout_seconds=_LOW_OPERATOR_CONFIGURED_DEFAULT_TIMEOUT_SECONDS / 1000,
        tool_name="xray_search",
    )

    assert result == {"job_id": "fake-job-id"}, (
        "xray_search's inline wait must complete even when the "
        "dispatcher's resolved timeout is proportionally smaller than "
        "the wait itself -- the job_id must never be discarded."
    )


@pytest.mark.asyncio
async def test_ci_search_logs_sequential_per_job_loop_is_not_killed_early() -> None:
    """Discriminating test: simulates gh_actions_search_logs /
    gitlab_ci_search_logs's real sequential-per-job loop shape -- N jobs
    in a single run/pipeline, each bounded by its own per-job floor --
    with a cumulative total that EXCEEDS a deadline shaped like a
    single-job-derived cap. Proves the search-logs-style handler is not
    killed early."""

    async def ci_search_logs_style_handler(arguments, user):
        matches = []
        for job_index in range(_CICD_JOB_COUNT):
            await asyncio.sleep(_CICD_SINGLE_JOB_FLOOR_SECONDS)
            matches.append({"job_id": job_index, "line": "match"})
        return {"success": True, "matches": matches, "match_count": len(matches)}

    user = _make_user()
    sig = inspect.signature(ci_search_logs_style_handler)

    result = await _invoke_handler(
        handler=ci_search_logs_style_handler,
        arguments={},
        user=user,
        session_state=None,
        sig=sig,
        is_async=True,
        timeout_seconds=_CICD_SINGLE_JOB_DERIVED_DEADLINE_SECONDS,
        tool_name="ci_search_logs",
    )

    assert result == {
        "success": True,
        "matches": [{"job_id": i, "line": "match"} for i in range(_CICD_JOB_COUNT)],
        "match_count": _CICD_JOB_COUNT,
    }, (
        "A CI/CD search_logs call whose cumulative sequential per-job "
        "runtime exceeds a single-job-derived deadline must still "
        "complete successfully -- it must never be killed early."
    )
