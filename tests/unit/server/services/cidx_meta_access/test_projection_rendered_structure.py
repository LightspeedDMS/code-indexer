"""Projection follows the structure the project's Markdown renderers see.

Each case is checked twice: the pinned projected lines, and the display
rendering through Python-Markdown (domain view) and markdown-it (wiki),
whose table cells must never carry a hidden name or a placeholder.
Real fixture files and real renderers; neutral names only.
"""

from __future__ import annotations

import html as html_module
import logging
import re
from pathlib import Path
from typing import List, Optional

import markdown  # type: ignore[import-untyped]
import pytest

from code_indexer.server.services.cidx_meta_access import (
    AliasUniverse,
    load_depmap_model,
    project_lines,
    render_for_display,
)
from tests.unit.server.services.cidx_meta_access.test_depmap_table_semantics import (
    HIDDEN_A,
    HIDDEN_B,
    VISIBLE_A,
    VISIBLE_B,
    StaticRegistry,
    build_real_format_map,
    project,
    restricted,
)

ROLES = ["| Repo | Language | Role |", "|---|---|---|"]
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)


def _rendered_cells(projected: List[Optional[str]]) -> List[str]:
    shown = render_for_display(projected)
    html = [markdown.markdown(shown, extensions=["tables", "fenced_code"])]
    markdown_it = pytest.importorskip("markdown_it")
    wiki = markdown_it.MarkdownIt("commonmark", {"html": True})
    html.append(wiki.enable(["table", "strikethrough"]).render(shown))
    return [cell for page in html for cell in _CELL.findall(page)]


def _assert_cells_clean(projected: List[Optional[str]]) -> None:
    for cell in _rendered_cells(projected):
        assert HIDDEN_A not in cell and HIDDEN_B not in cell, cell
        assert "[restricted" not in cell, cell


GFM_CASES = {
    "pipe_less_recognised_table": (
        [
            "Repo | Language | Role",
            "--- | --- | ---",
            f"{VISIBLE_A} | Python | api",
            f"{HIDDEN_A} | Go | ledger",
        ],
        ["Repo | Language | Role", "--- | --- | ---", None, None],
    ),
    "row_without_trailing_pipe": (
        ROLES + [f"| {VISIBLE_A} | Python | api |", f"| {HIDDEN_A} | Go | ledger"],
        ROLES + [f"| {VISIBLE_A} | Python | api |", None],
    ),
    "lazy_continuation_lines": (
        ROLES
        + [f"| {VISIBLE_A} | Python | api |", HIDDEN_A, f"{HIDDEN_B} owns it"]
        + ["", f"{VISIBLE_A} builds it."],
        ROLES
        + [f"| {VISIBLE_A} | Python | api |", None, None]
        + ["", f"{VISIBLE_A} builds it."],
    ),
    "unrecognised_pipe_less_table": (
        ["Owner | Secret", "--- | ---", f"{HIDDEN_A} | keys", "", "after the table"],
        [None, None, None, "", "after the table"],
    ),
    "code_fence_ends_a_table": (
        ["```markdown"] + ROLES + [f"| {VISIBLE_A} | Python | api |", "```", "After."],
        ["```markdown"] + ROLES + [f"| {VISIBLE_A} | Python | api |", "```", "After."],
    ),
    "unbalanced_backtick_spanning_cells": (
        [
            "| Repository | Language | Role | Notes |",
            "|---|---|---|---|",
            f"| `{VISIBLE_A} | {HIDDEN_A}` | Go | x |",
        ],
        ["| Repository | Language | Role | Notes |", "|---|---|---|---|", None],
    ),
}


@pytest.mark.parametrize("case", sorted(GFM_CASES))
def test_projection_follows_gfm_table_structure(tmp_path: Path, case: str) -> None:
    lines, expected = GFM_CASES[case]
    dep_map = build_real_format_map(tmp_path)
    projected = project(dep_map, "payments.md", lines)
    assert projected == expected
    _assert_cells_clean(projected)


def test_html_table_markup_is_removed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dep_map = build_real_format_map(tmp_path)
    lines = [
        f"<table><tr><td>{HIDDEN_A}</td></tr></table>",
        "<TABLE>",
        "  <THead><Th>Owner</Th></THead>",
        f"<tbody><TR><td>{VISIBLE_A}</td>",
        "Ordinary <b>bold</b> prose.",
    ]
    with caplog.at_level(logging.WARNING):
        projected = project(dep_map, "payments.md", lines)
    assert projected == [None, None, None, None, "Ordinary <b>bold</b> prose."]
    assert any("payments.md" in r.getMessage() for r in caplog.records)
    _assert_cells_clean(projected)


