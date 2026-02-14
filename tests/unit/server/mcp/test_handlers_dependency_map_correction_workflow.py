"""
Tests for dependency map correction workflow guidance (Story #197 AC5).

Tests verify that the dependency map section includes guidance on:
- How to use edit_file with cidx-meta-global
- Preserving YAML frontmatter structure
- Auto-reindexing and re-verification
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from code_indexer.server.mcp.handlers import _build_dependency_map_section


class TestDependencyMapCorrectionWorkflow:
    """Test correction workflow guidance in dependency map section."""

    def test_section_contains_correction_workflow_heading(self):
        """Test that section includes 'Correcting Inaccuracies' subsection."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Should contain correction workflow heading
            assert "Correcting" in result or "Correction" in result

    def test_section_mentions_edit_file_tool(self):
        """Test that section mentions using edit_file for corrections."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Should mention edit_file
            assert "edit_file" in result

    def test_section_mentions_cidx_meta_global_alias(self):
        """Test that section specifies cidx-meta-global as the repository alias."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Should mention cidx-meta-global
            assert "cidx-meta-global" in result

    def test_section_mentions_yaml_frontmatter_preservation(self):
        """Test that section warns about preserving YAML frontmatter."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Should mention YAML frontmatter or structure preservation
            assert "frontmatter" in result.lower() or "structure" in result.lower()

    def test_section_mentions_auto_reindexing(self):
        """Test that section mentions automatic reindexing."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Should mention reindex or auto-reindex
            assert "reindex" in result.lower() or "watch" in result.lower()

    def test_section_mentions_next_refresh_verification(self):
        """Test that section mentions re-verification on next refresh."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Should mention refresh or verification
            assert "refresh" in result.lower() or "verif" in result.lower()

    def test_correction_workflow_has_numbered_steps(self):
        """Test that correction workflow is presented as numbered steps."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # Look for numbered steps in correction workflow section
            correction_section = result.lower()

            # Explicit check - fail if correction section not present
            assert "correct" in correction_section, "Section must contain correction guidance"

            # Count numbered items after "correct" keyword
            after_correct = correction_section[correction_section.index("correct"):]
            numbered_items = [line for line in after_correct.split('\n') if line.strip().startswith(('1.', '2.', '3.', '4.'))]
            assert len(numbered_items) >= 3, "Correction workflow should have at least 3 steps"

    def test_section_complete_with_all_correction_elements(self):
        """Test that section includes all required correction workflow elements."""
        with TemporaryDirectory() as tmpdir:
            cidx_meta_path = Path(tmpdir)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()

            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")

            result = _build_dependency_map_section(cidx_meta_path)

            # All key elements should be present
            required_elements = [
                "edit_file",           # Tool to use
                "cidx-meta-global",    # Repository alias
                "frontmatter",         # Preservation warning
                "reindex",             # Auto-reindexing mention
            ]

            for element in required_elements:
                assert element.lower() in result.lower(), \
                    f"Correction workflow missing required element: {element}"
