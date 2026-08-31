"""Unit test for Issue #1601 AC-E -- the CLI `cidx global regex-search
--json` output must surface read_capped, not silently drop it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from code_indexer.cli import cli


def _mock_search_result(read_capped: bool):
    result = MagicMock()
    result.matches = []
    result.total_matches = 0
    result.truncated = False
    result.read_capped = read_capped
    result.search_engine = "ripgrep"
    result.search_time_ms = 12.5
    return result


class TestCliRegexSearchReadCappedSurfacing:
    """AC-E: `cidx global regex-search --json` must expose read_capped."""

    def test_json_output_includes_read_capped_true(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        mock_registry = MagicMock()
        mock_registry.get_global_repo.return_value = {
            "index_path": str(repo_path),
        }
        mock_service = MagicMock()

        async def _fake_search(**kwargs):
            return _mock_search_result(True)

        mock_service.search = _fake_search

        with (
            patch(
                "code_indexer.global_repos.global_registry.GlobalRegistry",
                return_value=mock_registry,
            ),
            patch(
                "code_indexer.global_repos.regex_search.RegexSearchService",
                return_value=mock_service,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "global",
                    "regex-search",
                    "myrepo",
                    "pattern",
                    "--json",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["read_capped"] is True
