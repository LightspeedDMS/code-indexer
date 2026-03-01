"""
Unit tests for dep_map_file_utils shared utilities (Story #342, M1 fix).

Tests cover all 5 module-level functions:
  - load_domains_json
  - parse_yaml_frontmatter
  - parse_simple_yaml
  - has_yaml_frontmatter
  - get_domain_md_files
"""

import json
from pathlib import Path

import pytest


def _get_utils():
    """Import dep_map_file_utils module functions."""
    from code_indexer.server.services.dep_map_file_utils import (
        load_domains_json,
        parse_yaml_frontmatter,
        parse_simple_yaml,
        has_yaml_frontmatter,
        get_domain_md_files,
    )
    return load_domains_json, parse_yaml_frontmatter, parse_simple_yaml, has_yaml_frontmatter, get_domain_md_files


# ─────────────────────────────────────────────────────────────────────────────
# load_domains_json
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadDomainsJson:
    """Tests for load_domains_json()."""

    def test_load_domains_json_returns_list(self, tmp_path):
        """Valid _domains.json returns a list of domain dicts."""
        load_domains_json, _, _, _, _ = _get_utils()

        domains = [
            {"name": "domain-a", "description": "desc-a"},
            {"name": "domain-b", "description": "desc-b"},
        ]
        (tmp_path / "_domains.json").write_text(json.dumps(domains))

        result = load_domains_json(tmp_path)

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "domain-a"
        assert result[1]["name"] == "domain-b"

    def test_load_domains_json_missing_file_returns_empty(self, tmp_path):
        """When _domains.json does not exist, returns empty list."""
        load_domains_json, _, _, _, _ = _get_utils()

        result = load_domains_json(tmp_path)

        assert result == []

    def test_load_domains_json_invalid_json_returns_empty(self, tmp_path):
        """When _domains.json contains invalid JSON, returns empty list."""
        load_domains_json, _, _, _, _ = _get_utils()

        (tmp_path / "_domains.json").write_text("{ this is not valid json }")

        result = load_domains_json(tmp_path)

        assert result == []

    def test_load_domains_json_not_a_list_returns_empty(self, tmp_path):
        """When _domains.json contains a dict (not list), returns empty list."""
        load_domains_json, _, _, _, _ = _get_utils()

        (tmp_path / "_domains.json").write_text(json.dumps({"name": "domain-a"}))

        result = load_domains_json(tmp_path)

        assert result == []

    def test_load_domains_json_empty_list(self, tmp_path):
        """Valid _domains.json with empty list returns empty list."""
        load_domains_json, _, _, _, _ = _get_utils()

        (tmp_path / "_domains.json").write_text("[]")

        result = load_domains_json(tmp_path)

        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# parse_yaml_frontmatter
# ─────────────────────────────────────────────────────────────────────────────


class TestParseYamlFrontmatter:
    """Tests for parse_yaml_frontmatter()."""

    def test_parse_yaml_frontmatter_extracts_fields(self):
        """Frontmatter with scalar fields is parsed correctly."""
        _, parse_yaml_frontmatter, _, _, _ = _get_utils()

        content = """\
---
name: my-domain
description: A test domain
last_analyzed: "2026-01-01T00:00:00Z"
---

# Domain content here
"""
        result = parse_yaml_frontmatter(content)

        assert result is not None
        assert result["name"] == "my-domain"
        assert result["description"] == "A test domain"

    def test_parse_yaml_frontmatter_no_frontmatter_returns_none(self):
        """Content without frontmatter block returns None."""
        _, parse_yaml_frontmatter, _, _, _ = _get_utils()

        content = "# Domain content without frontmatter\n\nSome body text.\n"

        result = parse_yaml_frontmatter(content)

        assert result is None

    def test_parse_yaml_frontmatter_extracts_list(self):
        """Frontmatter with YAML list values is parsed correctly."""
        _, parse_yaml_frontmatter, _, _, _ = _get_utils()

        content = """\
---
name: my-domain
participating_repos:
  - repo-alpha
  - repo-beta
  - repo-gamma
---

# Domain content
"""
        result = parse_yaml_frontmatter(content)

        assert result is not None
        assert "participating_repos" in result
        repos = result["participating_repos"]
        assert isinstance(repos, list)
        assert "repo-alpha" in repos
        assert "repo-beta" in repos
        assert "repo-gamma" in repos

    def test_parse_yaml_frontmatter_strips_quotes(self):
        """Quoted string values have surrounding quotes stripped."""
        _, parse_yaml_frontmatter, _, _, _ = _get_utils()

        content = '''\
---
last_analyzed: "2026-01-01T00:00:00Z"
name: 'single-quoted'
---

# Body
'''
        result = parse_yaml_frontmatter(content)

        assert result is not None
        assert result["last_analyzed"] == "2026-01-01T00:00:00Z"
        assert result["name"] == "single-quoted"

    def test_parse_yaml_frontmatter_incomplete_delimiters_returns_none(self):
        """Content that starts with --- but has no closing --- returns None."""
        _, parse_yaml_frontmatter, _, _, _ = _get_utils()

        content = "---\nname: my-domain\n# No closing delimiter\n"

        result = parse_yaml_frontmatter(content)

        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# parse_simple_yaml
