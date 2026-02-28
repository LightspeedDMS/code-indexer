"""Tests for wiki navigation: sidebar, link rewriting, breadcrumbs (Story #282)."""
import tempfile
from pathlib import Path

import pytest

from code_indexer.server.wiki.wiki_service import WikiService


@pytest.fixture
def svc():
    return WikiService()


class TestBuildSidebarTree:
    def test_flat_articles_in_root_group(self, svc):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "page-1.md").write_text("# Page 1")
            (repo_dir / "page-2.md").write_text("# Page 2")
            tree = svc.build_sidebar_tree(repo_dir, "test-repo")
            assert len(tree) == 1
            root_group = tree[0]
            # Story #288: all articles normalized into categories; uncategorized → "Uncategorized"
            titles = [a["title"] for a in root_group["categories"].get("Uncategorized", [])]
            assert "Page 1" in titles
            assert "Page 2" in titles

    def test_subdirectory_creates_group(self, svc):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            subdir = repo_dir / "guides"
            subdir.mkdir()
            (subdir / "intro.md").write_text("# Intro")
            tree = svc.build_sidebar_tree(repo_dir, "test-repo")
            group_names = [g["name"] for g in tree]
            assert "guides" in group_names

    def test_hidden_dirs_excluded(self, svc):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "visible.md").write_text("# Visible")
            hidden = repo_dir / ".git"
            hidden.mkdir()
            (hidden / "notes.md").write_text("# Hidden")
            tree = svc.build_sidebar_tree(repo_dir, "test-repo")
            # Story #288: all articles normalized into categories; collect from all categories
            all_titles = []
            for group in tree:
                for cat_articles in group["categories"].values():
                    all_titles.extend(a["title"] for a in cat_articles)
            assert "Notes" not in all_titles
            assert "Visible" in all_titles

    def test_articles_sorted_alphabetically(self, svc):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "zebra.md").write_text("# Zebra")
            (repo_dir / "apple.md").write_text("# Apple")
            (repo_dir / "mango.md").write_text("# Mango")
            tree = svc.build_sidebar_tree(repo_dir, "test-repo")
            # Story #288: uncategorized articles go to "Uncategorized" category
            titles = [a["title"] for a in tree[0]["categories"].get("Uncategorized", [])]
            assert titles == sorted(titles, key=str.lower)

    def test_article_path_has_no_extension(self, svc):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "my-article.md").write_text("# My Article")
            tree = svc.build_sidebar_tree(repo_dir, "test-repo")
            # Story #288: uncategorized articles go to "Uncategorized" category
            paths = [a["path"] for a in tree[0]["categories"].get("Uncategorized", [])]
            assert any(".md" not in p for p in paths)
            assert any("my-article" in p for p in paths)

    def test_category_from_front_matter(self, svc):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / "guide.md").write_text("---\ncategory: Tutorials\n---\n# Guide")
            tree = svc.build_sidebar_tree(repo_dir, "test-repo")
            root = tree[0]
            assert "Tutorials" in root["categories"]
            cat_titles = [a["title"] for a in root["categories"]["Tutorials"]]
            assert "Guide" in cat_titles

    def test_empty_repo_returns_empty_tree(self, svc):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            tree = svc.build_sidebar_tree(repo_dir, "test-repo")
            assert tree == []

    def test_groups_sorted_alphabetically(self, svc):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            for name in ["zgroup", "agroup", "mgroup"]:
                d = repo_dir / name
                d.mkdir()
                (d / "page.md").write_text(f"# {name}")
            tree = svc.build_sidebar_tree(repo_dir, "test-repo")
            names = [g["name"] for g in tree]
            assert names == sorted(names, key=str.lower)


class TestRewriteLinks:
    def test_anchor_links_unchanged(self, svc):
        html = '<a href="#section">Jump</a>'
        result = svc.rewrite_links(html, "my-repo", "")
        assert 'href="#section"' in result

    def test_absolute_http_links_get_target_blank(self, svc):
        html = '<a href="https://example.com">Link</a>'
        result = svc.rewrite_links(html, "my-repo", "")
        assert 'target="_blank"' in result
        assert 'rel="noopener"' in result

    def test_relative_link_in_root_dir(self, svc):
        html = '<a href="other-page">Link</a>'
        result = svc.rewrite_links(html, "my-repo", "")
        assert '/wiki/my-repo/other-page' in result

    def test_relative_link_in_subdir(self, svc):
        html = '<a href="sibling-page">Link</a>'
        result = svc.rewrite_links(html, "my-repo", "guides")
        assert '/wiki/my-repo/guides/sibling-page' in result

    def test_slash_link_rewrites_with_repo(self, svc):
        html = '<a href="guides/intro">Link</a>'
        result = svc.rewrite_links(html, "my-repo", "")
        assert '/wiki/my-repo/guides/intro' in result

    def test_already_wiki_prefixed_unchanged(self, svc):
        html = '<a href="/wiki/other-repo/page">Link</a>'
        result = svc.rewrite_links(html, "my-repo", "")
        assert '/wiki/other-repo/page' in result
        assert result.count('/wiki/') == 1

    def test_external_link_with_existing_target_unchanged(self, svc):
        html = '<a href="https://example.com" target="_blank">Link</a>'
        result = svc.rewrite_links(html, "my-repo", "")
        # Should not double-add target
        assert result.count('target=') == 1


class TestBuildBreadcrumbs:
    def test_root_article_path_returns_only_home(self, svc):
        crumbs = svc.build_breadcrumbs("", "my-repo")
        assert len(crumbs) == 1
        assert crumbs[0]["label"] == "my-repo Wiki Home"
        assert crumbs[0]["url"] == "/wiki/my-repo/"

    def test_single_level_article(self, svc):
        crumbs = svc.build_breadcrumbs("my-article", "my-repo")
        assert len(crumbs) == 2
        assert crumbs[0]["label"] == "my-repo Wiki Home"
        assert crumbs[1]["label"] == "My Article"
        assert crumbs[1]["url"] is None

    def test_nested_article_has_parent_crumbs(self, svc):
        crumbs = svc.build_breadcrumbs("guides/intro", "my-repo")
        assert len(crumbs) == 3
        labels = [c["label"] for c in crumbs]
        assert "my-repo Wiki Home" in labels
        assert "guides" in labels
        assert "Intro" in labels

    def test_last_crumb_has_no_url(self, svc):
        crumbs = svc.build_breadcrumbs("section/page", "my-repo")
        assert crumbs[-1]["url"] is None

    def test_intermediate_crumbs_have_urls(self, svc):
        crumbs = svc.build_breadcrumbs("a/b/c", "my-repo")
        for crumb in crumbs[:-1]:
            assert crumb["url"] is not None

    def test_title_format_replaces_hyphens(self, svc):
        crumbs = svc.build_breadcrumbs("getting-started", "my-repo")
        assert crumbs[-1]["label"] == "Getting Started"

    def test_title_format_replaces_underscores(self, svc):
        crumbs = svc.build_breadcrumbs("quick_start", "my-repo")
        assert crumbs[-1]["label"] == "Quick Start"
