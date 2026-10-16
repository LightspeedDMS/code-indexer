"""Tests for the MCP tool catalog generator (Story #2082)."""

import os
import re
import textwrap
from pathlib import Path
from typing import Dict

import pytest

from code_indexer.server.mcp.tools import TOOL_REGISTRY
from tools.docs import mcp_tools as gen

_TOOL_HEADING = re.compile(r"^## `([a-z0-9_]+)`$", re.MULTILINE)
_SOURCE_LINK = re.compile(r"^- Source: \[[^\]]+\]\(([^)]+)\)$", re.MULTILINE)
_INDEX_LINE = re.compile(r"^- \[`([a-z0-9_]+)`\]\(([a-z]+)\.md#([a-z0-9_]+)\)", re.M)


def _write_doc(docs_dir: Path, category: str, body: str) -> None:
    folder = docs_dir / category
    folder.mkdir(parents=True, exist_ok=True)
    content = textwrap.dedent(body).lstrip()
    name = re.search(r"^name: (\S+)$", content, re.MULTILINE)
    assert name is not None
    (folder / f"{name.group(1)}.md").write_text(content, encoding="utf-8")


@pytest.fixture
def synthetic_docs(tmp_path: Path) -> Path:
    docs = tmp_path / "src" / "tool_docs"
    _write_doc(
        docs,
        "search",
        """\
        ---
        name: zeta_search
        category: search
        required_permission: query_repos
        tl_dr: Search <things> | fast.
        inputSchema:
          type: object
          properties:
            query_text:
              type: string
              description: The query. Second sentence is dropped.
            mode:
              type: string
              enum: [semantic, fts]
              description: Search mode
            alias:
              oneOf:
                - type: string
                - type: array
                  items:
                    type: string
              description: One alias or several | pipes escaped
            limit:
              type: [integer, "null"]
              description: Max results
          required: [query_text]
        ---
        Body text.
        """,
    )
    _write_doc(
        docs,
        "search",
        """\
        ---
        name: alpha_browse
        category: search
        required_permission: public
        tl_dr: Browse.
        inputSchema:
          type: object
          properties: {}
        ---
        Body.
        """,
    )
    _write_doc(
        docs,
        "tracing",
        """\
        ---
        name: start_trace
        category: tracing
        required_permission: query_repos
        requires_config: langfuse_enabled
        tl_dr: Start a trace.
        inputSchema:
          type: object
          properties:
            topic:
              type: string
              description: x
        ---
        Body.
        """,
    )
    (docs / "search" / "_category.yaml").write_text(
        "name: search\ndescription: Code search tools\n", encoding="utf-8"
    )
    return docs


def _synthetic_pages(docs: Path) -> Dict[str, str]:
    target_dir = docs.parent.parent / "docs" / "reference" / "mcp-tools"
    return gen.render(docs_dir=docs, target_dir=target_dir)


@pytest.fixture(scope="module")
def real_pages() -> Dict[str, str]:
    return gen.render()


def test_real_docs_one_page_per_category(real_pages: Dict[str, str]) -> None:
    categories = {doc.category for doc in gen.load_tools().values()}
    assert set(real_pages) == {"README.md"} | {f"{c}.md" for c in categories}


def test_real_docs_every_registry_tool_listed_once(real_pages: Dict[str, str]) -> None:
    loaded = gen.load_tools()
    names = []
    for page, text in real_pages.items():
        if page == "README.md":
            continue
        for name in _TOOL_HEADING.findall(text):
            assert page == f"{loaded[name].category}.md", name
            names.append(name)
    assert sorted(names) == sorted(TOOL_REGISTRY)
    readme = real_pages["README.md"]
    assert f"exposes {len(TOOL_REGISTRY)} MCP tools" in readme
    indexed = _INDEX_LINE.findall(readme)
    assert sorted(t for t, _, _ in indexed) == sorted(TOOL_REGISTRY)
    for tool, category, anchor in indexed:
        assert category == loaded[tool].category and anchor == tool
    for name, tool in TOOL_REGISTRY.items():
        page = real_pages[f"{loaded[name].category}.md"]
        assert f"- Permission: `{tool['required_permission']}`" in page


def test_real_docs_source_links_resolve(real_pages: Dict[str, str]) -> None:
    links = [
        link for page, text in real_pages.items() for link in _SOURCE_LINK.findall(text)
    ]
    assert len(links) == len(TOOL_REGISTRY)
    for link in links:
        assert not link.startswith("/")
        assert (gen.TARGET_DIR / link).resolve().is_file(), link


def test_every_page_starts_with_generated_header(real_pages: Dict[str, str]) -> None:
    for text in real_pages.values():
        assert text.splitlines()[0] == gen.generated_header(gen.COMMAND)
    assert gen.COMMAND == "python3 -m tools.docs.mcp_tools"


