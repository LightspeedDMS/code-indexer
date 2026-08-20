"""Unit test for Issue #1601 Priority 8 -- the CLI `cidx global
regex-search` human-readable (non-JSON) output must disclose when the
scan was read-capped, not just silently print "Found N matches" as if
the count were complete.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from code_indexer.cli import cli


def _mock_search_result(read_capped: bool):
    match = MagicMock()
    match.file_path = "src/foo.py"
    match.line_number = 1
    match.column = 1
    match.line_content = "def foo():"
    match.context_before = []
    match.context_after = []

    result = MagicMock()
    result.matches = [match]
    result.total_matches = 1
    result.truncated = False
    result.read_capped = read_capped
    result.search_engine = "ripgrep"
    result.search_time_ms = 12.5
    return result


def _invoke_regex_search(read_capped: bool, tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    mock_registry = MagicMock()
    mock_registry.get_global_repo.return_value = {"index_path": str(repo_path)}
    mock_service = MagicMock()

    async def _fake_search(**kwargs):
        return _mock_search_result(read_capped)

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
        return runner.invoke(cli, ["global", "regex-search", "myrepo", "pattern"])


class TestCliRegexSearchCappedDisclosure:
    """Priority 8: the human-readable CLI output must disclose a capped
    scan, not present it as a complete count."""

    def test_human_readable_output_discloses_capped_scan(self, tmp_path):
        result = _invoke_regex_search(read_capped=True, tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        assert "capped" in result.output.lower(), (
            f"expected the human-readable output to disclose the scan was "
            f"read-capped, got: {result.output!r}"
        )

    def test_human_readable_output_unaffected_when_not_capped(self, tmp_path):
        result = _invoke_regex_search(read_capped=False, tmp_path=tmp_path)

        assert result.exit_code == 0, result.output
        assert "capped" not in result.output.lower(), (
            f"a non-capped scan must not display any capped disclosure, "
            f"got: {result.output!r}"
        )
        assert "Found 1 matches" in result.output
