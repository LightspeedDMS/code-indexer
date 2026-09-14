"""Consolidated review finding H3 (Issue #1811/Bug #1812, Codex).

`_resolve_evaluator_code` (handlers/xray.py) can do real filesystem I/O
(`XrayPatternService.ensure_seed_patterns`'s mkdir/write_text, and
`_load_pattern`'s YAML read) AND spawn a git subprocess (`ensure_seed_
patterns`'s git add + git commit) when `pattern_name` is supplied.
`handle_xray_search`/`handle_xray_explore` call it DIRECTLY, on the event
loop thread -- neither `anyio.to_thread.run_sync` nor the dedicated
`xray_executor` this same file already uses for the actual search
execution. `cidx-meta` lives on a hard NFSv3 mount, where any of these
calls can block FOREVER, stalling every OTHER request on this node (the
project's explicit async-I/O invariant).

This test proves the async handler currently calls `_resolve_evaluator_
code` on the event loop's own thread rather than on a worker thread from
the SPECIFIC dedicated executor `_get_xray_executor()` returns (a named
`thread_name_prefix` makes the executor's own worker threads identifiable
by name, so this discriminates "offloaded to xray_executor specifically"
from "offloaded to some other executor" -- e.g. asyncio's default one).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

from code_indexer.server.auth.user_manager import User, UserRole

_DEDICATED_EXECUTOR_THREAD_NAME_PREFIX = "xray-h3-dedicated-executor"


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


PATTERN_PARAMS: Dict[str, Any] = {
    "repository_alias": "myrepo-global",
    "pattern": r"prepareStatement",
    "pattern_name": "some-stored-pattern",
    "search_target": "content",
}


def _assert_offloaded_to_dedicated_executor(
    thread_name: str, main_thread_name: str, label: str
) -> None:
    """Shared assertion: `label`'s captured thread must be neither the
    event loop's own thread nor some other, non-dedicated executor."""
    assert thread_name != main_thread_name, (
        f"{label} must not run directly on the event loop thread"
    )
    assert thread_name.startswith(_DEDICATED_EXECUTOR_THREAD_NAME_PREFIX), (
        f"{label} must run on the DEDICATED xray_executor (thread name "
        f"prefix {_DEDICATED_EXECUTOR_THREAD_NAME_PREFIX!r}), got {thread_name!r}"
    )


async def test_xray_search_pattern_resolution_runs_on_the_dedicated_xray_executor() -> (
    None
):
    """`_resolve_evaluator_code` must be invoked on a worker thread from the
    SPECIFIC dedicated `xray_executor`, never directly on the event loop
    thread (nor on some other, non-dedicated executor)."""
    from code_indexer.server.mcp.handlers.xray import handle_xray_search
    from code_indexer.server.mcp.handlers._utils import _mcp_response

    main_thread_name = threading.current_thread().name
    captured: Dict[str, str] = {}

    def _fake_resolve_evaluator_code(
        params: Dict[str, Any],
        repo_alias: str,
        default_evaluator: str = "",
        allow_default_evaluator: bool = False,
    ):
        captured["thread_name"] = threading.current_thread().name
        # Short-circuit the handler immediately after this call via a real
        # structured error response -- this test only needs to observe
        # WHICH thread called _resolve_evaluator_code, not the rest of the
        # search pipeline.
        return "", _mcp_response({"error": "pattern_not_found"})

    real_executor = ThreadPoolExecutor(
        max_workers=2, thread_name_prefix=_DEDICATED_EXECUTOR_THREAD_NAME_PREFIX
    )
    try:
        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_evaluator_code",
                side_effect=_fake_resolve_evaluator_code,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_xray_executor",
                return_value=real_executor,
            ),
        ):
            await handle_xray_search(dict(PATTERN_PARAMS), _make_user())
    finally:
        real_executor.shutdown(wait=True)

    assert "thread_name" in captured, "expected _resolve_evaluator_code to be called"
    _assert_offloaded_to_dedicated_executor(
        captured["thread_name"], main_thread_name, "_resolve_evaluator_code"
    )


async def test_ensure_seed_patterns_and_pattern_resolution_run_off_the_event_loop_thread() -> (
    None
):
    """Stronger, more targeted proof for the same finding: patches
    `XrayPatternService.ensure_seed_patterns` (mkdir/write_text/git add/git
    commit) and `resolve_and_prepare_pattern` (the YAML read) DIRECTLY --
    not the `_resolve_evaluator_code` wrapper -- so this keeps
    discriminating even if `_resolve_evaluator_code`'s internals are later
    refactored, as long as it still calls through `XrayPatternService`.
    Forces the module-level `_seeds_ensured` latch back to False so the
    seed-write path is genuinely exercised.
    """
    from code_indexer.server.mcp.handlers.xray import handle_xray_search
    from code_indexer.server.services.xray_pattern_service import XrayPatternService

    main_thread_name = threading.current_thread().name
    captured: Dict[str, str] = {}

    def _fake_ensure_seed_patterns(self: XrayPatternService) -> None:
        captured["ensure_seed_patterns"] = threading.current_thread().name

    def _fake_resolve_and_prepare_pattern(
        self: XrayPatternService,
        repo_alias: str,
        pattern_name: str,
        pattern_params: Any = None,
    ):
        captured["resolve_and_prepare_pattern"] = threading.current_thread().name
        raise ValueError("pattern_not_found: synthetic for this test")

    real_executor = ThreadPoolExecutor(
        max_workers=2, thread_name_prefix=_DEDICATED_EXECUTOR_THREAD_NAME_PREFIX
    )
    try:
        with (
            patch.object(
                XrayPatternService, "ensure_seed_patterns", _fake_ensure_seed_patterns
            ),
            patch.object(
                XrayPatternService,
                "resolve_and_prepare_pattern",
                _fake_resolve_and_prepare_pattern,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_xray_executor",
                return_value=real_executor,
            ),
            patch("code_indexer.server.mcp.handlers.xray._seeds_ensured", False),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_cidx_meta_path",
                return_value=Path("/tmp/fake-cidx-meta-for-offload-test"),
            ),
        ):
            await handle_xray_search(dict(PATTERN_PARAMS), _make_user())
    finally:
        real_executor.shutdown(wait=True)

    for label in ("ensure_seed_patterns", "resolve_and_prepare_pattern"):
        assert label in captured, f"expected {label} to be called"
        _assert_offloaded_to_dedicated_executor(
            captured[label], main_thread_name, label
        )