UNSUPPORTED_FRONTMATTER = {
    "anchor_and_alias": [
        "domain: payments",
        f"all: &x [{VISIBLE_A}, {HIDDEN_A}]",
        "participating_repos: *x",
    ],
    "multiline_flow_list": [
        "domain: payments",
        f"participating_repos: [{VISIBLE_A},",
        f"  {HIDDEN_A}]",
    ],
}


@pytest.mark.parametrize("case", sorted(UNSUPPORTED_FRONTMATTER))
def test_frontmatter_with_anchors_or_multiline_flow_is_removed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, case: str
) -> None:
    dep_map = build_real_format_map(tmp_path)
    block = ["---"] + UNSUPPORTED_FRONTMATTER[case] + ["---"]
    with caplog.at_level(logging.WARNING):
        projected = project(dep_map, "payments.md", block + ["", "Body text."])
    assert projected == [None] * len(block) + ["", "Body text."]
    assert any("payments.md" in r.getMessage() for r in caplog.records)


def test_single_repo_name_rejects_unbalanced_backticks_and_pipes() -> None:
    from code_indexer.server.services.cidx_meta_access.depmap_model import (
        single_repo_name,
    )

    assert single_repo_name(f"`{VISIBLE_A}") is None
    assert single_repo_name(f"`{VISIBLE_A} | {HIDDEN_A}`") is None
    assert single_repo_name(f"{VISIBLE_A}|{HIDDEN_A}") is None
    assert single_repo_name(f"`{VISIBLE_A}`") == VISIBLE_A


_TAG = re.compile(r"<!--.*?-->|<[^>]*>", re.S)


def _rendered_text(projected: List[Optional[str]]) -> str:
    """Visible text of the display rendering through both renderers."""
    shown = render_for_display(projected)
    pages = [markdown.markdown(shown, extensions=["tables", "fenced_code"])]
    markdown_it = pytest.importorskip("markdown_it")
    wiki = markdown_it.MarkdownIt("commonmark", {"html": True})
    pages.append(wiki.enable(["table", "strikethrough"]).render(shown))
    return "\n".join(html_module.unescape(_TAG.sub("", page)) for page in pages)


SPLIT_PREFIX, SPLIT_SUFFIX = HIDDEN_A.split("-", 1)


@pytest.mark.parametrize(
    "split",
    [
        f"**{SPLIT_PREFIX}**-{SPLIT_SUFFIX}",
        f"*{SPLIT_PREFIX}*-{SPLIT_SUFFIX}",
        f"`{SPLIT_PREFIX}`-{SPLIT_SUFFIX}",
        f"[{SPLIT_PREFIX}](https://example.com)-{SPLIT_SUFFIX}",
    ],
)
def test_inline_markup_split_alias_removes_the_line(tmp_path: Path, split: str) -> None:
    dep_map = build_real_format_map(tmp_path)
    lines = [f"Settlement runs in {split} nightly.", f"See {HIDDEN_A} too."]
    projected = project(dep_map, "payments.md", lines)
    assert projected == [None, "See [restricted repository] too."]
    assert HIDDEN_A not in _rendered_text(projected)


SHORT_HIDDEN = "qzx"


@pytest.mark.parametrize("alias", [SHORT_HIDDEN, HIDDEN_A])
def test_blockquoted_table_lines_are_removed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, alias: str
) -> None:
    dep_map = build_real_format_map(tmp_path)
    names = [VISIBLE_A, VISIBLE_B, HIDDEN_A, HIDDEN_B, SHORT_HIDDEN]
    lines = [
        "> | Repo | Language | Role |",
        "> |---|---|---|",
        f"> | {alias} | Go | secret |",
        "> plain quote",
        "> x | y",
        "",
        "After.",
    ]
    with caplog.at_level(logging.WARNING):
        projected = project_lines(
            lines,
            "dependency-map/payments.md",
            restricted(),
            load_depmap_model(dep_map),
            AliasUniverse(StaticRegistry(names)).snapshot(),
        )
    assert projected == [None, None, None, "> plain quote", None, "", "After."]
    assert any("payments.md" in r.getMessage() for r in caplog.records)
    shown = _rendered_text(projected)
    assert alias not in shown and "secret" not in shown
