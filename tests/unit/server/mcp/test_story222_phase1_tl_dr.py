"""
Unit tests for Story #222 Phase 1 TODO 1: tl_dr trimming to <= 80 chars.

All tl_dr values in tool_docs must be <= 80 characters after trimming.
TDD: These tests are written FIRST to define expected behavior.
"""

from pathlib import Path
import yaml


TOOL_DOCS_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "mcp"
    / "tool_docs"
)


def _get_tl_dr(md_file: Path) -> str:
    """Parse tl_dr from a tool doc markdown file."""
    content = md_file.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    if len(parts) < 2:
        raise ValueError(f"Missing frontmatter delimiters in {md_file}")
    fm = yaml.safe_load(parts[1])
    return str(fm.get("tl_dr", ""))


class TestTlDrLengths:
    """All 13 target tool docs must have tl_dr trimmed to <= 80 chars."""

    def test_activate_repository_tl_dr_under_80_chars(self):
        """activate_repository tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "repos" / "activate_repository.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"

    def test_get_repository_statistics_tl_dr_under_80_chars(self):
        """get_repository_statistics tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "repos" / "get_repository_statistics.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"

    def test_get_repository_status_tl_dr_under_80_chars(self):
        """get_repository_status tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "repos" / "get_repository_status.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"

    def test_global_repo_status_tl_dr_under_80_chars(self):
        """global_repo_status tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "repos" / "global_repo_status.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"

    def test_discover_repositories_tl_dr_under_80_chars(self):
        """discover_repositories tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "repos" / "discover_repositories.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"

    def test_get_branches_tl_dr_under_80_chars(self):
        """get_branches tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "repos" / "get_branches.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"

    def test_scip_callchain_tl_dr_under_80_chars(self):
        """scip_callchain tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "scip" / "scip_callchain.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"

    def test_scip_impact_tl_dr_under_80_chars(self):
        """scip_impact tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "scip" / "scip_impact.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"

    def test_get_file_content_tl_dr_under_80_chars(self):
        """get_file_content tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "search" / "get_file_content.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"

    def test_scip_references_tl_dr_under_80_chars(self):
        """scip_references tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "scip" / "scip_references.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"

    def test_start_trace_tl_dr_under_80_chars(self):
        """start_trace tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "tracing" / "start_trace.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"

    def test_list_repositories_tl_dr_under_80_chars(self):
        """list_repositories tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "repos" / "list_repositories.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"

    def test_end_trace_tl_dr_under_80_chars(self):
        """end_trace tl_dr must be <= 80 chars."""
        md_file = TOOL_DOCS_DIR / "tracing" / "end_trace.md"
        tl_dr = _get_tl_dr(md_file)
        assert len(tl_dr) <= 80, f"tl_dr is {len(tl_dr)} chars: {tl_dr!r}"


class TestTlDrAudit:
    """Comprehensive audit: all tool docs must have tl_dr <= 80 chars."""

    def test_all_tool_docs_tl_dr_under_80_chars(self):
        """ALL tool docs must have tl_dr <= 80 chars (comprehensive audit)."""
        violations = []
        for md_file in TOOL_DOCS_DIR.rglob("*.md"):
            try:
                tl_dr = _get_tl_dr(md_file)
                if len(tl_dr) > 80:
                    rel = str(md_file).replace(str(TOOL_DOCS_DIR) + "/", "")
                    violations.append(f"{rel}: {len(tl_dr)} chars")
            except (yaml.YAMLError, ValueError, IndexError, KeyError):
                pass  # Skip files with invalid or missing frontmatter

        assert violations == [], (
            f"Found {len(violations)} tool docs with tl_dr > 80 chars:\n"
            + "\n".join(violations)
        )
