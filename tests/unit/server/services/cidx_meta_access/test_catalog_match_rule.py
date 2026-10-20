"""Catalog matches are judged without executing the caller's pattern.

A match (ripgrep or full-text regex) on a line the caller sees verbatim
keeps the engine's own match; a match on a line projection changed or
removed is dropped. The caller's pattern never reaches Python ``re``.
Real fixture, no mocks; ``re`` is only observed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List

import pytest

from code_indexer.server.services.cidx_meta_access.hit_projection import (
    TextRematch,
    open_catalog_scope,
    project_meta_hits,
    project_regex_matches,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    GAMMA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
    read_lines,
)

pytestmark = pytest.mark.timeout(60)
PATHOLOGICAL = "(a+)+$"
LONG_LINE = "a" * 30 + "!"
TALKS_LINE = 3  # names the hidden repositories: projection changes it
APPLIES_LINE = 6  # "alphabet ordering applies.": shown verbatim
LONG_LINE_NUMBER = 7


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> CidxMetaFixture:
    built = build_cidx_meta_fixture(tmp_path_factory.mktemp("match-rule"))
    doc = built.root / f"{ALPHA}.md"
    doc.write_text(doc.read_text(encoding="utf-8") + LONG_LINE + "\n")
    return built


def _scope(fx: CidxMetaFixture) -> Any:
    return open_catalog_scope(RESTRICTED_USER, fx.access_service, fx.root, fx.registry)


def _lines(fx: CidxMetaFixture) -> List[str]:
    return read_lines(fx.root / f"{ALPHA}.md")


@pytest.fixture
def caller_pattern_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[List[str]]:
    """Fails any ``re`` call that is handed one of the guarded patterns."""
    guarded: List[str] = []

    def guard(name: str) -> None:
        real: Callable[..., Any] = getattr(re, name)

        def spy(pattern: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(pattern, str) and pattern in guarded:
                raise AssertionError(f"caller pattern executed via re.{name}")
            return real(pattern, *args, **kwargs)

        monkeypatch.setattr(re, name, spy)

    for name in ("compile", "search", "match", "fullmatch", "finditer", "findall"):
        guard(name)
    yield guarded


def _match(fx: CidxMetaFixture, line: int, count: int = 1, ctx: int = 0) -> Dict:
    lines = _lines(fx)
    return {
        "file_path": f"{ALPHA}.md",
        "line_number": line,
        "column": 4,
        "line_content": "\n".join(lines[line - 1 : line - 1 + count]),
        "context_before": lines[line - 1 - ctx : line - 1],
        "context_after": lines[line - 1 + count : line - 1 + count + ctx],
    }


def _regex(fx: CidxMetaFixture, matches: List[Dict], pattern: str) -> List[Dict]:
    """*matches* are what ripgrep returned for *pattern*; projection never
    receives the pattern itself."""
    assert pattern
    return project_regex_matches(matches, _scope(fx))


def test_ripgrep_match_never_runs_the_callers_pattern(
    fx: CidxMetaFixture, caller_pattern_guard: List[str]
) -> None:
    caller_pattern_guard.append(PATHOLOGICAL)
    kept = _regex(fx, [_match(fx, LONG_LINE_NUMBER)], PATHOLOGICAL)
    assert [m["line_content"] for m in kept] == [LONG_LINE]


def test_fts_regex_hit_never_runs_the_callers_pattern(
    fx: CidxMetaFixture, caller_pattern_guard: List[str]
) -> None:
    caller_pattern_guard.append(PATHOLOGICAL)
    hit = {
        "file_path": f"{ALPHA}.md",
        "line_number": LONG_LINE_NUMBER,
        "code_snippet": LONG_LINE,
        "match_text": "a" * 30,
        "repository_alias": "cidx-meta-global",
    }
    kept = project_meta_hits([hit], _scope(fx), TextRematch(PATHOLOGICAL, regex=True))
    assert [h["match_text"] for h in kept] == ["a" * 30]


def test_ripgrep_match_on_an_unchanged_line_keeps_the_engine_match(
    fx: CidxMetaFixture,
) -> None:
    (kept,) = _regex(fx, [_match(fx, APPLIES_LINE, ctx=3)], "ordering")
    assert kept["line_content"] == "alphabet ordering applies."
    assert kept["column"] == 4  # the engine's column, not recomputed
    assert len(kept["context_before"]) == 3
    assert "[restricted repository]" in kept["context_before"][0]
    dumped = repr(kept).lower()
    assert CANARY not in dumped and GAMMA not in dumped


def test_ripgrep_match_on_a_changed_line_is_dropped(fx: CidxMetaFixture) -> None:
    assert _regex(fx, [_match(fx, TALKS_LINE)], "talks") == []
    assert _regex(fx, [_match(fx, TALKS_LINE, count=2)], "talks(.|\\n)*It") == []


def test_fts_regex_hit_judged_by_the_line_holding_the_engine_match(
    fx: CidxMetaFixture,
) -> None:
    snippet = "\n".join(_lines(fx)[TALKS_LINE - 1 : APPLIES_LINE])

    def hit(match_text: str) -> Dict[str, Any]:
        return {
            "file_path": f"{ALPHA}.md",
            "line_number": TALKS_LINE,
            "code_snippet": snippet,
            "match_text": match_text,
            "repository_alias": "cidx-meta-global",
        }

    rematch = TextRematch("alpha.*", regex=True)
    kept = project_meta_hits([hit("alphabet ordering")], _scope(fx), rematch)
    assert [h["match_text"] for h in kept] == ["alphabet ordering"]
    assert CANARY not in kept[0]["code_snippet"]
    assert project_meta_hits([hit("alpha talks")], _scope(fx), rematch) == []
    no_engine_match = dict(hit("x"), match_text=None)
    assert project_meta_hits([no_engine_match], _scope(fx), rematch) == []


def test_fts_literal_terms_still_rematch_on_projected_text(
    fx: CidxMetaFixture,
) -> None:
    line = _lines(fx)[TALKS_LINE - 1]
    row = {"path": f"{ALPHA}.md", "line_start": TALKS_LINE, "snippet": line}
    assert project_meta_hits([row], _scope(fx), TextRematch(CANARY)) == []
    assert project_meta_hits([row], _scope(fx), TextRematch("http"))


def test_fixture_line_numbers(fx: CidxMetaFixture) -> None:
    lines = _lines(fx)
    assert CANARY in lines[TALKS_LINE - 1]
    assert lines[APPLIES_LINE - 1] == "alphabet ordering applies."
    assert lines[LONG_LINE_NUMBER - 1] == LONG_LINE
    assert isinstance(fx.root, Path)
