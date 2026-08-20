"""Issue #1601 remediation round 5, Priority 1 (REQUIRED -- Codex Critical
finding).

Round 4's fix added ``_MAX_MATCH_CONTENT_BYTES`` (256 KiB), bounding any
SINGLE match's content. But nothing bounds the TOTAL across all matches
+ their context lines accumulated in one ``search()`` call: a request
with a large ``max_results`` and non-trivial ``context_lines`` can still
accumulate far more memory than any single safety budget -- the same
class of risk #1601 exists to close, just redistributed across many
objects instead of one.

This proves the fix at the ripgrep engine's real parse path
(``RegexSearchService.search()``, mocking only the subprocess boundary
-- the same "only mock the subprocess, exercise the real parser"
convention already established in
``test_regex_search_event_loop_offload_1601.py``):

(a) A request producing many large matches is capped by the new
    aggregate budget BEFORE max_results is reached, with
    ``read_capped=True`` and ``truncated=False`` (the scan does not know
    whether more than max_results matches existed -- the budget, not
    max_results, is what stopped it).
(b) A normal request comfortably under the budget is completely
    unaffected: exact match count, ``read_capped=False``,
    ``truncated=False``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import code_indexer.global_repos.regex_search as regex_search_module
from code_indexer.global_repos.regex_search import RegexSearchService

# Reduced test budget (patched onto the module constant, read at call
# time -- see _ResultContentBudget's docstring for why this is
# patchable, mirroring the established _MAX_MATCH_CONTENT_BYTES pattern
# in test_regex_search_match_content_bound_1601.py) so the test is fast
# and the expected match count is exact, not a multi-megabyte fixture.
_TEST_TOTAL_BUDGET_BYTES = 4096
# Comfortably under _MAX_MATCH_CONTENT_BYTES (256 KiB) so each match's
# content passes through _bounded_match_content untouched -- isolating
# this test to the AGGREGATE budget, not the per-match one.
_MATCH_CONTENT_BYTES = 500


def _write_many_match_events(path: str, num_events: int, content_bytes: int) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i in range(num_events):
            event = {
                "type": "match",
                "data": {
                    "path": {"text": f"file{i}.py"},
                    "line_number": 1,
                    "lines": {"text": "x" * content_bytes},
                    "submatches": [{"start": 0, "end": 1}],
                },
            }
            f.write(json.dumps(event) + "\n")


def _mock_executor_copying_from(source_path: str):
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


def _build_service(tmp_path) -> RegexSearchService:
    with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
        mock_which.return_value = "/usr/bin/rg"
        return RegexSearchService(tmp_path)


class TestAggregateResultContentBudget:
    """Priority 1: an aggregate byte budget across all accumulated
    match/context content, independent of max_results."""

    @pytest.mark.asyncio
    async def test_many_large_matches_capped_before_max_results(self, tmp_path):
        source_path = tmp_path / "many_matches.jsonl"
        num_events = 50  # comfortably more than the reduced budget can hold
        _write_many_match_events(str(source_path), num_events, _MATCH_CONTENT_BYTES)

        service = _build_service(tmp_path)
        mock_executor = _mock_executor_copying_from(str(source_path))

        with (
            patch(
                "code_indexer.global_repos.regex_search.SubprocessExecutor",
                return_value=mock_executor,
            ),
            patch.object(
                regex_search_module,
                "_MAX_TOTAL_RESULT_CONTENT_BYTES",
                _TEST_TOTAL_BUDGET_BYTES,
            ),
        ):
            result = await service.search(
                pattern="x+",
                max_results=num_events,  # generous -- the budget must trip first
            )

        expected_matches = _TEST_TOTAL_BUDGET_BYTES // _MATCH_CONTENT_BYTES
        assert len(result.matches) == expected_matches, (
            f"expected exactly {expected_matches} matches before the "
            f"{_TEST_TOTAL_BUDGET_BYTES}-byte aggregate budget tripped, "
            f"got {len(result.matches)}"
        )
        assert len(result.matches) < num_events
        assert result.read_capped is True
        # Not "truncated" in the max_results sense: the scan does not
        # know whether more than max_results matches existed -- the
        # aggregate budget, not max_results, stopped it.
        assert result.truncated is False

    @pytest.mark.asyncio
    async def test_normal_request_well_under_budget_is_unaffected(self, tmp_path):
        source_path = tmp_path / "few_matches.jsonl"
        num_events = 3
        _write_many_match_events(str(source_path), num_events, _MATCH_CONTENT_BYTES)

        service = _build_service(tmp_path)
        mock_executor = _mock_executor_copying_from(str(source_path))

        with patch(
            "code_indexer.global_repos.regex_search.SubprocessExecutor",
            return_value=mock_executor,
        ):
            result = await service.search(pattern="x+", max_results=100)

        assert len(result.matches) == num_events
        assert result.total_matches == num_events
        assert result.read_capped is False
        assert result.truncated is False


def _write_many_grep_match_lines(
    path: str, num_events: int, content_bytes: int
) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i in range(num_events):
            f.write(f"file{i}.py:1:{'x' * content_bytes}\n")


def _build_grep_service(tmp_path) -> RegexSearchService:
    with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
        mock_which.side_effect = lambda cmd: (
            "/usr/bin/grep" if cmd == "grep" else None
        )
        return RegexSearchService(tmp_path)


class TestAggregateResultContentBudgetGrepEngine:
    """Priority 1 parity: the aggregate budget applies identically on the
    grep engine's parse path, not just ripgrep's."""

    @pytest.mark.asyncio
    async def test_many_large_matches_capped_before_max_results_grep(self, tmp_path):
        source_path = tmp_path / "many_matches.grep"
        num_events = 50
        _write_many_grep_match_lines(str(source_path), num_events, _MATCH_CONTENT_BYTES)

        service = _build_grep_service(tmp_path)
        mock_executor = _mock_executor_copying_from(str(source_path))

        with (
            patch(
                "code_indexer.global_repos.regex_search.SubprocessExecutor",
                return_value=mock_executor,
            ),
            patch.object(
                regex_search_module,
                "_MAX_TOTAL_RESULT_CONTENT_BYTES",
                _TEST_TOTAL_BUDGET_BYTES,
            ),
        ):
            result = await service.search(pattern="x+", max_results=num_events)

        expected_matches = _TEST_TOTAL_BUDGET_BYTES // _MATCH_CONTENT_BYTES
        assert len(result.matches) == expected_matches
        assert len(result.matches) < num_events
        assert result.read_capped is True
        assert result.truncated is False
