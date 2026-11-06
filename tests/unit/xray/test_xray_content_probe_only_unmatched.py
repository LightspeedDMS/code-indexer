"""X-Ray content Phase 1: probe only the include patterns that matched nothing,
and read the trigram index this pod serves for the repository.

The zero-match probe runs one ripgrep walk per include pattern. A pattern that
selected a file the main search returned is not zero-match, so probing it is a
full-repository walk for an answer already in hand.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from code_indexer.global_repos.regex_search import RegexMatch, RegexSearchResult
from cidx_xray import search_engine as se


def _result(paths: List[str]) -> RegexSearchResult:
    return RegexSearchResult(
        matches=[
            RegexMatch(file_path=p, line_number=1, column=1, line_content="x")
            for p in paths
        ],
        total_matches=len(paths),
        truncated=False,
        search_engine="ripgrep",
        search_time_ms=0.0,
    )


@pytest.mark.parametrize(
    "patterns, matched, expected",
    [
        (["src/**/*.py", "docs/*.md"], ["src/a/b.py"], ["docs/*.md"]),
        (["src/**/*.py", "docs/*.md"], ["src/a/b.py", "docs/x.md"], []),
        (["*.py"], [], ["*.py"]),
        (["lib/*.java"], ["src/a.py"], ["lib/*.java"]),
    ],
)
def test_patterns_without_matches(
    patterns: List[str], matched: List[str], expected: List[str]
) -> None:
    assert se._patterns_without_matches(patterns, matched) == expected


#: Repo-relative paths a main search returned, for the -g semantics table.
_RETURNED = ["src/a/Foo.java", "src/b/c/Bar.py", "README.md", "docs/x/y.md"]

#: (pattern, whether ripgrep's ``-g pattern`` selects one of _RETURNED). The
#: expectations are ripgrep's; ``test_the_table_is_ripgreps`` re-derives
#: them from a real ``rg --files -g`` when the binary is present.
_RG_GLOB_TABLE = [
    ("*.java", True),  # no slash: matches a basename at any depth
    ("**/*.java", True),
    ("src/*.java", False),  # one segment only: Foo.java is two deep
    ("src/**/*.java", True),
    ("src", True),  # a bare directory token selects everything under it
    ("src/", True),
    ("src/b/**", True),  # a directory glob
    ("src/**/c/*.py", True),
    ("docs/**/*.md", True),
    ("{src,docs}/**/*.md", True),  # brace expansion
    ("*.md", True),
    ("nope/**", False),
    ("**/*.rs", False),
]


@pytest.mark.parametrize("pattern, selects", _RG_GLOB_TABLE)
def test_a_pattern_is_probed_only_when_ripgrep_would_select_nothing(
    pattern: str, selects: bool
) -> None:
    remaining = se._patterns_without_matches([pattern], _RETURNED)
    assert remaining == ([] if selects else [pattern])


@pytest.mark.skipif(shutil.which("rg") is None, reason="needs the rg binary")
@pytest.mark.parametrize("pattern, selects", _RG_GLOB_TABLE)
def test_the_table_is_ripgreps(tmp_path: Path, pattern: str, selects: bool) -> None:
    for rel in _RETURNED:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x\n")
    from code_indexer.services.path_pattern_matcher import normalize_glob_pattern

    argv = ["rg", "--files", "--no-ignore", "--hidden"]
    for normalized in normalize_glob_pattern(pattern):
        argv += ["-g", normalized]
    listed = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True)
    assert bool(listed.stdout.strip()) is selects


def test_a_negated_include_never_reaches_the_probe() -> None:
    """``!x`` is ripgrep's exclude syntax; as an include it is refused by the
    front-door validation, and the probe filter refuses it the same way
    rather than silently treating it as matched."""
    from code_indexer.services.path_pattern_matcher import InvalidPatternError

    with pytest.raises(InvalidPatternError):
        se._patterns_without_matches(["!*.md"], _RETURNED)


def test_no_patterns_means_nothing_to_probe() -> None:
    assert se._patterns_without_matches([], _RETURNED) == []


def test_each_pattern_is_decided_on_its_own() -> None:
    assert se._patterns_without_matches(
        ["src/*.java", "*.md", "nope/**"], _RETURNED
    ) == ["src/*.java", "nope/**"]


def test_only_unmatched_patterns_are_probed(tmp_path: Path) -> None:
    engine = se.XRaySearchEngine.__new__(se.XRaySearchEngine)
    engine._last_phase1_positions = {}
    engine._last_phase1_warnings = []
    probed: List[List[str]] = []

    def fake_probe(
        repo_path: Path, include_patterns: List[str], timeout_seconds: int = 120
    ) -> List[Dict[str, Any]]:
        probed.append(list(include_patterns))
        return []

    service = AsyncMock()
    service.search = AsyncMock(return_value=_result(["src/app/A.java"]))
    with (
        patch.object(se, "_phase1_regex_service", return_value=service),
        patch.object(engine, "_probe_zero_match_patterns_content", fake_probe),
    ):
        engine._run_phase1_content(
            tmp_path,
            "class",
            ["src/**/*.java", "test/**/*.java"],
            [],
        )

    assert probed == [["test/**/*.java"]]


def test_phase1_service_reads_the_served_trigram_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.startup import ripgrep_backend_wiring

    base = tmp_path / "base"
    snapshot = tmp_path / ".versioned" / "example-repo" / "v_1"

    class _Delegated:
        is_local = False

        async def supports_pcre2(self) -> bool:
            return False

    monkeypatch.setattr(
        ripgrep_backend_wiring,
        "server_regex_target",
        lambda p: (base, _Delegated()),
    )

    service = se._phase1_regex_service(snapshot)

    assert service.repo_path == base
    assert service._trigram_root == snapshot
