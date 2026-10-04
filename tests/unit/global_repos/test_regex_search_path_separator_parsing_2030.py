"""Bug #2030: grep output parsing must not depend on the path's own characters.

grep reports ``path:LINE:content`` for match lines and ``path-LINE-content``
for context lines. A regex that splits at the first ``:<digits>:`` /
``-<digits>-`` mis-attributes any record whose path (directory OR file name)
contains such a segment -- and can even reclassify a context line whose
CONTENT contains ``:<digits>:`` as a bogus match.

These tests drive the REAL service (real grep / real ripgrep subprocesses)
against a repository whose root carries both segment shapes and whose files
cover dash-only, colon-only and mixed directory/file names (plus a plain
control path), on every engine code path:

- grep, recursive fast path (no glob filters)
- grep, glob-filtered batch path (``include_patterns``)
- ripgrep (``--json``), with and without glob filters
- multiline (grep engine -> Python fallback; ripgrep -> ``--json``)
"""

import shutil
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import patch

import pytest

from code_indexer.global_repos.regex_search import RegexMatch, RegexSearchService

_GREP_BINARY = shutil.which("grep")
_RG_BINARY = shutil.which("rg")

pytestmark = pytest.mark.skipif(
    _GREP_BINARY is None or _RG_BINARY is None,
    reason="Bug #2030 tests drive both real grep and real ripgrep",
)

# Separator-shaped segments under test: "-<digits>-" and ":<digits>:".
# The repository root itself carries both, so absolute paths emitted by
# grep's recursive mode contain them regardless of TMPDIR.
_REPO_DIR_NAME = "repo-4321-r:9:s"
_DASH_FILE = "run-1234-x/f-9-g.py"  # dash-only directory and file name
_COLON_FILE = "v:12:z/h:7:k.py"  # colon-only directory and file name
_MIXED_FILE = "a-5-b:6:c/d:3:e-4-f.py"  # both shapes in directory and file
_PLAIN_FILE = "plain/normal.py"  # control: normal path behaviour unchanged
_ALL_FILES = [_DASH_FILE, _COLON_FILE, _MIXED_FILE, _PLAIN_FILE]

# The match is on line 3; context lines AND the match line carry
# separator-shaped content so the content side of each record is exercised.
_BEFORE_LINES = ["alpha", "beta-3-gamma"]
_MATCH_CONTENT = "NEEDLE x:5:y-6-z"
_AFTER_LINES = ["delta:12:eps", "omega-8-tail"]
_FILE_CONTENT = "\n".join(_BEFORE_LINES + [_MATCH_CONTENT] + _AFTER_LINES) + "\n"
_MATCH_LINE_NUMBER = len(_BEFORE_LINES) + 1
_CONTEXT_LINES = len(_BEFORE_LINES)
_NO_CONTEXT = 0
_ONE_CONTEXT_LINE = 1
_MAX_RESULTS = 100
_NULL = "\x00"
_PATTERN = "NEEDLE"
_GLOB_ALL_PY = ["**/*.py"]


@pytest.fixture
def segment_repo(tmp_path: Path) -> Path:
    repo = tmp_path / _REPO_DIR_NAME
    for rel in _ALL_FILES:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_FILE_CONTENT)
    return repo


def _make_service(repo: Path, engine: str) -> RegexSearchService:
    """Pin the engine; binary locations come from the real PATH lookup."""
    with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
        if engine == "grep":
            mock_which.side_effect = lambda cmd: _GREP_BINARY if cmd == "grep" else None
        else:
            mock_which.return_value = _RG_BINARY
        service = RegexSearchService(repo)
    assert service._search_engine == engine
    return service


def _by_path(matches: List[RegexMatch]) -> Dict[str, RegexMatch]:
    by_path: Dict[str, RegexMatch] = {}
    for m in matches:
        assert m.file_path not in by_path, f"duplicate match for {m.file_path}"
        by_path[m.file_path] = m
    return by_path


def _record(rel: str, line_number: int, sep: str, content: str) -> str:
    """One grep --null record: ``path\\0LINE<sep>content``."""
    return f"{rel}{_NULL}{line_number}{sep}{content}"


_ENGINE_PATHS = [
    pytest.param("grep", None, id="grep-recursive"),
    pytest.param("grep", _GLOB_ALL_PY, id="grep-glob-batch"),
    pytest.param("ripgrep", None, id="ripgrep"),
    pytest.param("ripgrep", _GLOB_ALL_PY, id="ripgrep-glob"),
]


class TestSegmentPathsWithContext:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine,include", _ENGINE_PATHS)
    async def test_match_and_context_lines_attributed_to_real_path(
        self, segment_repo: Path, engine: str, include: Optional[List[str]]
    ) -> None:
        service = _make_service(segment_repo, engine)

        result = await service.search(
            pattern=_PATTERN, include_patterns=include, context_lines=_CONTEXT_LINES
        )

        by_path = _by_path(result.matches)
        assert sorted(by_path) == sorted(_ALL_FILES)
        assert result.total_matches == len(_ALL_FILES)
        for rel in _ALL_FILES:
            m = by_path[rel]
            assert m.line_number == _MATCH_LINE_NUMBER, rel
            assert m.line_content == _MATCH_CONTENT, rel
            assert m.context_before == _BEFORE_LINES, rel
            assert m.context_after == _AFTER_LINES, rel

    @pytest.mark.asyncio
    async def test_grep_and_ripgrep_agree_on_segment_paths(
        self, segment_repo: Path
    ) -> None:
        grep_res = await _make_service(segment_repo, "grep").search(
            pattern=_PATTERN, context_lines=_ONE_CONTEXT_LINE
        )
        rg_res = await _make_service(segment_repo, "ripgrep").search(
            pattern=_PATTERN, context_lines=_ONE_CONTEXT_LINE
        )

        def _key(ms: List[RegexMatch]) -> list:
            return sorted(
                (m.file_path, m.line_number, m.line_content)
                + (tuple(m.context_before), tuple(m.context_after))
                for m in ms
            )

        assert len(grep_res.matches) == len(_ALL_FILES)
        assert _key(grep_res.matches) == _key(rg_res.matches)


