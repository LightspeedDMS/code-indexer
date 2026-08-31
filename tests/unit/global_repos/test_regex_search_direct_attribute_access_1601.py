"""Unit test for Issue #1601 Priority 9 -- `_search_ripgrep`/`_search_grep`
must access ``result.output_capped`` directly, not via
``getattr(result, "output_capped", False)``.

The defensive ``getattr`` masks a future rename of the
``output_capped`` field: if it were ever renamed or removed from
``SearchExecutionResult``, ``getattr`` would silently swallow the
resulting ``AttributeError`` and default to False forever, permanently
hiding the read-capped signal rather than failing loudly. This is
demonstrated with a strictly-``spec``'d ``Mock`` (one that raises
``AttributeError`` for any attribute not in its spec, simulating the
field having been renamed away) instead of a bare ``MagicMock`` (which
auto-creates attributes and would mask the discriminator either way).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService

_TEST_MAX_RESULTS = 100
_TEST_TIMEOUT_SECONDS = 30


def _mock_executor_with_result_missing_output_capped():
    """A mocked SubprocessExecutor whose result object is strictly
    spec'd WITHOUT an ``output_capped`` attribute -- simulating a future
    rename. Direct attribute access must raise AttributeError; the old
    getattr-based code would silently default to False instead."""

    async def _side_effect(**kwargs):
        with open(kwargs["output_file_path"], "w"):
            pass  # empty output is fine -- this test only cares about
            #     output_capped attribute access, not parse results.
        result = Mock(spec=["timed_out", "status", "exit_code", "stderr_output"])
        result.timed_out = False
        result.status = "success"
        result.exit_code = 0
        result.stderr_output = None
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
    pytest.param("ripgrep", "_search_ripgrep", id="ripgrep"),
    pytest.param("grep", "_search_grep", id="grep"),
]


class TestRegexSearchDirectAttributeAccess:
    """Priority 9: output_capped must be accessed directly, not via a
    defensive getattr that silently masks a future field rename."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine, search_attr", _ENGINE_CASES)
    async def test_search_fails_loud_when_output_capped_renamed(
        self, tmp_path, engine, search_attr
    ):
        service = _build_service(tmp_path, engine)
        mock_executor = _mock_executor_with_result_missing_output_capped()

        with patch(
            "code_indexer.global_repos.regex_search.SubprocessExecutor",
            return_value=mock_executor,
        ):
            search_method = getattr(service, search_attr)
            with pytest.raises(AttributeError, match="output_capped"):
                await search_method(
                    pattern="func",
                    search_path=tmp_path,
                    include_patterns=None,
                    exclude_patterns=None,
                    case_sensitive=True,
                    context_lines=0,
                    max_results=_TEST_MAX_RESULTS,
                    timeout_seconds=_TEST_TIMEOUT_SECONDS,
                )
