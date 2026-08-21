"""Unit test for Issue #1601 Priority 9 -- the read-capped WARNING log must
thread the repository ALIAS through when the caller has one available, not
just the (possibly versioned-snapshot, not-necessarily-the-alias) repo
path.

Two tests:
1. The MCP handler call site (`_execute_regex_search_impl`) already has
   `repository_alias` in scope at the exact point it constructs
   `RegexSearchService` -- proves that value is actually passed through as
   the new `alias=` constructor argument.
2. `RegexSearchService` itself, given an `alias`, surfaces it as a
   distinct structured ``alias`` field on the read-capped WARNING log
   record (alongside the existing ``repo_path`` field, kept for backward
   compatibility).
"""

from __future__ import annotations

import logging
import shutil
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService
from code_indexer.server.auth.user_manager import User, UserRole

_REGEX_SEARCH_LOGGER_NAME = "code_indexer.global_repos.regex_search"
_TEST_ALIAS = "my-test-repo-global"
_TEST_BYTE_CEILING = 4096
_SYNTHETIC_LINE_COUNT = 5000


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


class TestReadCappedLogCarriesAlias:
    """Priority 9: the read-capped WARNING log threads the repository
    alias through as a distinct structured field when available."""

    @pytest.mark.asyncio
    async def test_warning_log_includes_alias_when_provided(self, tmp_path, caplog):
        import code_indexer.global_repos.regex_search as regex_search_module

        source_path = tmp_path / "source.jsonl"
        _write_synthetic_ripgrep_output(str(source_path), _SYNTHETIC_LINE_COUNT)

        with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/rg"
            service = RegexSearchService(tmp_path, alias=_TEST_ALIAS)

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

        warning_records = [
            r
            for r in caplog.records
            if r.name == _REGEX_SEARCH_LOGGER_NAME and r.levelno == logging.WARNING
        ]
        assert len(warning_records) == 1
        record = warning_records[0]

        # Backward compatible: repo_path is still present.
        assert record.repo_path == str(tmp_path)
        # New: the repository alias is threaded through as its own field.
        assert record.alias == _TEST_ALIAS


class TestMcpHandlerThreadsAliasIntoRegexSearchService:
    """Priority 9: the MCP handler call site must actually pass its
    in-scope repository_alias through to RegexSearchService, not just
    have it available and unused."""

    @pytest.mark.asyncio
    async def test_execute_regex_search_impl_passes_alias_to_service(self, tmp_path):
        from code_indexer.server.mcp.handlers.search import (
            _execute_regex_search_impl,
        )

        captured_kwargs: dict = {}

        def _fake_regex_search_service(repo_path, **kwargs):
            captured_kwargs.update(kwargs)
            mock_service = MagicMock()
            mock_result = MagicMock()
            mock_result.matches = []
            mock_result.total_matches = 0
            mock_result.truncated = False
            mock_result.read_capped = False
            mock_result.search_engine = "ripgrep"
            mock_result.search_time_ms = 1.0
            mock_service.search = AsyncMock(return_value=mock_result)
            return mock_service

        mock_config = MagicMock()
        mock_config.search_limits_config.timeout_seconds = 30
        mock_config.background_jobs_config.subprocess_max_workers = 2

        user = Mock(spec=User)
        user.username = "testuser"
        user.role = UserRole.NORMAL_USER
        user.has_permission = Mock(return_value=True)

        with (
            patch(
                "code_indexer.server.mcp.handlers.search.get_config_service"
            ) as mock_get_config_service,
            patch(
                "code_indexer.global_repos.regex_search.RegexSearchService",
                side_effect=_fake_regex_search_service,
            ),
        ):
            mock_get_config_service.return_value.get_config.return_value = mock_config
            await _execute_regex_search_impl(
                {"pattern": "func"}, tmp_path, _TEST_ALIAS, user
            )

        assert captured_kwargs.get("alias") == _TEST_ALIAS, (
            f"expected RegexSearchService to be constructed with "
            f"alias={_TEST_ALIAS!r} (the handler's own in-scope "
            f"repository_alias), got kwargs={captured_kwargs}"
        )
