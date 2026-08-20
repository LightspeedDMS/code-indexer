"""Issue #1601 remediation round 5, Priority 2 (REQUIRED -- Codex High
finding; investigated for real, not taken at face value or dismissed).

Codex's finding named ``subprocess_executor.py``'s ``execute_with_limits``
around lines 160-170 (the outer ``asyncio.wait_for`` deadline), but also
separately asked whether ``executor.shutdown(wait=True)`` is "genuinely
still synchronous and still on the async path". Investigation confirms
it is real, but the call sites themselves live in THIS module
(``regex_search.py``), not in ``subprocess_executor.py``: ``_search_ripgrep``,
``_search_grep``, and ``_find_files_by_patterns`` each construct a fresh
``SubprocessExecutor`` and call ``executor.shutdown(wait=True)`` directly
inside their own ``finally`` block, on the calling coroutine's own
(event-loop) thread -- never offloaded.

``ThreadPoolExecutor.shutdown(wait=True)`` is a genuine synchronous,
potentially-blocking call: if ``asyncio.wait_for``'s outer deadline in
``execute_with_limits`` ever DOES fire while the underlying worker thread
is still running (the exact race Priority 2's other half addresses by
sizing that deadline correctly), the worker thread keeps running to
completion regardless of the cancellation, and a subsequent
``executor.shutdown(wait=True)`` on this module's own async call sites
would then block the event loop for however long that orphaned thread
takes -- violating this project's "never call a synchronous
filesystem/network function directly inside async def" invariant.

This test proves, via the same thread-identity-capture technique already
established in ``test_regex_search_event_loop_offload_1601.py`` (a
deterministic, timing-independent check: un-offloaded code always
records the SAME thread identity as the caller; genuinely offloaded code
never does), that ``executor.shutdown`` runs on a worker thread, not the
event-loop thread, for both engines.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService

_TEST_MAX_RESULTS = 100


def _write_synthetic_ripgrep_json(path: str) -> None:
    event = {
        "type": "match",
        "data": {
            "path": {"text": "file1.py"},
            "line_number": 1,
            "lines": {"text": "def func_1():\n"},
            "submatches": [{"start": 0, "end": 3}],
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _write_synthetic_grep_output(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("file1.py:1:def func_1():\n")


def _mock_executor_copying_from(source_path: str, shutdown_captured: Dict[str, int]):
    """A mocked SubprocessExecutor whose execute_with_limits instantly
    "completes" by copying pre-built synthetic content into the real
    output_file_path it is given, and whose ``shutdown`` records the OS
    thread identity it was actually called from."""
    import shutil

    async def _side_effect(**kwargs: Any) -> Any:
        shutil.copyfile(source_path, kwargs["output_file_path"])
        result = MagicMock()
        result.timed_out = False
        result.status = "success"
        result.exit_code = 0
        result.stderr_output = None
        result.output_capped = False
        return result

    def _shutdown_side_effect(*args: Any, **kwargs: Any) -> None:
        shutdown_captured["thread_id"] = threading.get_ident()

    mock_executor = MagicMock()
    mock_executor.execute_with_limits = AsyncMock(side_effect=_side_effect)
    mock_executor.shutdown = MagicMock(side_effect=_shutdown_side_effect)
    return mock_executor


def _build_service(tmp_path, engine: str) -> RegexSearchService:
    with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
        if engine == "ripgrep":
            mock_which.return_value = "/usr/bin/rg"
        else:
            mock_which.side_effect = (
                lambda cmd: "/usr/bin/grep" if cmd == "grep" else None
            )
        return RegexSearchService(tmp_path)


_ENGINE_CASES = [
    pytest.param(
        "ripgrep", _write_synthetic_ripgrep_json, "_search_ripgrep", id="ripgrep"
    ),
    pytest.param("grep", _write_synthetic_grep_output, "_search_grep", id="grep"),
]


class TestExecutorShutdownOffload:
    """Priority 2: SubprocessExecutor.shutdown(wait=True) must never run
    directly on the event-loop thread."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine, writer, search_attr", _ENGINE_CASES)
    async def test_executor_shutdown_runs_off_event_loop_thread(
        self, tmp_path, engine, writer, search_attr
    ):
        source_path = tmp_path / f"source_{engine}.out"
        writer(str(source_path))
        service = _build_service(tmp_path, engine)

        caller_thread_id = threading.get_ident()
        shutdown_captured: Dict[str, int] = {}
        mock_executor = _mock_executor_copying_from(str(source_path), shutdown_captured)

        with patch(
            "code_indexer.global_repos.regex_search.SubprocessExecutor",
            return_value=mock_executor,
        ):
            search_method = getattr(service, search_attr)
            await search_method(
                pattern="func",
                search_path=tmp_path,
                include_patterns=None,
                exclude_patterns=None,
                case_sensitive=True,
                context_lines=0,
                max_results=_TEST_MAX_RESULTS,
                timeout_seconds=30,
            )

        assert "thread_id" in shutdown_captured, "executor.shutdown was never called"
        assert shutdown_captured["thread_id"] != caller_thread_id, (
            f"{search_attr}'s executor.shutdown(wait=True) ran on the "
            f"event-loop thread instead of being offloaded to a worker "
            f"thread via anyio.to_thread.run_sync"
        )
        assert threading.get_ident() == caller_thread_id
