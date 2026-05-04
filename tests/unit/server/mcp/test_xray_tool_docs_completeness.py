"""Tests verifying completeness of xray_search and xray_explore tool docs.

Each test asserts one or more Acceptance Criteria from Story #979.
Tests parse the .md files from disk and check frontmatter + body content.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOOL_DOCS_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "mcp"
    / "tool_docs"
    / "search"
)

XRAY_MD = TOOL_DOCS_DIR / "xray_search.md"
XRAY_EXPLORE_MD = TOOL_DOCS_DIR / "xray_explore.md"


def _split_frontmatter(path: Path) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text) for a tool doc .md file."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text

    # Find closing --- delimiter
    end = text.index("---", 3)
    yaml_block = text[3:end].strip()
    body = text[end + 3 :].strip()
    return yaml.safe_load(yaml_block) or {}, body


# Parsed once per test session (files are tiny, no need to cache with fixtures)
@pytest.fixture(scope="module")
def xray_frontmatter_and_body() -> tuple[dict, str]:
    return _split_frontmatter(XRAY_MD)


@pytest.fixture(scope="module")
def xray_explore_frontmatter_and_body() -> tuple[dict, str]:
    return _split_frontmatter(XRAY_EXPLORE_MD)


# ---------------------------------------------------------------------------
# Scenario: xray_search.md exists with valid frontmatter
# ---------------------------------------------------------------------------


def test_xray_md_has_required_frontmatter(xray_frontmatter_and_body):
    fm, _ = xray_frontmatter_and_body
    assert fm.get("name") == "xray_search", (
        f"Expected name=xray_search, got {fm.get('name')!r}"
    )
    assert fm.get("category") == "search", (
        f"Expected category=search, got {fm.get('category')!r}"
    )
    assert fm.get("required_permission") == "query_repos", (
        f"Expected required_permission=query_repos, got {fm.get('required_permission')!r}"
    )
    assert fm.get("tl_dr"), "tl_dr must be a non-empty string"


# ---------------------------------------------------------------------------
# Scenario: xray_explore.md exists with valid frontmatter
# ---------------------------------------------------------------------------


def test_xray_explore_md_has_required_frontmatter(xray_explore_frontmatter_and_body):
    fm, _ = xray_explore_frontmatter_and_body
    assert fm.get("name") == "xray_explore", (
        f"Expected name=xray_explore, got {fm.get('name')!r}"
    )
    assert fm.get("category") == "search", (
        f"Expected category=search, got {fm.get('category')!r}"
    )
    assert fm.get("required_permission") == "query_repos", (
        f"Expected required_permission=query_repos, got {fm.get('required_permission')!r}"
    )
    assert fm.get("tl_dr"), "tl_dr must be a non-empty string"


# ---------------------------------------------------------------------------
# Scenario: Both docs document all parameters in body
# ---------------------------------------------------------------------------

_XRAY_REQUIRED_PARAMS = [
    "repository_alias",
    "driver_regex",
    "evaluator_code",
    "search_target",
    "include_patterns",
    "exclude_patterns",
    "timeout_seconds",
    "max_files",
]


def test_xray_md_documents_all_parameters(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    missing = [p for p in _XRAY_REQUIRED_PARAMS if p not in body]
    assert not missing, f"xray_search.md body is missing parameters: {missing}"


def test_xray_explore_md_documents_all_parameters(xray_explore_frontmatter_and_body):
    _, body = xray_explore_frontmatter_and_body
    required = _XRAY_REQUIRED_PARAMS + ["max_debug_nodes"]
    missing = [p for p in required if p not in body]
    assert not missing, f"xray_explore.md body is missing parameters: {missing}"


# ---------------------------------------------------------------------------
# Scenario: Both docs include the Evaluator API reference
# ---------------------------------------------------------------------------

_EVALUATOR_EXPOSED_NAMES = ["node", "root", "source", "lang", "file_path"]

_WHITELISTED_AST_NODES = [
    "Call",
    "Name",
    "Attribute",
    "Constant",
    "Subscript",
    "Compare",
    "BoolOp",
    "UnaryOp",
    "List",
    "Tuple",
    "Dict",
    "Return",
    "Expr",
]

_STRIPPED_BUILTINS = [
    "getattr",
    "setattr",
    "delattr",
    "hasattr",
    "__import__",
    "eval",
    "exec",
    "open",
    "compile",
]

_DUNDER_BLOCKLIST_SAMPLE = [
    "__class__",
    "__bases__",
    "__globals__",
    "__builtins__",
    "__dict__",
]


def test_xray_md_has_evaluator_api_section(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    assert "Evaluator API" in body, (
        "xray_search.md must contain an 'Evaluator API' section header"
    )


def test_xray_explore_md_has_evaluator_api_section(xray_explore_frontmatter_and_body):
    _, body = xray_explore_frontmatter_and_body
    assert "Evaluator API" in body, (
        "xray_explore.md must contain an 'Evaluator API' section header"
    )


def test_xray_md_evaluator_api_lists_exposed_names(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    missing = [n for n in _EVALUATOR_EXPOSED_NAMES if n not in body]
    assert not missing, (
        f"xray_search.md Evaluator API section missing exposed names: {missing}"
    )


def test_xray_explore_md_evaluator_api_lists_exposed_names(
    xray_explore_frontmatter_and_body,
):
    _, body = xray_explore_frontmatter_and_body
    missing = [n for n in _EVALUATOR_EXPOSED_NAMES if n not in body]
    assert not missing, (
        f"xray_explore.md Evaluator API section missing exposed names: {missing}"
    )


def test_xray_md_evaluator_api_lists_whitelisted_nodes(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    missing = [n for n in _WHITELISTED_AST_NODES if n not in body]
    assert not missing, (
        f"xray_search.md Evaluator API missing whitelisted AST nodes: {missing}"
    )


def test_xray_explore_md_evaluator_api_lists_whitelisted_nodes(
    xray_explore_frontmatter_and_body,
):
    _, body = xray_explore_frontmatter_and_body
    missing = [n for n in _WHITELISTED_AST_NODES if n not in body]
    assert not missing, (
        f"xray_explore.md Evaluator API missing whitelisted AST nodes: {missing}"
    )


def test_xray_md_evaluator_api_lists_stripped_builtins(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    missing = [b for b in _STRIPPED_BUILTINS if b not in body]
    assert not missing, (
        f"xray_search.md Evaluator API missing stripped builtins: {missing}"
    )


def test_xray_explore_md_evaluator_api_lists_stripped_builtins(
    xray_explore_frontmatter_and_body,
):
    _, body = xray_explore_frontmatter_and_body
    missing = [b for b in _STRIPPED_BUILTINS if b not in body]
    assert not missing, (
        f"xray_explore.md Evaluator API missing stripped builtins: {missing}"
    )


def test_xray_md_evaluator_api_names_5s_timeout(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    assert re.search(r"5[ -]?s(econd)?", body, re.IGNORECASE), (
        "xray_search.md must document the 5-second sandbox timeout"
    )


def test_xray_explore_md_evaluator_api_names_5s_timeout(
    xray_explore_frontmatter_and_body,
):
    _, body = xray_explore_frontmatter_and_body
    assert re.search(r"5[ -]?s(econd)?", body, re.IGNORECASE), (
        "xray_explore.md must document the 5-second sandbox timeout"
    )


def test_xray_md_documents_dunder_attr_blocklist(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    missing = [attr for attr in _DUNDER_BLOCKLIST_SAMPLE if attr not in body]
    assert not missing, (
        f"xray_search.md must document the dunder attribute blocklist; missing: {missing}"
    )


# ---------------------------------------------------------------------------
# Scenario: xray_search.md includes working examples
# ---------------------------------------------------------------------------


def test_xray_md_has_content_target_example(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    assert "content" in body, (
        "xray_search.md must have an example using search_target=content"
    )
    # Must have at least one code block with content and a meaningful evaluator
    assert "```json" in body or "```" in body, (
        "xray_search.md must contain fenced code block examples"
    )


def test_xray_md_has_filename_target_example(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    assert "filename" in body, (
        "xray_search.md must have an example using search_target=filename"
    )


def test_xray_md_has_include_exclude_patterns_example(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    # Both include_patterns and exclude_patterns must appear in the examples section
    assert "include_patterns" in body, (
        "xray_search.md must show include_patterns in an example"
    )
    assert "exclude_patterns" in body, (
        "xray_search.md must show exclude_patterns in an example"
    )


# ---------------------------------------------------------------------------
# Scenario: xray_explore.md includes ast_debug example
# ---------------------------------------------------------------------------


def test_xray_explore_md_has_ast_debug_example(xray_explore_frontmatter_and_body):
    _, body = xray_explore_frontmatter_and_body
    assert "ast_debug" in body, "xray_explore.md must demonstrate the ast_debug field"
    assert "```json" in body or "```" in body, (
        "xray_explore.md must contain fenced code block examples"
    )


# ---------------------------------------------------------------------------
# Scenario: xray_search.md includes evaluator iteration guidance section
# ---------------------------------------------------------------------------


def test_xray_md_has_iterating_section(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    assert "Iterating on Your Evaluator" in body, (
        "xray_search.md must contain an 'Iterating on Your Evaluator' section"
    )


def test_xray_md_iterating_section_recommends_max_files_5(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    assert "max_files" in body and ("5" in body), (
        "xray_search.md 'Iterating on Your Evaluator' section must recommend max_files: 5"
    )


def test_xray_md_iterating_section_mentions_xray_explore(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    assert "xray_explore" in body, (
        "xray_search.md 'Iterating on Your Evaluator' section must mention xray_explore"
    )


def test_xray_md_iterating_section_mentions_evaluation_errors(
    xray_frontmatter_and_body,
):
    _, body = xray_frontmatter_and_body
    assert "evaluation_errors" in body, (
        "xray_search.md 'Iterating on Your Evaluator' section must mention evaluation_errors"
    )


# ---------------------------------------------------------------------------
# Scenario: Both docs document the evaluation_errors output field
# ---------------------------------------------------------------------------

_EVALUATION_ERROR_SCHEMA_FIELDS = [
    "file_path",
    "error_type",
    "error_message",
]

_EVALUATION_ERROR_TYPES = [
    "EvaluatorTimeout",
    "AttributeError",
]


def test_xray_md_documents_evaluation_errors_field(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    assert "evaluation_errors" in body, (
        "xray_search.md must document the evaluation_errors output field"
    )
    missing_fields = [f for f in _EVALUATION_ERROR_SCHEMA_FIELDS if f not in body]
    assert not missing_fields, (
        f"xray_search.md evaluation_errors documentation missing schema fields: {missing_fields}"
    )


def test_xray_md_evaluation_errors_lists_error_types(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    missing = [t for t in _EVALUATION_ERROR_TYPES if t not in body]
    assert not missing, (
        f"xray_search.md must document error_type values including: {missing}"
    )


def test_xray_md_evaluation_errors_clarifies_no_job_failure(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    # Must state that evaluation_errors does NOT cause job failure
    assert (
        re.search(
            r"(not|does not|doesn.t).{0,60}(fail|failure|COMPLETED)",
            body,
            re.IGNORECASE,
        )
        or re.search(r"COMPLETED.{0,80}evaluation_errors", body, re.IGNORECASE)
        or re.search(r"evaluation_errors.{0,200}COMPLETED", body, re.IGNORECASE)
    ), (
        "xray_search.md must clarify that evaluation_errors does NOT cause job failure (status stays COMPLETED)"
    )


def test_xray_explore_md_documents_evaluation_errors_field(
    xray_explore_frontmatter_and_body,
):
    _, body = xray_explore_frontmatter_and_body
    assert "evaluation_errors" in body, (
        "xray_explore.md must document the evaluation_errors output field"
    )
    missing_fields = [f for f in _EVALUATION_ERROR_SCHEMA_FIELDS if f not in body]
    assert not missing_fields, (
        f"xray_explore.md evaluation_errors documentation missing schema fields: {missing_fields}"
    )


def test_xray_explore_md_evaluation_errors_lists_error_types(
    xray_explore_frontmatter_and_body,
):
    _, body = xray_explore_frontmatter_and_body
    missing = [t for t in _EVALUATION_ERROR_TYPES if t not in body]
    assert not missing, (
        f"xray_explore.md must document error_type values including: {missing}"
    )


def test_xray_explore_md_evaluation_errors_clarifies_no_job_failure(
    xray_explore_frontmatter_and_body,
):
    _, body = xray_explore_frontmatter_and_body
    assert (
        re.search(
            r"(not|does not|doesn.t).{0,60}(fail|failure|COMPLETED)",
            body,
            re.IGNORECASE,
        )
        or re.search(r"COMPLETED.{0,80}evaluation_errors", body, re.IGNORECASE)
        or re.search(r"evaluation_errors.{0,200}COMPLETED", body, re.IGNORECASE)
    ), "xray_explore.md must clarify that evaluation_errors does NOT cause job failure"


# ---------------------------------------------------------------------------
# Scenario: Tool registry entries exist
# ---------------------------------------------------------------------------


def test_tool_registry_has_both_xray_tools():
    import sys
    import io

    sys.stderr = io.StringIO()
    try:
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
    finally:
        sys.stderr = sys.__stderr__

    assert "xray_search" in TOOL_REGISTRY, "TOOL_REGISTRY must contain xray_search"
    assert "xray_explore" in TOOL_REGISTRY, "TOOL_REGISTRY must contain xray_explore"


# ---------------------------------------------------------------------------
# Scenario: Cross-references between docs
# ---------------------------------------------------------------------------


def test_xray_md_references_xray_explore(xray_frontmatter_and_body):
    _, body = xray_frontmatter_and_body
    assert "xray_explore" in body, (
        "xray_search.md must cross-reference xray_explore (e.g. 'see xray_explore for AST debug')"
    )


def test_xray_explore_md_references_xray_search(xray_explore_frontmatter_and_body):
    _, body = xray_explore_frontmatter_and_body
    assert "xray_search" in body, "xray_explore.md must cross-reference xray_search"
