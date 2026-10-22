"""Visible catalog hits at the end of a file survive projection whatever
line endings the snippet carries (real fixture, no mocks).

Projected text is always rebuilt from the document's lines joined with
"\\n" (``LineView.text``), so a snippet's own line endings, and a trailing
line break, never reach the caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.server.services.cidx_meta_access.hit_projection import (
    CatalogScope,
    TextRematch,
    open_catalog_scope,
    project_meta_hits,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

LAST_LINES = ["The zed adapter is optional.", "alphabet ordering applies."]
FIRST_LAST = 5
HIT_SCORE = 0.9


def _fixture(tmp: Path, newline: str) -> CidxMetaFixture:
    """The shared fixture with its description document rewritten to use
    *newline* (the builder writes LF, checked here)."""
    fx = build_cidx_meta_fixture(tmp)
    doc = fx.root / f"{ALPHA}.md"
    text = doc.read_bytes().decode("utf-8")
    assert "\r" not in text and text.endswith("\n".join(LAST_LINES) + "\n")
    doc.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return fx


def _hit(snippet: str) -> Dict[str, Any]:
    return {
        "file_path": f"{ALPHA}.md",
        "line_number": FIRST_LAST,
        "code_snippet": snippet,
        "similarity_score": HIT_SCORE,
        "repository_alias": "cidx-meta-global",
    }


def _scope(fx: CidxMetaFixture) -> CatalogScope:
    scope = open_catalog_scope(RESTRICTED_USER, fx.access_service, fx.root, fx.registry)
    assert scope is not None
    return scope


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_snippet_ending_with_a_newline_keeps_the_visible_hit(
    tmp_path: Path, newline: str
) -> None:
    fx = _fixture(tmp_path, newline)
    snippet = newline.join(LAST_LINES) + newline
    kept = project_meta_hits([_hit(snippet)], _scope(fx))
    assert [row["code_snippet"] for row in kept] == ["\n".join(LAST_LINES)]


def test_chunk_whose_last_line_is_empty_keeps_it(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, "\n")
    hit = dict(_hit(f"# {ALPHA}\n"), line_number=1)
    kept = project_meta_hits([hit], _scope(fx))
    assert [row["code_snippet"] for row in kept] == [f"# {ALPHA}\n"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_fulltext_hit_on_the_last_line_is_kept(tmp_path: Path, newline: str) -> None:
    fx = _fixture(tmp_path, newline)
    hit = dict(_hit(newline.join(LAST_LINES) + newline), match_text="ordering")
    kept = project_meta_hits([hit], _scope(fx), TextRematch("ordering"))
    assert [(row["line_number"], row["match_text"]) for row in kept] == [
        (FIRST_LAST + 1, "ordering")
    ]
