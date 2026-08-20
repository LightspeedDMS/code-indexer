"""Unit test for Issue #1601 Priority 9 -- ``bytes_read_at_cutoff`` must
report the real cutoff of whichever mechanism actually triggered the cap.

When the executor's byte cap killed the still-running SUBPROCESS
(``result.output_capped=True``), the real cutoff is the output file's
on-disk size at that moment -- which can be much LARGER than the
reader's own ``bytes_read`` if the consumer's ``max_results`` stopped
parsing after only the first internal chunk. Reporting the reader's
(smaller) ``bytes_read`` in that case understates the actual cutoff.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService, _READ_CHUNK_BYTES

_TEST_MAX_RESULTS = 1  # stop parsing after the very first match
_SYNTHETIC_EVENT_COUNT = 2000
_KILLED_EXIT_CODE = -9
_TEST_TIMEOUT_SECONDS = 30


def _write_synthetic_ripgrep_json_exceeding_one_chunk(path: str) -> int:
    """Write enough synthetic ripgrep --json match lines that the total
    file size exceeds _READ_CHUNK_BYTES (one internal reader chunk).
    Returns the real on-disk byte size written."""
    lines = []
    for i in range(_SYNTHETIC_EVENT_COUNT):
        event = {
            "type": "match",
            "data": {
                "path": {"text": f"file{i}.py"},
                "line_number": i + 1,
                "lines": {
                    "text": f"def func_{i}_padding_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx():\n"
                },
                "submatches": [{"start": 0, "end": 3}],
            },
        }
        lines.append(json.dumps(event))
    content = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    written_bytes = len(content.encode("utf-8"))
    assert written_bytes > _READ_CHUNK_BYTES, (
        "fixture must genuinely exceed one reader chunk for this test to "
        "be discriminating"
    )
    return written_bytes


def _mock_output_capped_executor(source_path: str):
    """A mocked SubprocessExecutor reporting output_capped=True (the
    subprocess was killed by the byte cap), having copied the real
    synthetic content into the real output_file_path it is given."""
    import shutil

    async def _side_effect(**kwargs):
        shutil.copyfile(source_path, kwargs["output_file_path"])
        result = MagicMock()
        result.timed_out = False
        result.status = "success"
        result.exit_code = _KILLED_EXIT_CODE
        result.stderr_output = None
        result.output_capped = True
        return result

    mock_executor = MagicMock()
    mock_executor.execute_with_limits = AsyncMock(side_effect=_side_effect)
    return mock_executor


class TestBytesReadAtCutoffCorrectness:
    """Priority 9: bytes_read_at_cutoff reflects whichever mechanism
    actually triggered the cap, not always the reader's own bytes_read."""

    @pytest.mark.asyncio
    async def test_reports_real_file_size_when_output_capped_triggered_it(
        self, tmp_path
    ):
        source_path = tmp_path / "source.jsonl"
        real_file_size = _write_synthetic_ripgrep_json_exceeding_one_chunk(
            str(source_path)
        )

        with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/rg"
            service = RegexSearchService(tmp_path)

        mock_executor = _mock_output_capped_executor(str(source_path))
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
                max_results=_TEST_MAX_RESULTS,
                timeout_seconds=_TEST_TIMEOUT_SECONDS,
            )

        assert len(matches) == 1  # parsing genuinely stopped after 1 match
        assert service._last_search_read_capped is True
        assert service._last_read_capped_bytes == real_file_size, (
            f"expected the real cutoff ({real_file_size} bytes, the "
            f"actual output file size when the subprocess was killed), "
            f"got {service._last_read_capped_bytes} (looks like the "
            f"reader's own early-stopped bytes_read was reported instead)"
        )