def test_synthetic_index(synthetic_docs: Path) -> None:
    pages = _synthetic_pages(synthetic_docs)
    assert set(pages) == {"README.md", "search.md", "tracing.md"}
    readme = pages["README.md"]
    assert "exposes 3 MCP tools in 2 categories" in readme
    assert "| [search](search.md) | 2 | Code search tools |" in readme
    assert "| [tracing](tracing.md) | 1 |  |" in readme
    assert _INDEX_LINE.findall(readme) == [
        ("alpha_browse", "search", "alpha_browse"),
        ("zeta_search", "search", "zeta_search"),
        ("start_trace", "tracing", "start_trace"),
    ]
    assert "- [`zeta_search`](search.md#zeta_search) - Search &lt;things&gt;" in readme


def test_synthetic_category_page_details(synthetic_docs: Path) -> None:
    pages = _synthetic_pages(synthetic_docs)
    page = pages["search.md"]
    assert _TOOL_HEADING.findall(page) == ["alpha_browse", "zeta_search"]
    assert "Code search tools" in page
    assert "[MCP Tool Catalog](README.md)" in page
    assert "Search &lt;things&gt; \\| fast." in page
    assert "- Permission: `query_repos`" in page
    target_dir = synthetic_docs.parent.parent / "docs" / "reference" / "mcp-tools"
    expected_link = os.path.relpath(
        synthetic_docs / "search" / "zeta_search.md", target_dir
    )
    assert f"- Source: [search/zeta_search.md]({expected_link})" in page
    assert "| `query_text` | string | yes | The query. |" in page
    assert "| `mode` | string (one of: `semantic`, `fts`) | no | Search mode |" in page
    assert (
        "| `alias` | string or array of string | no | "
        "One alias or several \\| pipes escaped |"
    ) in page
    assert "| `limit` | integer or null | no | Max results |" in page
    assert page.index("`query_text`") < page.index("`mode`") < page.index("`limit`")
    assert "- Requires config: `langfuse_enabled`" in pages["tracing.md"]


def test_tool_without_parameters_says_so(synthetic_docs: Path) -> None:
    page = _synthetic_pages(synthetic_docs)["search.md"]
    entry = page[page.index("## `alpha_browse`") : page.index("## `zeta_search`")]
    assert "No parameters." in entry
    assert "| Parameter |" not in entry


def test_doc_without_input_schema_is_not_a_tool(synthetic_docs: Path) -> None:
    _write_doc(
        synthetic_docs,
        "search",
        """\
        ---
        name: just_a_guide
        category: search
        required_permission: public
        tl_dr: A guide.
        ---
        Body.
        """,
    )
    for text in _synthetic_pages(synthetic_docs).values():
        assert "just_a_guide" not in text


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("One. Two.", "One."),
        ("No terminal period", "No terminal period"),
        ("Version 1.5 is used. Next.", "Version 1.5 is used."),
        ("Multi\n  line   text. Rest.", "Multi line text."),
        ("Alias (e.g. 'x-global'). More.", "Alias (e.g. 'x-global')."),
        ("Alias (e.g. Foo or Bar). More.", "Alias (e.g. Foo or Bar)."),
        ("The id, i.e. The key. Next.", "The id, i.e. The key."),
        ("Files, docs, etc. Then more.", "Files, docs, etc. Then more."),
        ("Semantic vs. FTS mode. Rest.", "Semantic vs. FTS mode."),
        ("", ""),
    ],
)
def test_short_description_keeps_first_sentence(raw: str, expected: str) -> None:
    assert gen.short_description(raw) == expected


def test_short_description_truncates_long_text_at_word_boundary() -> None:
    raw = "abcd " * 100  # 500 chars, no sentence end
    result = gen.short_description(raw)
    assert result.endswith("...")
    kept = result[:-3]
    assert len(kept) <= gen.MAX_PARAM_DESCRIPTION
    assert raw.startswith(kept + " ")
    assert kept.endswith("abcd")


def test_render_is_deterministic(real_pages: Dict[str, str]) -> None:
    assert gen.render() == real_pages


def test_check_fails_on_stale_and_passes_after_generation(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "reference" / "mcp-tools"
    target.mkdir(parents=True)
    (target / "README.md").write_text("# stale\n", encoding="utf-8")
    assert gen.main(["--check"], target_dir=target) == 1
    assert gen.main([], target_dir=target) == 0
    assert gen.main(["--check"], target_dir=target) == 0
    for name, text in gen.render(target_dir=target).items():
        assert (target / name).read_text(encoding="utf-8") == text