class TestSegmentPathsWithoutContext:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine,include", _ENGINE_PATHS)
    async def test_match_lines_attributed_to_real_path(
        self, segment_repo: Path, engine: str, include: Optional[List[str]]
    ) -> None:
        service = _make_service(segment_repo, engine)

        result = await service.search(
            pattern=_PATTERN, include_patterns=include, context_lines=_NO_CONTEXT
        )

        by_path = _by_path(result.matches)
        assert sorted(by_path) == sorted(_ALL_FILES)
        for rel in _ALL_FILES:
            m = by_path[rel]
            assert m.line_number == _MATCH_LINE_NUMBER, rel
            assert m.line_content == _MATCH_CONTENT, rel
            assert m.context_before == [] and m.context_after == [], rel

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["grep", "ripgrep"])
    async def test_multiline_paths_attributed_to_real_path(
        self, segment_repo: Path, engine: str
    ) -> None:
        """Multiline: grep engine uses the Python fallback, ripgrep uses
        ``--json`` -- neither text-parses paths; guard that it stays so."""
        service = _make_service(segment_repo, engine)

        result = await service.search(
            pattern=_PATTERN + r"[^\n]*\ndelta", multiline=True
        )

        by_path = _by_path(result.matches)
        assert sorted(by_path) == sorted(_ALL_FILES)
        for rel in _ALL_FILES:
            assert by_path[rel].line_number == _MATCH_LINE_NUMBER, rel


class TestGrepNullDelimitedCommand:
    def test_build_grep_command_requests_null_delimited_filenames(
        self, segment_repo: Path
    ) -> None:
        service = _make_service(segment_repo, "grep")
        for recursive, files in ((True, None), (False, [_PLAIN_FILE])):
            cmd = service._build_grep_command(
                _PATTERN, True, _CONTEXT_LINES, recursive, files
            )
            assert "--null" in cmd
            assert cmd.index("--null") < cmd.index(_PATTERN)


class TestGrepNullDelimitedRecordParsing:
    """Direct parser tests over grep's ``--null`` record format:
    ``path\\0LINE:content`` (match) / ``path\\0LINE-content`` (context)."""

    def test_parses_records_for_segment_paths(self, segment_repo: Path) -> None:
        service = _make_service(segment_repo, "grep")
        before, after = _BEFORE_LINES[-1], _AFTER_LINES[0]
        lines: List[str] = []
        for rel in (_MIXED_FILE, _COLON_FILE):
            if lines:
                lines.append("--")
            lines += [
                _record(rel, _MATCH_LINE_NUMBER - 1, "-", before),
                _record(rel, _MATCH_LINE_NUMBER, ":", _MATCH_CONTENT),
                _record(rel, _MATCH_LINE_NUMBER + 1, "-", after),
            ]

        matches, total = service._parse_grep_output(
            "\n".join(lines), _MAX_RESULTS, _ONE_CONTEXT_LINE
        )

        assert total == 2
        assert [(m.file_path, m.line_number) for m in matches] == [
            (_MIXED_FILE, _MATCH_LINE_NUMBER),
            (_COLON_FILE, _MATCH_LINE_NUMBER),
        ]
        for m in matches:
            assert m.line_content == _MATCH_CONTENT
            assert m.context_before == [before]
            assert m.context_after == [after]

    def test_line_without_null_delimiter_is_not_a_record(
        self, segment_repo: Path
    ) -> None:
        """grep diagnostics such as ``Binary file X matches`` and legacy
        colon-delimited text carry no NUL: they are not records."""
        service = _make_service(segment_repo, "grep")
        output = "\n".join(
            [
                f"{_PLAIN_FILE}:{_MATCH_LINE_NUMBER}:{_PATTERN} legacy",
                "Binary file plain/blob.bin matches",
                _record(_PLAIN_FILE, _MATCH_LINE_NUMBER, ":", _PATTERN + " real"),
            ]
        )

        matches, total = service._parse_grep_output(output, _MAX_RESULTS, _NO_CONTEXT)

        assert total == 1
        assert [(m.file_path, m.line_content) for m in matches] == [
            (_PLAIN_FILE, _PATTERN + " real")
        ]

    def test_record_with_malformed_line_field_is_ignored(
        self, segment_repo: Path
    ) -> None:
        service = _make_service(segment_repo, "grep")
        output = "\n".join(
            [
                f"{_PLAIN_FILE}{_NULL}x{_MATCH_LINE_NUMBER}:{_PATTERN}",
                f"{_PLAIN_FILE}{_NULL}{_MATCH_LINE_NUMBER}{_PATTERN}",
            ]
        )

        matches, total = service._parse_grep_output(output, _MAX_RESULTS, _NO_CONTEXT)

        assert (matches, total) == ([], 0)