# ─────────────────────────────────────────────────────────────────────────────


class TestParseSimpleYaml:
    """Tests for parse_simple_yaml()."""

    def test_parse_simple_yaml_scalar(self):
        """Scalar key-value pairs are parsed correctly."""
        _, _, parse_simple_yaml, _, _ = _get_utils()

        lines = ["name: my-domain", "description: A test domain"]

        result = parse_simple_yaml(lines)

        assert result["name"] == "my-domain"
        assert result["description"] == "A test domain"

    def test_parse_simple_yaml_list_items(self):
        """List items following a key are parsed into a list."""
        _, _, parse_simple_yaml, _, _ = _get_utils()

        lines = [
            "participating_repos:",
            "  - repo-alpha",
            "  - repo-beta",
        ]

        result = parse_simple_yaml(lines)

        assert "participating_repos" in result
        repos = result["participating_repos"]
        assert isinstance(repos, list)
        assert "repo-alpha" in repos
        assert "repo-beta" in repos

    def test_parse_simple_yaml_empty_lines_ignored(self):
        """Empty lines in frontmatter are skipped without error."""
        _, _, parse_simple_yaml, _, _ = _get_utils()

        lines = ["name: my-domain", "", "description: A test domain", ""]

        result = parse_simple_yaml(lines)

        assert result["name"] == "my-domain"
        assert result["description"] == "A test domain"

    def test_parse_simple_yaml_empty_list_returns_empty_dict(self):
        """Empty line list returns empty dict."""
        _, _, parse_simple_yaml, _, _ = _get_utils()

        result = parse_simple_yaml([])

        assert result == {}


# ─────────────────────────────────────────────────────────────────────────────
# has_yaml_frontmatter
# ─────────────────────────────────────────────────────────────────────────────


class TestHasYamlFrontmatter:
    """Tests for has_yaml_frontmatter()."""

    def test_has_yaml_frontmatter_true(self):
        """Content with opening and closing --- returns True."""
        _, _, _, has_yaml_frontmatter, _ = _get_utils()

        content = "---\nname: domain\n---\n\n# Body\n"

        assert has_yaml_frontmatter(content) is True

    def test_has_yaml_frontmatter_false_no_opening(self):
        """Content without opening --- returns False."""
        _, _, _, has_yaml_frontmatter, _ = _get_utils()

        content = "# No frontmatter here\n\nJust content.\n"

        assert has_yaml_frontmatter(content) is False

    def test_has_yaml_frontmatter_false_no_closing(self):
        """Content with opening --- but no closing --- returns False."""
        _, _, _, has_yaml_frontmatter, _ = _get_utils()

        content = "---\nname: domain\n# No closing delimiter\n"

        assert has_yaml_frontmatter(content) is False

    def test_has_yaml_frontmatter_false_empty_string(self):
        """Empty string returns False."""
        _, _, _, has_yaml_frontmatter, _ = _get_utils()

        assert has_yaml_frontmatter("") is False


# ─────────────────────────────────────────────────────────────────────────────
# get_domain_md_files
# ─────────────────────────────────────────────────────────────────────────────


class TestGetDomainMdFiles:
    """Tests for get_domain_md_files()."""

    def test_get_domain_md_files_returns_non_underscore_files(self, tmp_path):
        """Returns .md files that do NOT start with underscore."""
        _, _, _, _, get_domain_md_files = _get_utils()

        (tmp_path / "domain-a.md").write_text("content a")
        (tmp_path / "domain-b.md").write_text("content b")

        result = get_domain_md_files(tmp_path)

        names = {f.name for f in result}
        assert "domain-a.md" in names
        assert "domain-b.md" in names

    def test_get_domain_md_files_excludes_underscore_prefixed(self, tmp_path):
        """Excludes _index.md, _domains.json, _activity.md and other _*.md files."""
        _, _, _, _, get_domain_md_files = _get_utils()

        (tmp_path / "real-domain.md").write_text("content")
        (tmp_path / "_index.md").write_text("index content")
        (tmp_path / "_activity.md").write_text("activity log")

        result = get_domain_md_files(tmp_path)

        names = {f.name for f in result}
        assert "real-domain.md" in names
        assert "_index.md" not in names
        assert "_activity.md" not in names

    def test_get_domain_md_files_empty_dir_returns_empty_list(self, tmp_path):
        """Empty directory returns empty list."""
        _, _, _, _, get_domain_md_files = _get_utils()

        result = get_domain_md_files(tmp_path)

        assert result == []

    def test_get_domain_md_files_only_underscore_files_returns_empty(self, tmp_path):
        """Directory with only underscore-prefixed .md files returns empty list."""
        _, _, _, _, get_domain_md_files = _get_utils()

        (tmp_path / "_index.md").write_text("index")
        (tmp_path / "_domains.json").write_text("[]")

        result = get_domain_md_files(tmp_path)

        assert result == []
