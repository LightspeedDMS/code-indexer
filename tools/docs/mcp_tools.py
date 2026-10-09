"""Generate ``docs/reference/mcp-tools/`` from the MCP tool docs frontmatter.

Output: ``README.md`` (categories with tool counts, one line per tool) and one
page per category. Tool docs are read with the server's own
``ToolDocLoader``; a doc is a tool when it has an ``inputSchema`` (the same
rule ``build_tool_registry`` applies to build ``TOOL_REGISTRY``).

Usage (from the repository root)::

    PYTHONPATH=src python3 -m tools.docs.mcp_tools [--check]
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence

from tools.docs._runner import REPO_ROOT, generated_header, md_cell, run

if TYPE_CHECKING:  # server modules load inside render(), after the tree guard
    from code_indexer.server.mcp.tool_doc_loader import ToolDoc, ToolDocLoader

COMMAND = "python3 -m tools.docs.mcp_tools"
TARGET_DIR = REPO_ROOT / "docs" / "reference" / "mcp-tools"

# Parameter descriptions keep their first sentence, cut to this many chars.
MAX_PARAM_DESCRIPTION = 200

# A sentence ends at . ! ? followed by whitespace and an uppercase letter (or
# the end of the text), never right after a common abbreviation.
_FIRST_SENTENCE = re.compile(
    r"(.+?(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)[.!?])(?=\s+[A-Z]|$)"
)
_WHITESPACE = re.compile(r"\s+")


def default_docs_dir() -> Path:
    """The tool_docs directory beside the server's ToolDocLoader module."""
    from code_indexer.server.mcp import tool_doc_loader

    return Path(tool_doc_loader.__file__).resolve().parent / "tool_docs"


def _loader(docs_dir: Optional[Path]) -> ToolDocLoader:
    from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

    loader = ToolDocLoader(default_docs_dir() if docs_dir is None else docs_dir)
    loader.load_all_docs()
    return loader


def _tools_of(loader: ToolDocLoader) -> Dict[str, ToolDoc]:
    docs = loader.get_all_docs()
    return {name: doc for name, doc in docs.items() if doc.inputSchema is not None}


def load_tools(docs_dir: Optional[Path] = None) -> Dict[str, ToolDoc]:
    """Tool docs that define an inputSchema, keyed by tool name."""
    return _tools_of(_loader(docs_dir))


def short_description(text: str) -> str:
    """First sentence of ``text``, whitespace collapsed, at most the cap."""
    collapsed = _WHITESPACE.sub(" ", text).strip()
    match = _FIRST_SENTENCE.match(collapsed)
    sentence = match.group(1) if match else collapsed
    if len(sentence) <= MAX_PARAM_DESCRIPTION:
        return sentence
    return sentence[:MAX_PARAM_DESCRIPTION].rsplit(" ", 1)[0] + "..."


def _schema_type(schema: Mapping[str, Any]) -> str:
    for key in ("oneOf", "anyOf"):
        if key in schema:
            return " or ".join(_schema_type(alt) for alt in schema[key])
    declared = schema.get("type")
    if isinstance(declared, list):
        base = " or ".join(str(t) for t in declared)
    elif declared == "array" and isinstance(schema.get("items"), dict):
        base = f"array of {_schema_type(schema['items'])}"
    elif declared is None:
        base = "any"
    else:
        base = str(declared)
    if "enum" in schema:
        values = ", ".join(f"`{value}`" for value in schema["enum"])
        base = f"{base} (one of: {values})"
    return base


def _parameter_lines(schema: Mapping[str, Any]) -> List[str]:
    properties = schema.get("properties") or {}
    if not properties:
        return ["No parameters."]
    required = set(schema.get("required") or [])
    lines = [
        "| Parameter | Type | Required | Description |",
        "|-----------|------|----------|-------------|",
    ]
    for name, prop in properties.items():
        lines.append(
            f"| `{name}` | {md_cell(_schema_type(prop))} "
            f"| {'yes' if name in required else 'no'} "
            f"| {md_cell(short_description(str(prop.get('description', ''))))} |"
        )
    return lines


