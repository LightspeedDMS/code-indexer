"""
Tests for the frontmatter split helper.

Covers:
- split_frontmatter_and_body splits standard YAML frontmatter, handles
  missing frontmatter, missing closing delimiter, and nested lifecycle block
"""

import sys
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


class TestSplitFrontmatterAndBody:
    """Tests for split_frontmatter_and_body helper."""

    def test_splits_standard_yaml_frontmatter(self):
        """Correctly splits content with opening and closing --- delimiters."""
        from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body

        content = "---\nname: test\nurl: http://x\n---\n# Title\n\nBody text.\n"
        fm_dict, body = split_frontmatter_and_body(content)

        assert fm_dict == {"name": "test", "url": "http://x"}
        assert "# Title" in body

    def test_returns_empty_dict_when_no_frontmatter(self):
        """Returns ({}, content) when content does not start with ---."""
        from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body

        content = "# Plain markdown\n\nNo frontmatter here.\n"
        fm_dict, body = split_frontmatter_and_body(content)

        assert fm_dict == {}
        assert body == content

    def test_returns_empty_dict_when_no_closing_delimiter(self):
        """Returns ({}, content) when opening --- has no matching closing ---."""
        from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body

        content = "---\nname: broken\n"
        fm_dict, body = split_frontmatter_and_body(content)

        assert fm_dict == {}
        assert body == content

    def test_nested_lifecycle_block_preserved(self):
        """Nested lifecycle: dict round-trips correctly via yaml.safe_load."""
        from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body

        content = (
            "---\n"
            "name: repo\n"
            "lifecycle:\n"
            "  confidence: high\n"
            "  branches_to_env:\n"
            "    main: production\n"
            "---\n"
            "# Body\n"
        )
        fm_dict, body = split_frontmatter_and_body(content)

        assert fm_dict["lifecycle"]["confidence"] == "high"
        assert fm_dict["lifecycle"]["branches_to_env"]["main"] == "production"
        assert "# Body" in body
