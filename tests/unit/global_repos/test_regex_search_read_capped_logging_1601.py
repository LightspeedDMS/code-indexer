"""Unit test for Issue #1601 AC-I3 -- a single structured WARNING-level log
entry is emitted when a call is read-capped (repository path, pattern,
bytes read at cutoff), so this failure mode is never silent at ~900-repo
fleet scale.
"""

from __future__ import annotations

import shutil
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService

_REGEX_SEARCH_LOGGER_NAME = "code_indexer.global_repos.regex_search"
# Line count far exceeding the test byte ceiling below, so the read is
# genuinely capped (not a coincidence of exactly matching the ceiling).
_SYNTHETIC_LINE_COUNT = 5000
_TEST_BYTE_CEILING = 4096


def _write_synthetic_ripgrep_output(path: str, num_lines: int) -> None:
    import json

    lines = []
    for i in range(num_lines):
        event = {
            "type": "match",
            "data": {
                "path": {"text": f"file{i}.py"},
                "line_number": i + 1,
                "lines": {"text": f"def func_{i}_padding_xxxxxxxxxxxxxxxxxxxx():\n"},
                "submatches": [{"start": 0, "end": 3}],
            },
        }
        lines.append(json.dumps(event))
    content = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


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


class TestReadCappedWarningLog:
    """AC-I3: exactly one structured WARNING log entry when read-capped,
    carrying repository path, pattern, and the EXACT bytes read at cutoff
    as structured LogRecord attributes (not merely rendered message text)."""

    @pytest.mark.asyncio
    async def test_warning_logged_when_read_capped(self, tmp_path, caplog):
        import code_indexer.global_repos.regex_search as regex_search_module
        import logging

        source_path = tmp_path / "source.jsonl"
        _write_synthetic_ripgrep_output(str(source_path), _SYNTHETIC_LINE_COUNT)

        with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/rg"
            service = RegexSearchService(tmp_path)

        with patch.object(regex_search_module, "_MAX_READ_BYTES", _TEST_BYTE_CEILING):
            mock_executor = _mock_success_executor_copying_from(str(source_path))
            with patch(
                "code_indexer.global_repos.regex_search.SubprocessExecutor",
                return_value=mock_executor,
            ):
                with caplog.at_level(logging.WARNING, logger=_REGEX_SEARCH_LOGGER_NAME):
                    result = await service.search(
                        pattern="func_padding",
                        max_results=1_000_000,
                    )

        assert result.read_capped is True

        # ALL WARNING records from this logger during the call -- not a
        # filtered subset -- must be exactly one, proving no duplicate or
        # unrelated WARNING noise accompanies the read-capped signal.
        warning_records = [
            r
            for r in caplog.records
            if r.name == _REGEX_SEARCH_LOGGER_NAME and r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1, (
            f"expected exactly one WARNING log record, got "
            f"{len(warning_records)}: {[r.message for r in warning_records]}"
        )

        record = warning_records[0]
        # Structured fields (via logging `extra=`), asserted as exact
        # attribute values -- not inferred from rendered message text.
        assert record.repo_path == str(tmp_path)
        assert record.pattern == "func_padding"
        # The reader clamps every read to never exceed the ceiling, and
        # with a file this much larger than the ceiling, the final chunk
        # read brings bytes_read to EXACTLY the ceiling -- an exact,
        # deterministic value, not an inferred bound.
        assert record.bytes_read_at_cutoff == _TEST_BYTE_CEILING
