"""Unit tests for Issue #1601 -- bounded read/parse of ripgrep/grep output.

RegexSearchService._search_ripgrep()/_search_grep() historically did an
unconditional ``open(temp_path).read()`` of ripgrep/grep's entire output
before any truncation logic ran, followed by a second full-string copy via
``.splitlines()``, and then unconditionally called ``json.loads()``/regex-
matched every remaining line just to compute an exact ``total`` -- even
after the returned ``matches`` list had already hit ``max_results``. A
broad, non-selective pattern against a large repository could therefore
read gigabytes into memory in one call, independent of ``max_results``.

These tests inject synthetic large-volume output AT the real
``open(temp_path).read()`` boundary (a real temp file, read by the real
production read/parse code) rather than through the ``SubprocessExecutor``
mock pattern used elsewhere in this test suite -- that existing mock
pattern hides this exact bug because it never exercises the real file-read
boundary (see Issue #1601's "Existing test coverage" section).
"""

from __future__ import annotations

import json
import shutil
import tracemalloc
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.global_repos.regex_search import (
    RegexSearchService,
    _BoundedLineReader,
)

# Synthetic-volume tuning: enough lines to comfortably exceed the small test
# byte ceiling below by a wide margin, proving the bound holds regardless of
# true volume.
_SYNTHETIC_LINE_COUNT = 20000
_TEST_BYTE_CEILING = 8192
# Peak traced allocation must stay within this multiple of the ceiling --
# generous enough to absorb chunk-buffer/object overhead, but far below the
# multi-hundred-KB the full synthetic file would cost if read unbounded.
_PEAK_MEMORY_CEILING_MULTIPLIER = 20
# search() call tuning for this test -- max_results deliberately huge so it
# is never the limiting factor, isolating the byte ceiling as the only cap.
_UNLIMITED_TEST_MAX_RESULTS = 1_000_000
_TEST_TIMEOUT_SECONDS = 10


def _write_synthetic_ripgrep_output(path: str, num_lines: int, file_prefix: str) -> int:
    """Write ``num_lines`` synthetic ripgrep --json match events to ``path``.

    Returns the number of bytes written.
    """
    lines = []
    for i in range(num_lines):
        event = {
            "type": "match",
            "data": {
                "path": {"text": f"{file_prefix}/file{i}.py"},
                "line_number": i + 1,
                "lines": {"text": f"def func_{i}_padding_xxxxxxxxxxxxxxxxxxxx():\n"},
                "submatches": [{"start": 0, "end": 3}],
            },
        }
        lines.append(json.dumps(event))
    content = "\n".join(lines) + "\n"
    with open(path, "w") as f:
        f.write(content)
    return len(content.encode("utf-8"))


@pytest.fixture
def ripgrep_service(tmp_path):
    """RegexSearchService pinned to the ripgrep engine."""
    with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
        mock_which.return_value = "/usr/bin/rg"
        return RegexSearchService(tmp_path)


def _mock_success_executor_copying_from(source_path: str):
    """Build a mocked SubprocessExecutor whose execute_with_limits copies a
    pre-built source file into the real output_file_path it is given, then
    reports a normal SUCCESS completion -- exercising the real read/parse
    boundary on a real temp file without spawning a real subprocess, and
    without generating the synthetic content inside any traced region."""

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


