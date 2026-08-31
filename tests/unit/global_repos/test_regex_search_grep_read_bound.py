"""Unit tests for Issue #1601 -- bounded read/parse of grep output (AC-A2/D1).

Mirrors test_regex_search_read_bound.py's ripgrep coverage for the grep
fallback engine's temp-file read path (_search_grep / _parse_grep_output).
"""

from __future__ import annotations

import shutil
import tracemalloc
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService

_SYNTHETIC_LINE_COUNT = 20000
_TEST_BYTE_CEILING = 8192
_PEAK_MEMORY_CEILING_MULTIPLIER = 20
_UNLIMITED_TEST_MAX_RESULTS = 1_000_000
_TEST_TIMEOUT_SECONDS = 10


def _write_synthetic_grep_output(path: str, num_lines: int) -> int:
    """Write ``num_lines`` synthetic grep-format match lines to ``path``.

    Format: "relative/path.py:LINENUM:content" (colon-separated, matching
    grep -n -H output).

    Returns the number of bytes written.
    """
    lines = [
        f"file{i}.py:{i + 1}:def func_{i}_padding_xxxxxxxxxxxxxxxxxxxx():"
        for i in range(num_lines)
    ]
    content = "\n".join(lines) + "\n"
    with open(path, "w") as f:
        f.write(content)
    return len(content.encode("utf-8"))


@pytest.fixture
def grep_service(tmp_path):
    """RegexSearchService pinned to the grep (non-ripgrep) engine."""
    with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
        mock_which.side_effect = lambda cmd: "/usr/bin/grep" if cmd == "grep" else None
        return RegexSearchService(tmp_path)


def _mock_success_executor_copying_from(source_path: str):
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


class TestGrepReadBound:
    """AC-A2/D1: bytes actually read/parsed stay bounded regardless of
    volume, mirroring the ripgrep fix exactly for the grep engine."""

    @pytest.mark.asyncio
    async def test_bytes_processed_stay_bounded_far_beyond_the_ceiling(
        self, grep_service, tmp_path
    ):
        """A synthetic volume far exceeding the byte ceiling must not cause
        proportional memory growth -- measured directly via tracemalloc."""
        import code_indexer.global_repos.regex_search as regex_search_module

        source_path = tmp_path / "synthetic_grep_source.txt"
        written_bytes = _write_synthetic_grep_output(
            str(source_path), _SYNTHETIC_LINE_COUNT
        )
        assert written_bytes > _TEST_BYTE_CEILING

        with patch.object(regex_search_module, "_MAX_READ_BYTES", _TEST_BYTE_CEILING):
            mock_executor = _mock_success_executor_copying_from(str(source_path))
            with patch(
                "code_indexer.global_repos.regex_search.SubprocessExecutor",
                return_value=mock_executor,
            ):
                # Issue #1601 test-isolation fix: an UNTRACED priming call
                # runs the exact same operation once before measurement.
                # Verified via tracemalloc instrumentation that a clean
                # start()/stop() cycle always resets traced ``current`` to
                # 0 (so a leftover-baseline theory does not hold) -- the
                # real flake is TRANSIENT peak-during-the-call varying by
                # 10-30KB depending on what ran earlier in the same pytest
                # process (e.g. one-time interpreter-global cache growth
                # such as the ``re`` module's compiled-pattern cache, or
                # allocator arena first-touch cost). That one-time cost is
                # order-dependent and unrelated to the byte-ceiling defect
                # this test exists to catch. Running the operation once,
                # untraced, absorbs it; the traced call below then measures
                # only this invocation's own genuine allocation.
                await grep_service._search_grep(
                    pattern="func",
                    search_path=tmp_path,
                    include_patterns=None,
                    exclude_patterns=None,
                    case_sensitive=True,
                    context_lines=0,
                    max_results=_UNLIMITED_TEST_MAX_RESULTS,
                    timeout_seconds=_TEST_TIMEOUT_SECONDS,
                )
                tracemalloc.start()
                tracemalloc.reset_peak()
                try:
                    matches, total = await grep_service._search_grep(
                        pattern="func",
                        search_path=tmp_path,
                        include_patterns=None,
                        exclude_patterns=None,
                        case_sensitive=True,
                        context_lines=0,
                        max_results=_UNLIMITED_TEST_MAX_RESULTS,
                        timeout_seconds=_TEST_TIMEOUT_SECONDS,
                    )
                    _current, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()

        assert peak < _TEST_BYTE_CEILING * _PEAK_MEMORY_CEILING_MULTIPLIER, (
            f"peak traced memory {peak} bytes is not bounded relative to "
            f"the {_TEST_BYTE_CEILING}-byte ceiling"
        )
        assert grep_service._last_search_read_capped is True
        assert 0 < len(matches) < _SYNTHETIC_LINE_COUNT
        assert total == len(matches)
