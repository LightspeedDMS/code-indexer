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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.global_repos.regex_search import (
    RegexSearchService,
    _BoundedLineReader,
)

# Synthetic-volume tuning: enough lines to comfortably exceed the small test
# byte ceiling below by a wide margin, proving the bound holds regardless of
# true volume. _SYNTHETIC_LINE_COUNT_10X is a second, 10x-larger volume used
# for a differential comparison (Bug #1852 Item 1): bytes actually read must
# be IDENTICAL at both volumes, not merely "under some absolute number" --
# see test_bytes_actually_read_stay_bounded_and_do_not_scale_with_match_volume.
_SYNTHETIC_LINE_COUNT = 20000
_SYNTHETIC_LINE_COUNT_10X = _SYNTHETIC_LINE_COUNT * 10
_TEST_BYTE_CEILING = 8192
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


async def _run_capped_search(service, tmp_path, num_lines: int, label: str):
    """Run one byte-ceiling-capped search; return (matches, total,
    bytes_read, read_capped). Bug #1852 Item 1: asserts belong to the
    caller -- this only collects the production code's own counters."""
    import code_indexer.global_repos.regex_search as regex_search_module

    source_path = tmp_path / f"synthetic_rg_source_{label}.jsonl"
    written_bytes = _write_synthetic_ripgrep_output(
        str(source_path), num_lines, str(tmp_path)
    )
    assert written_bytes > _TEST_BYTE_CEILING, (
        "fixture must genuinely exceed the ceiling for this test to be discriminating"
    )
    with patch.object(regex_search_module, "_MAX_READ_BYTES", _TEST_BYTE_CEILING):
        mock_executor = _mock_success_executor_copying_from(str(source_path))
        with patch(
            "code_indexer.global_repos.regex_search.SubprocessExecutor",
            return_value=mock_executor,
        ):
            matches, total = await service._search_ripgrep(
                pattern="func",
                search_path=tmp_path,
                include_patterns=None,
                exclude_patterns=None,
                case_sensitive=True,
                context_lines=0,
                max_results=_UNLIMITED_TEST_MAX_RESULTS,
                timeout_seconds=_TEST_TIMEOUT_SECONDS,
            )
    return (
        matches,
        total,
        service._last_read_capped_bytes,
        service._last_search_read_capped,
    )


class TestRipgrepReadBound:
    """AC-A1: bytes actually read/parsed stay bounded regardless of volume."""

    @pytest.mark.asyncio
    async def test_bytes_actually_read_stay_bounded_and_do_not_scale_with_match_volume(
        self, ripgrep_service, tmp_path
    ):
        """Bug #1852 Item 1 fix: assert on the production code's own exact
        byte counter (``_BoundedLineReader.bytes_read``, via
        ``_last_read_capped_bytes``) instead of an absolute ``tracemalloc``
        peak, which is process-wide and picks up unrelated concurrent
        threads. Bytes read at 10x match volume must equal bytes read at 1x.

        Investigation finding: driving the real ``_BoundedLineReader``
        directly through its own ``max_bytes`` parameter at 1x and 10x
        match volume showed bytes_read == 8192 (the configured ceiling) at
        BOTH volumes when the ceiling is enforced -- equal, not merely
        bounded -- versus 3,406,674 (1x) vs 34,666,675 (10x) when the
        ceiling is removed, which scales linearly with volume as expected
        for an unbounded read. This confirms the read bound is genuinely
        enforced; the 10.5 MB ``tracemalloc`` peak that used to fail this
        test under the old measurement approach came from unrelated
        allocations elsewhere in the ~16,000-test process, since
        ``tracemalloc`` measures the whole process, not this call alone."""
        matches_1x, total_1x, bytes_read_1x, capped_1x = await _run_capped_search(
            ripgrep_service, tmp_path, _SYNTHETIC_LINE_COUNT, "1x"
        )
        matches_10x, total_10x, bytes_read_10x, capped_10x = await _run_capped_search(
            ripgrep_service, tmp_path, _SYNTHETIC_LINE_COUNT_10X, "10x"
        )

        # Exact, deterministic invariant guaranteed by _BoundedLineReader
        # itself: never exceeds the ceiling, at either volume.
        assert bytes_read_1x <= _TEST_BYTE_CEILING
        assert bytes_read_10x <= _TEST_BYTE_CEILING
        assert capped_1x is True
        assert capped_10x is True

        # Differential proof: 10x the match volume must not read a single
        # byte more than 1x did -- deterministic equality, no
        # measured-resource magnitude or multiplier involved.
        assert bytes_read_10x == bytes_read_1x, (
            f"bytes actually read scaled with match volume: {bytes_read_1x} "
            f"(1x) vs {bytes_read_10x} (10x) -- read is not truly bounded"
        )

        # read_capped (byte ceiling hit before EOF/max_results) makes
        # total_matches a lower bound, not exact, per the #1601 contract.
        assert 0 < len(matches_1x) < _SYNTHETIC_LINE_COUNT
        assert 0 < len(matches_10x) < _SYNTHETIC_LINE_COUNT_10X
        # max_results was never the limiting factor (it is enormous), so
        # every match observed before the byte ceiling stopped the scan was
        # appended -- total equals exactly what was returned.
        assert total_1x == len(matches_1x)
        assert total_10x == len(matches_10x)


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
