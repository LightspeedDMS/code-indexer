"""Issue #1601 remediation Priority 2: regex_search.py must never run its
synchronous read/parse/scan work directly on the asyncio event loop.

Both ``_search_ripgrep`` and ``_search_grep`` are ``async def`` but, before
this fix, iterated a ``_BoundedLineReader`` (open()/read()/decode/split) and
parsed JSON/grep-format lines directly on the calling coroutine.
``_search_python_multiline`` is a fully synchronous method (its own
``os.walk`` + per-file ``open()``/``read()``) called directly from the async
``_search_grep`` with no thread offload at all.

This project's own CLAUDE.md states the invariant plainly: "NEVER call a
synchronous filesystem/network function directly inside `async def` -- it
blocks the WHOLE event loop... Offload with `anyio.to_thread.run_sync(...)`."
At the project's documented ~900-repo production scale, a single regex
search's parse work blocking the event loop stalls the ENTIRE server, not
just that one request.

Discriminating test strategy: this is a THREAD-IDENTITY check, not a timing
heuristic. Each test wraps the specific synchronous method under test
(``_parse_ripgrep_json_output``, ``_parse_grep_output``, or
``_search_python_multiline`` itself) so it records ``threading.get_ident()``
at the moment it actually executes, then compares that to the identity of
the thread driving the test's own event loop (captured before/after the
``await``, which is necessarily the same OS thread throughout, since
asyncio never migrates a coroutine's own execution across threads -- only a
genuine worker-thread offload, e.g. via ``anyio.to_thread.run_sync``,
introduces a different thread identity). This is deterministic regardless
of data size, hardware speed, or scheduling: un-offloaded code ALWAYS
records the same thread identity as the caller; offloaded code ALWAYS
records a different one.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService

_TEST_MAX_RESULTS = 1_000_000


def _capture_thread_and_delegate(
    bound_method: Callable, captured: Dict[str, int]
) -> Callable:
    """Wrap ``bound_method`` so calling it records the OS thread identity
    it actually executed on, then delegates to the real implementation."""

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        captured["thread_id"] = threading.get_ident()
        return bound_method(*args, **kwargs)

    return _wrapped


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


def _mock_executor_copying_from(source_path: str):
    """A mocked SubprocessExecutor whose execute_with_limits instantly
    "completes" by copying pre-built synthetic content into the real
    output_file_path it is given -- isolates the test to the read+parse
    work itself, not real subprocess/rg overhead."""
    import shutil

    async def _side_effect(**kwargs):
        shutil.copyfile(source_path, kwargs["output_file_path"])
        result = MagicMock()
        result.timed_out = False
        result.status = "success"
        result.exit_code = 0
        result.stderr_output = None
        result.output_capped = False
        return result

    mock_executor = MagicMock()
    mock_executor.execute_with_limits = AsyncMock(side_effect=_side_effect)
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
        "ripgrep",
        _write_synthetic_ripgrep_json,
        "_parse_ripgrep_json_output",
        "_search_ripgrep",
        id="ripgrep",
    ),
    pytest.param(
        "grep",
        _write_synthetic_grep_output,
        "_parse_grep_output",
        "_search_grep",
        id="grep",
    ),
]


class TestRegexSearchEventLoopOffload:
    """Priority 2: sync read/parse/scan work must run off the event-loop
    thread, proven via thread-identity capture (not timing)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine, writer, parse_attr, search_attr", _ENGINE_CASES)
    async def test_parse_runs_off_event_loop_thread(
        self, tmp_path, engine, writer, parse_attr, search_attr
    ):
        source_path = tmp_path / f"source_{engine}.out"
        writer(str(source_path))
        service = _build_service(tmp_path, engine)

        caller_thread_id = threading.get_ident()
        captured: Dict[str, int] = {}
        mock_executor = _mock_executor_copying_from(str(source_path))
        original_parse_method = getattr(service, parse_attr)
        with (
            patch(
                "code_indexer.global_repos.regex_search.SubprocessExecutor",
                return_value=mock_executor,
            ),
            patch.object(
                service,
                parse_attr,
                side_effect=_capture_thread_and_delegate(
                    original_parse_method, captured
                ),
            ),
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

        assert "thread_id" in captured, f"{parse_attr} was never invoked"
        assert captured["thread_id"] != caller_thread_id, (
            f"{engine} output parse ran on the event-loop thread instead "
            f"of being offloaded to a worker thread via "
            f"anyio.to_thread.run_sync"
        )
        # Still on the same (event-loop) thread after the await returns --
        # proves the comparison above is meaningful, not an artifact of the
        # test itself having moved threads.
        assert threading.get_ident() == caller_thread_id

    @pytest.mark.asyncio
    async def test_search_python_multiline_scan_runs_off_event_loop_thread(
        self, tmp_path
    ):
        """_search_python_multiline (os.walk + per-file read + re.DOTALL
        scan) is fully synchronous and reached via _search_grep's
        multiline=True branch with no thread offload at all -- the
        deficiency Priority 2 calls out by name. Structurally different
        setup from the parametrized case above (no subprocess/executor
        involved at all), so kept as its own test."""
        (tmp_path / "f0.py").write_text(
            "class Foo:\n    def login(self):\n        pass\n"
        )

        service = _build_service(tmp_path, "grep")

        caller_thread_id = threading.get_ident()
        captured: Dict[str, int] = {}
        with patch.object(
            service,
            "_search_python_multiline",
            side_effect=_capture_thread_and_delegate(
                service._search_python_multiline, captured
            ),
        ):
            await service._search_grep(
                pattern=r"class[\s\S]*login",
                search_path=tmp_path,
                include_patterns=None,
                exclude_patterns=None,
                case_sensitive=True,
                context_lines=0,
                max_results=_TEST_MAX_RESULTS,
                timeout_seconds=30,
                multiline=True,
            )

        assert "thread_id" in captured, "_search_python_multiline was never invoked"
        assert captured["thread_id"] != caller_thread_id, (
            "Python multiline os.walk/read/scan ran on the event-loop "
            "thread instead of being offloaded to a worker thread via "
            "anyio.to_thread.run_sync"
        )
        assert threading.get_ident() == caller_thread_id
