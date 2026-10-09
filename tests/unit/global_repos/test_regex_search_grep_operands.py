"""grep fallback passes the regex_search pattern and paths to grep as operands.

The pattern is always handed to grep through ``-e`` and every search path
(the recursive search root or a glob-selected file batch) follows a ``--``
end-of-options marker, mirroring the ripgrep command builder. These tests
run the real grep binary with ripgrep hidden so the grep engine is used.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

import pytest

from code_indexer.global_repos.regex_search import RegexSearchService

_NEEDS_GREP = pytest.mark.skipif(
    shutil.which("grep") is None, reason="grep binary not available"
)

_DASH_PATTERNS = ["-foo", "--version"]


@pytest.fixture(autouse=True)
def _isolate_grep_fallback_warned_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building a grep-engine service sets the process-wide one-time warning
    flag; scope it to each test so later tests see the original value."""
    monkeypatch.setattr(RegexSearchService, "_grep_fallback_warned", False)


def _grep_only_service(repo_path: Path) -> RegexSearchService:
    """Build the real grep fallback while making only ripgrep unavailable."""
    real_which = shutil.which
    with patch("code_indexer.global_repos.regex_search.shutil.which") as which:
        which.side_effect = lambda command: (
            None if command == "rg" else real_which(command)
        )
        service = RegexSearchService(repo_path)
    assert service._search_engine == "grep"
    return service


def _write(repo: Path, relative: str, content: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


async def _search_files(
    repo: Path, pattern: str, include_patterns: Optional[List[str]] = None
) -> List[str]:
    result = await _grep_only_service(repo).search(
        pattern=pattern,
        include_patterns=include_patterns,
        max_results=50,
        timeout_seconds=30,
    )
    return sorted(match.file_path for match in result.matches)


@_NEEDS_GREP
@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", _DASH_PATTERNS)
async def test_recursive_search_matches_pattern_starting_with_dash(
    tmp_path: Path, pattern: str
) -> None:
    _write(tmp_path, "src/hit.py", f"value = 'x {pattern} y'\n")
    _write(tmp_path, "src/miss.py", "value = 'nothing here'\n")

    assert await _search_files(tmp_path, pattern) == ["src/hit.py"]


@_NEEDS_GREP
@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", _DASH_PATTERNS)
async def test_glob_batch_search_matches_pattern_starting_with_dash(
    tmp_path: Path, pattern: str
) -> None:
    _write(tmp_path, "src/hit.py", f"value = 'x {pattern} y'\n")
    _write(tmp_path, "src/miss.py", "value = 'nothing here'\n")

    assert await _search_files(tmp_path, pattern, ["*.py"]) == ["src/hit.py"]


@_NEEDS_GREP
@pytest.mark.asyncio
async def test_glob_batch_search_reads_file_whose_name_starts_with_dash(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "-dash.py", "OperandNeedle = 1\n")
    _write(tmp_path, "plain.py", "OperandNeedle = 2\n")

    assert await _search_files(tmp_path, "OperandNeedle", ["*.py"]) == [
        "-dash.py",
        "plain.py",
    ]


@_NEEDS_GREP
@pytest.mark.asyncio
async def test_recursive_search_reads_file_whose_name_starts_with_dash(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "-dash.py", "OperandNeedle = 1\n")

    assert await _search_files(tmp_path, "OperandNeedle") == ["-dash.py"]


@_NEEDS_GREP
@pytest.mark.asyncio
async def test_recursive_search_still_excludes_internal_directories(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/kept.py", "OperandNeedle\n")
    _write(tmp_path, ".git/config", "OperandNeedle\n")
    _write(tmp_path, ".code-indexer/meta.json", "OperandNeedle\n")

    assert await _search_files(tmp_path, "OperandNeedle") == ["src/kept.py"]


@_NEEDS_GREP
@pytest.mark.parametrize(
    "recursive, operands",
    [(True, ["/repo/root"]), (False, ["-a.py", "b.py"])],
)
def test_built_command_places_pattern_and_operands_after_options(
    tmp_path: Path, recursive: bool, operands: List[str]
) -> None:
    cmd = _grep_only_service(tmp_path)._build_grep_command(
        "-foo", True, 2, recursive, operands
    )

    end_of_options = cmd.index("--")
    assert cmd[cmd.index("-e") + 1] == "-foo"
    assert cmd.index("-e") < end_of_options
    assert cmd[end_of_options + 1 :] == operands
    if recursive:
        exclude_positions = [i for i, arg in enumerate(cmd) if arg == "--exclude-dir"]
        assert [cmd[i + 1] for i in exclude_positions] == [".code-indexer", ".git"]
        assert all(i < end_of_options for i in exclude_positions)