def _tool_lines(doc: ToolDoc, docs_dir: Path, target_dir: Path) -> List[str]:
    if doc.source_path is None:
        raise ValueError(f"Tool doc {doc.name!r} has no source path")
    shown = doc.source_path.relative_to(docs_dir).as_posix()
    link = Path(os.path.relpath(doc.source_path, target_dir)).as_posix()
    lines = ["", f"## `{doc.name}`", "", md_cell(doc.tl_dr), ""]
    lines.append(f"- Permission: `{doc.required_permission}`")
    if doc.requires_config:
        lines.append(f"- Requires config: `{doc.requires_config}`")
    lines += [f"- Source: [{shown}]({link})", ""]
    assert doc.inputSchema is not None  # _tools_of() keeps schema docs only
    return lines + _parameter_lines(doc.inputSchema)


def _index_page(
    by_category: Mapping[str, List[ToolDoc]], descriptions: Mapping[str, str]
) -> str:
    total = sum(len(docs) for docs in by_category.values())
    lines = [
        generated_header(COMMAND),
        "",
        "# MCP Tool Catalog",
        "",
        f"The server exposes {total} MCP tools in {len(by_category)} "
        "categories. This catalog is generated from the YAML frontmatter of the "
        "tool docs under `src/code_indexer/server/mcp/tool_docs/`, the files the "
        "server loads into `TOOL_REGISTRY`. Each category page lists its tools' "
        "permission, parameters and a link to the tool doc, which holds the full "
        "description. Parameter descriptions are the first sentence of the "
        "schema description; Permission is the tool's `required_permission`.",
        "",
        "## Categories",
        "",
        "| Category | Tools | Description |",
        "|----------|-------|-------------|",
    ]
    for category, docs in by_category.items():
        lines.append(
            f"| [{category}]({category}.md) | {len(docs)} "
            f"| {md_cell(descriptions.get(category, ''))} |"
        )
    for category, docs in by_category.items():
        lines += ["", f"## {category}", ""]
        for doc in docs:
            lines.append(
                f"- [`{doc.name}`]({category}.md#{doc.name}) - {md_cell(doc.tl_dr)}"
            )
    return "\n".join(lines) + "\n"


def _category_page(
    category: str,
    docs: List[ToolDoc],
    description: str,
    docs_dir: Path,
    target_dir: Path,
) -> str:
    lines = [generated_header(COMMAND), "", f"# {category} tools", ""]
    if description:
        lines += [md_cell(description), ""]
    lines.append(f"{len(docs)} tools. All categories: [MCP Tool Catalog](README.md).")
    for doc in docs:
        lines += _tool_lines(doc, docs_dir, target_dir)
    return "\n".join(lines) + "\n"


def render(
    docs_dir: Optional[Path] = None, target_dir: Path = TARGET_DIR
) -> Dict[str, str]:
    """Return ``{page name: Markdown}`` for the MCP tool catalog."""
    resolved_dir = (default_docs_dir() if docs_dir is None else docs_dir).resolve()
    loader = _loader(resolved_dir)
    tools = _tools_of(loader)
    if not tools:
        raise ValueError(f"No MCP tool docs found under {resolved_dir}")
    descriptions = {
        entry["name"]: entry["description"] for entry in loader.get_category_overview()
    }
    grouped: Dict[str, List[ToolDoc]] = {}
    for name in sorted(tools):
        grouped.setdefault(tools[name].category, []).append(tools[name])
    by_category = {category: grouped[category] for category in sorted(grouped)}

    pages = {"README.md": _index_page(by_category, descriptions)}
    for category, docs in by_category.items():
        pages[f"{category}.md"] = _category_page(
            category, docs, descriptions.get(category, ""), resolved_dir, target_dir
        )
    return pages


def main(argv: Optional[Sequence[str]] = None, target_dir: Path = TARGET_DIR) -> int:
    return run(
        argv,
        target_dir=target_dir,
        render=lambda: render(target_dir=target_dir),
        command=COMMAND,
    )


if __name__ == "__main__":
    raise SystemExit(main())
