"""
Unit tests for Story #859: Dependency Analysis Workflow Guide.

TDD RED phase -- these tests are written before the guide file and cross-references
exist. They define the exact acceptance criteria the implementation must satisfy.

Tests:
- TestWorkflowGuideLoader.test_workflow_guide_frontmatter_valid: loads via real
  ToolDocLoader, asserts name, category, required_permission, tl_dr
- TestWorkflowGuideLoader.test_workflow_guide_has_input_schema: frontmatter has
  inputSchema for TOOL_REGISTRY and get_tool_categories visibility
- test_workflow_guide_body_has_four_sections: body covers all 4 AC-F2 sections
- test_workflow_guide_no_emojis: guide contains no emoji (project standard)
- test_all_depmap_tool_docs_link_to_workflow_guide: parametrized across 5 depmap docs,
  each has a "See also" section referencing dependency_analysis_workflow
"""

import re
from pathlib import Path
from typing import List

import pytest
import yaml

TOOL_DOCS_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "mcp"
    / "tool_docs"
)

DEPMAP_TOOL_DOCS = [
    "depmap/depmap_find_consumers.md",
    "depmap/depmap_get_repo_domains.md",
    "depmap/depmap_get_domain_summary.md",
    "depmap/depmap_get_stale_domains.md",
    "depmap/depmap_get_cross_domain_graph.md",
]


@pytest.fixture
def guide_file() -> Path:
    """Return the path to the workflow guide markdown file."""
    return TOOL_DOCS_DIR / "guides" / "dependency_analysis_workflow.md"


@pytest.fixture
def guide_parts(guide_file: Path) -> List[str]:
    """Parse the guide file once; return the split parts list.

    parts[0] is empty (before first ---), parts[1] is raw YAML frontmatter,
    parts[2] is the body text.
    """
    content = guide_file.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    if len(parts) < 3:
        pytest.fail(f"Missing frontmatter delimiters in {guide_file}")
    return parts


@pytest.fixture
def guide_frontmatter(guide_parts: List[str]) -> dict:
    """Return parsed YAML frontmatter from the shared guide_parts fixture."""
    return yaml.safe_load(guide_parts[1]) or {}


@pytest.fixture
def guide_body(guide_parts: List[str]) -> str:
    """Return the body text (after frontmatter) from the shared guide_parts fixture."""
    return guide_parts[2]


class TestWorkflowGuideLoader:
    """Guide must load correctly via ToolDocLoader with required frontmatter (AC-F1)."""

    def test_workflow_guide_frontmatter_valid(self, guide_frontmatter: dict) -> None:
        """Guide loads via real ToolDocLoader and has name, category, required_permission, tl_dr."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(TOOL_DOCS_DIR)
        docs = loader.load_all_docs()

        assert "dependency_analysis_workflow" in docs, (
            "dependency_analysis_workflow not found in ToolDocLoader cache. "
            "Create guides/dependency_analysis_workflow.md with valid frontmatter."
        )
        doc = docs["dependency_analysis_workflow"]
        assert doc.name == "dependency_analysis_workflow"
        assert doc.category == "guides"
        assert doc.required_permission == "query_repos"
        assert isinstance(doc.tl_dr, str) and len(doc.tl_dr.strip()) > 0

    def test_workflow_guide_has_input_schema(self, guide_frontmatter: dict) -> None:
        """Guide frontmatter must have inputSchema to appear in TOOL_REGISTRY and get_tool_categories."""
        assert "inputSchema" in guide_frontmatter, (
            "inputSchema is required so the guide appears under get_tool_categories"
        )


def test_workflow_guide_body_has_four_sections(guide_body: str) -> None:
    """Guide body must cover all 4 AC-F2 sections: semantic search, depmap,
    anomalies contract, and worked example."""
    assert re.search(r"(?i)semantic\s+search", guide_body), (
        "Guide must have a section about semantic search (phase 1)"
    )
    assert re.search(r"(?i)depmap", guide_body), (
        "Guide must have a section about depmap_* tools (phase 2)"
    )
    assert re.search(r"(?i)anomalies", guide_body), (
        "Guide must document the anomalies[] contract"
    )
    assert re.search(r"(?i)worked\s+example|am\s+i\s+safe", guide_body), (
        "Guide must have a worked example section"
    )


def test_workflow_guide_uses_canonical_response_field_names(guide_body: str) -> None:
    """Guide must name the exact response fields MCP callers key on.

    Story #859 goal is correct interpretation of depmap_* responses. If the
    guide invents field names (e.g. `consumer_repo` instead of `consuming_repo`,
    `dependency_types` instead of `types`), a reader following the guide will
    hit KeyError on real responses. This regression guard pins the guide to the
    canonical contract from the per-tool docs and parser.
    """
    canonical_fields = [
        "consuming_repo",
        "dependency_type",
        "domain_name",
        "types",
    ]
    missing = [f for f in canonical_fields if f not in guide_body]
    assert not missing, (
        f"Guide must mention canonical response field names. Missing: {missing}. "
        "These are the identifiers MCP clients will key on when iterating "
        "depmap_* responses; inventing alternative names misleads readers."
    )
    forbidden_fabricated = [
        "consumer_repo",
        "dependency_types",
    ]
    present_fabricated = [f for f in forbidden_fabricated if f in guide_body]
    assert not present_fabricated, (
        f"Guide must not use fabricated field names that do not exist in any "
        f"depmap_* response. Found: {present_fabricated}. See tool_docs/depmap/ "
        "for the real contract."
    )


def test_workflow_guide_no_emojis(guide_file: Path) -> None:
    """Guide must contain no emoji characters (project documentation standard)."""
    content = guide_file.read_text(encoding="utf-8")
    emoji_chars = [ch for ch in content if ord(ch) >= 0x1F300]
    assert not emoji_chars, (
        f"Guide contains emoji characters (project standard prohibits them): "
        f"{emoji_chars[:5]}"
    )


@pytest.mark.parametrize("doc_path", DEPMAP_TOOL_DOCS)
def test_all_depmap_tool_docs_link_to_workflow_guide(doc_path: str) -> None:
    """Each depmap_* tool doc must contain a 'See also' section referencing the guide."""
    md_file = TOOL_DOCS_DIR / doc_path
    assert md_file.exists(), f"depmap tool doc not found: {md_file}"

    content = md_file.read_text(encoding="utf-8")

    has_see_also = bool(re.search(r"(?i)see\s+also", content))
    has_guide_ref = "dependency_analysis_workflow" in content

    assert has_see_also, (
        f"{doc_path}: Missing 'See also' section. "
        "Add a '### See also' heading with a link to dependency_analysis_workflow."
    )
    assert has_guide_ref, (
        f"{doc_path}: 'dependency_analysis_workflow' not referenced. "
        "Add a link to guides/dependency_analysis_workflow.md under 'See also'."
    )