class TestRipgrepReadBound:
    """AC-A1: bytes actually read/parsed stay bounded regardless of volume."""

    @pytest.mark.asyncio
    async def test_bytes_processed_stay_bounded_far_beyond_the_ceiling(
        self, ripgrep_service, tmp_path
    ):
        """A synthetic volume far exceeding the byte ceiling must not cause
        proportional memory growth -- measured directly via tracemalloc,
        not inferred from RSS (which allocator retention can mask)."""
        import code_indexer.global_repos.regex_search as regex_search_module

        # Generate the synthetic source file BEFORE tracing starts -- the
        # generation itself (building 20000 JSON strings) must not count
        # toward the traced peak, only the production read/parse code path.
        source_path = tmp_path / "synthetic_rg_source.jsonl"
        written_bytes = _write_synthetic_ripgrep_output(
            str(source_path), _SYNTHETIC_LINE_COUNT, str(tmp_path)
        )
        assert written_bytes > _TEST_BYTE_CEILING, (
            "fixture must genuinely exceed the ceiling for this test to be "
            "discriminating"
        )

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
                await ripgrep_service._search_ripgrep(
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
                    matches, total = await ripgrep_service._search_ripgrep(
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

        # Direct measurement: peak traced allocation must stay in the same
        # order of magnitude as the ceiling, never proportional to the
        # (much larger) synthetic volume actually written to disk.
        assert peak < _TEST_BYTE_CEILING * _PEAK_MEMORY_CEILING_MULTIPLIER, (
            f"peak traced memory {peak} bytes is not bounded relative to "
            f"the {_TEST_BYTE_CEILING}-byte ceiling"
        )

        # The read was capped (byte ceiling hit before EOF/max_results), so
        # per the new contract total_matches is a lower bound, not exact,
        # and the returned match count is a strict partial slice of the
        # synthetic volume actually produced.
        assert ripgrep_service._last_search_read_capped is True
        assert 0 < len(matches) < _SYNTHETIC_LINE_COUNT
        # max_results was not the limiting factor here (it is enormous), so
        # every match observed before the byte ceiling stopped the scan was
        # appended -- total equals exactly what was returned.
        assert total == len(matches)


# AC-A3c table-driven contract test tuning.
_CONTRACT_NUM_LINES = 10
_CONTRACT_SMALL_MAX_RESULTS = 3
_CONTRACT_HUGE_MAX_RESULTS = 1_000_000


def _write_synthetic_ripgrep_output_with_offsets(
    path: str, num_lines: int, file_prefix: str
):
    """Like _write_synthetic_ripgrep_output, but also returns the exact
    cumulative byte offset immediately after each written line (including
    its trailing newline), so a test can pick a byte ceiling that lands
    precisely at a chosen line boundary."""
    offsets = []
    cumulative = 0
    with open(path, "w") as f:
        for i in range(num_lines):
            event = {
                "type": "match",
                "data": {
                    "path": {"text": f"{file_prefix}/file{i}.py"},
                    "line_number": i + 1,
                    "lines": {"text": f"def func_{i}():\n"},
                    "submatches": [{"start": 0, "end": 3}],
                },
            }
            line_bytes = (json.dumps(event) + "\n").encode("utf-8")
            f.write(line_bytes.decode("utf-8"))
            cumulative += len(line_bytes)
            offsets.append(cumulative)
    return offsets


def _build_contract_test_cases(
    num_lines: int, offsets: list, total_file_bytes: int
) -> list:
    """Build the AC-A3c table: one row per reachable
    (truncated, read_capped) combination."""
    byte_ceiling_generous = total_file_bytes * 2
    # offsets[1]: cumulative size through exactly the first 2 lines.
    byte_ceiling_only_first_two_lines = offsets[1]
    # offsets[_CONTRACT_SMALL_MAX_RESULTS]: cumulative size through exactly
    # the (max_results + 1)-th line -- the one whose match event triggers
    # the stop-on-truncation sentinel.
    byte_ceiling_at_truncation_boundary = offsets[_CONTRACT_SMALL_MAX_RESULTS]

    return [
        {
            "name": "i_complete_scan_no_cap",
            "max_results": _CONTRACT_HUGE_MAX_RESULTS,
            "byte_ceiling": byte_ceiling_generous,
            "expected_truncated": False,
            "expected_read_capped": False,
            "expected_matches_len": num_lines,
            "total_matches_exact": num_lines,
        },
        {
            "name": "ii_max_results_before_byte_ceiling",
            "max_results": _CONTRACT_SMALL_MAX_RESULTS,
            "byte_ceiling": byte_ceiling_generous,
            "expected_truncated": True,
            "expected_read_capped": False,
            "expected_matches_len": _CONTRACT_SMALL_MAX_RESULTS,
            "total_matches_min": _CONTRACT_SMALL_MAX_RESULTS + 1,
        },
        {
            "name": "iii_byte_ceiling_before_max_results",
            "max_results": _CONTRACT_HUGE_MAX_RESULTS,
            "byte_ceiling": byte_ceiling_only_first_two_lines,
            "expected_truncated": False,
            "expected_read_capped": True,
            "total_matches_min": 1,
            "total_matches_max": num_lines - 1,
        },
        {
            # Both thresholds crossed at effectively the same point: both
            # flags legitimately True at once, no crash/contradiction.
            "name": "iv_both_thresholds_simultaneously",
            "max_results": _CONTRACT_SMALL_MAX_RESULTS,
            "byte_ceiling": byte_ceiling_at_truncation_boundary,
            "expected_truncated": True,
            "expected_read_capped": True,
            "expected_matches_len": _CONTRACT_SMALL_MAX_RESULTS,
        },
    ]


def _assert_contract_case(result, case: dict) -> None:
    """Shared assertion body for every row of the AC-A3c table."""
    assert result.truncated is case["expected_truncated"], case["name"]
    assert result.read_capped is case["expected_read_capped"], case["name"]
    if "expected_matches_len" in case:
        assert len(result.matches) == case["expected_matches_len"], case["name"]
    if "total_matches_exact" in case:
        assert result.total_matches == case["total_matches_exact"], case["name"]
    if "total_matches_min" in case:
        assert result.total_matches >= case["total_matches_min"], case["name"]
    if "total_matches_max" in case:
        assert result.total_matches <= case["total_matches_max"], case["name"]


class TestRegexSearchResultContract:
    """AC-A3/A3b/A3c: truncated and read_capped are distinct, independently
    correct signals on the public RegexSearchService.search() result."""

    @pytest.mark.asyncio
    async def test_truncated_and_read_capped_are_independent_and_correct(
        self, tmp_path
    ):
        """Table-driven coverage of all 4 reachable (truncated, read_capped)
        combinations, per AC-A3c."""
        import code_indexer.global_repos.regex_search as regex_search_module

        source_path = tmp_path / "contract_source.jsonl"
        offsets = _write_synthetic_ripgrep_output_with_offsets(
            str(source_path), _CONTRACT_NUM_LINES, str(tmp_path)
        )
        total_file_bytes = offsets[-1]

        with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/rg"
            service = RegexSearchService(tmp_path)

        async def _run_search(max_results, byte_ceiling):
            mock_executor = _mock_success_executor_copying_from(str(source_path))
            with patch.object(regex_search_module, "_MAX_READ_BYTES", byte_ceiling):
                with patch(
                    "code_indexer.global_repos.regex_search.SubprocessExecutor",
                    return_value=mock_executor,
                ):
                    return await service.search(pattern="func", max_results=max_results)

        cases = _build_contract_test_cases(
            _CONTRACT_NUM_LINES, offsets, total_file_bytes
        )
        for case in cases:
            result = await _run_search(case["max_results"], case["byte_ceiling"])
            _assert_contract_case(result, case)


# AC-A5 chunk-boundary integrity tuning.
_GENEROUS_MAX_BYTES = 1024 * 1024
_CHUNK_SIZE_VARIANTS = [1, 3, 8, 16, 100, 4096]
# Chosen so the 2-byte UTF-8 encoding of 'e-acute' straddles an 8-byte chunk
# boundary exactly: a 7-byte ASCII prefix puts its first byte at offset 7
# (the last byte of the first 8-byte chunk) and its second byte at offset 8
# (the first byte of the next chunk) -- verified below via an explicit
# byte-offset assertion, not merely asserted by construction.
_SPLIT_CHAR_CHUNK_BYTES = 8
_SPLIT_CHAR_ASCII_PREFIX = "1234567"  # exactly 7 bytes
_SPLIT_CHAR = "é"  # e-acute, 2 bytes in UTF-8 (0xC3 0xA9)


class TestChunkBoundaryIntegrity:
    """AC-A5: a chunk boundary landing mid-record must never corrupt or
    merge a line, nor truncate/merge context_before/context_after."""

    @pytest.mark.parametrize("chunk_bytes", _CHUNK_SIZE_VARIANTS)
    def test_bounded_line_reader_reconstructs_exact_lines_across_chunk_sizes(
        self, tmp_path, chunk_bytes
    ):
        """Every line must come back byte-for-byte identical regardless of
        how small/large chunk_bytes is, across a range of chunk sizes."""
        original_lines = [
            "first line of plain ascii content",
            f"{_SPLIT_CHAR_ASCII_PREFIX}{_SPLIT_CHAR}89 rest of line content",
            "third line, deliberately long to cross several tiny chunks",
            "",  # an empty line must round-trip too
            "final line with no trailing corruption",
        ]
        content = "\n".join(original_lines) + "\n"
        path = tmp_path / f"chunked_source_{chunk_bytes}.txt"
        path.write_text(content, encoding="utf-8")

        reader = _BoundedLineReader(
            str(path), _GENEROUS_MAX_BYTES, chunk_bytes=chunk_bytes
        )
        recovered_lines = list(reader)

        assert recovered_lines == original_lines
        assert reader.read_capped is False

    def test_bounded_line_reader_handles_utf8_character_split_exactly_at_boundary(
        self, tmp_path
    ):
        """Deterministic proof the incremental UTF-8 decoder correctly
        reassembles a multi-byte character whose two bytes are split
        across two DIFFERENT chunk reads (not merely a coincidence of a
        parametrized size)."""
        line = f"{_SPLIT_CHAR_ASCII_PREFIX}{_SPLIT_CHAR}89 rest of line content"
        encoded = line.encode("utf-8")
        # Verify the split actually happens where this test claims it does,
        # rather than assuming it from the string construction alone.
        assert len(_SPLIT_CHAR_ASCII_PREFIX.encode("utf-8")) == 7
        assert encoded[7:9] == _SPLIT_CHAR.encode("utf-8")

        content = line + "\n"
        path = tmp_path / "split_char_source.txt"
        path.write_text(content, encoding="utf-8")

        reader = _BoundedLineReader(
            str(path), _GENEROUS_MAX_BYTES, chunk_bytes=_SPLIT_CHAR_CHUNK_BYTES
        )
        recovered_lines = list(reader)

        assert recovered_lines == [line]

    @pytest.mark.asyncio
    async def test_context_accumulation_survives_tiny_chunk_boundaries(self, tmp_path):
        """context_before/context_after must be complete and correctly
        attributed even when every underlying JSON event line is split
        across several tiny internal chunk reads."""
        with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/rg"
            service = RegexSearchService(tmp_path)

        events = [
            {
                "type": "context",
                "data": {
                    "path": {"text": "src/main.py"},
                    "line_number": 1,
                    "lines": {"text": "# a context line before the match\n"},
                },
            },
            {
                "type": "match",
                "data": {
                    "path": {"text": "src/main.py"},
                    "line_number": 2,
                    "lines": {"text": "def authenticate_user(username):\n"},
                    "submatches": [{"start": 4, "end": 21}],
                },
            },
            {
                "type": "context",
                "data": {
                    "path": {"text": "src/main.py"},
                    "line_number": 3,
                    "lines": {"text": "    return True\n"},
                },
            },
        ]
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("placeholder\n")
        content = "\n".join(json.dumps(e) for e in events) + "\n"
        path = tmp_path / "context_source.jsonl"
        path.write_text(content, encoding="utf-8")

        reader = _BoundedLineReader(str(path), _GENEROUS_MAX_BYTES, chunk_bytes=8)
        matches, total = service._parse_ripgrep_json_output(
            reader, max_results=100, context_lines=1
        )

        assert total == 1
        assert len(matches) == 1
        assert matches[0].context_before == ["# a context line before the match"]
        assert matches[0].context_after == ["    return True"]
