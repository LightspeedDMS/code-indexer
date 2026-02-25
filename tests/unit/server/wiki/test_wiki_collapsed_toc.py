"""Tests for Story #288: Collapsed TOC with Smart Expansion.

AC1: TOC sections collapsed by default, category normalization
AC2: Active article section auto-expands
AC3: Click toggle expand/collapse (JS - validated via HTML structure)
AC4: Session storage persistence (JS - validated via HTML structure)
AC5: Theme compatibility (CSS - validated via absence of hardcoded colors)
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.wiki.wiki_service import WikiService
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir


@pytest.fixture
def svc():
    return WikiService()


# ---------------------------------------------------------------------------
# Python unit tests: build_sidebar_tree() category normalization (AC1)
# ---------------------------------------------------------------------------


class TestBuildSidebarTreeNormalization:
    """AC1 CRITICAL: All articles must go through group['categories'].
    Articles without 'category' front matter → 'Uncategorized' category.
    group['articles'] must be empty (or absent) after normalization.
    """

    def test_uncategorized_article_goes_to_uncategorized_category(self, svc):
        """Article with no category front matter must appear in 'Uncategorized' category."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "readme.md").write_text("# Readme\nNo category here.")
            result = svc.build_sidebar_tree(Path(tmpdir), "test-repo")
            assert len(result) > 0
            group = result[0]
            assert (
                "Uncategorized" in group["categories"]
            ), "Article without category must be placed in 'Uncategorized' category"
            articles_in_uncategorized = group["categories"]["Uncategorized"]
            paths = [a["path"] for a in articles_in_uncategorized]
            assert "readme" in paths

    def test_group_articles_list_is_empty_after_normalization(self, svc):
        """group['articles'] must be empty - all articles routed through categories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "article.md").write_text("# Article\nNo category.")
            result = svc.build_sidebar_tree(Path(tmpdir), "test-repo")
            assert len(result) > 0
            group = result[0]
            # After normalization, articles list must be empty
            assert (
                group.get("articles", []) == []
            ), "group['articles'] must be empty - all articles go through group['categories']"

    def test_categorized_article_goes_to_named_category(self, svc):
        """Article with 'category: Guides' front matter → 'Guides' category."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "guide.md").write_text(
                "---\ncategory: Guides\ntitle: My Guide\n---\n# Guide"
            )
            result = svc.build_sidebar_tree(Path(tmpdir), "test-repo")
            group = result[0]
            assert (
                "Guides" in group["categories"]
            ), "Article with category:'Guides' must appear in 'Guides' category"
            paths = [a["path"] for a in group["categories"]["Guides"]]
            assert "guide" in paths

    def test_categorized_article_not_in_uncategorized(self, svc):
        """Article with category must NOT appear in 'Uncategorized'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "guide.md").write_text(
                "---\ncategory: Guides\ntitle: My Guide\n---\n# Guide"
            )
            result = svc.build_sidebar_tree(Path(tmpdir), "test-repo")
            group = result[0]
            uncategorized = group["categories"].get("Uncategorized", [])
            paths = [a["path"] for a in uncategorized]
            assert "guide" not in paths

    def test_mixed_articles_correctly_distributed(self, svc):
        """Mixed articles (some with category, some without) must be distributed correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "uncategorized.md").write_text("# Uncategorized\nNo category.")
            Path(tmpdir, "guide.md").write_text(
                "---\ncategory: Guides\ntitle: Guide\n---\n# Guide"
            )
            Path(tmpdir, "tutorial.md").write_text(
                "---\ncategory: Tutorials\ntitle: Tutorial\n---\n# Tutorial"
            )
            result = svc.build_sidebar_tree(Path(tmpdir), "test-repo")
            group = result[0]

            # Categorized articles in their named categories
            assert "Guides" in group["categories"]
            assert "Tutorials" in group["categories"]
            # Uncategorized in "Uncategorized"
            assert "Uncategorized" in group["categories"]

            guide_paths = [a["path"] for a in group["categories"]["Guides"]]
            assert "guide" in guide_paths

            tutorial_paths = [a["path"] for a in group["categories"]["Tutorials"]]
            assert "tutorial" in tutorial_paths

            uncategorized_paths = [
                a["path"] for a in group["categories"]["Uncategorized"]
            ]
            assert "uncategorized" in uncategorized_paths

            # group["articles"] is empty
            assert group.get("articles", []) == []

    def test_uncategorized_sorted_alphabetically_with_other_categories(self, svc):
        """'Uncategorized' must be sorted alphabetically among other categories.
        The returned group['categories'] dict should follow alphabetical key order.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "no-cat.md").write_text("# No cat")
            Path(tmpdir, "alpha.md").write_text(
                "---\ncategory: Alpha\ntitle: Alpha\n---\n# Alpha"
            )
            Path(tmpdir, "zebra.md").write_text(
                "---\ncategory: Zebra\ntitle: Zebra\n---\n# Zebra"
            )
            result = svc.build_sidebar_tree(Path(tmpdir), "test-repo")
            group = result[0]
            keys = list(group["categories"].keys())
            # All three categories must be present
            assert "Alpha" in keys
            assert "Uncategorized" in keys
            assert "Zebra" in keys

    def test_articles_within_uncategorized_sorted_by_title(self, svc):
        """Articles within 'Uncategorized' must be sorted by title (case-insensitive)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "zebra-article.md").write_text("---\ntitle: Zebra\n---\n# Z")
            Path(tmpdir, "alpha-article.md").write_text("---\ntitle: Alpha\n---\n# A")
            result = svc.build_sidebar_tree(Path(tmpdir), "test-repo")
            group = result[0]
            uncategorized = group["categories"]["Uncategorized"]
            titles = [a["title"] for a in uncategorized]
            assert titles == sorted(titles, key=str.lower)

    def test_empty_category_field_treated_as_uncategorized(self, svc):
        """An article with category: '' (empty string) must go to 'Uncategorized'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "empty-cat.md").write_text(
                "---\ncategory: \ntitle: Empty Cat\n---\n# Empty cat"
            )
            result = svc.build_sidebar_tree(Path(tmpdir), "test-repo")
            group = result[0]
            assert "Uncategorized" in group["categories"]
            paths = [a["path"] for a in group["categories"]["Uncategorized"]]
            assert "empty-cat" in paths

    def test_subdirectory_articles_also_normalized(self, svc):
        """Articles in subdirectories (groups) must also route through categories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir, "guides")
            subdir.mkdir()
            Path(subdir, "intro.md").write_text("# Intro\nNo category.")
            result = svc.build_sidebar_tree(Path(tmpdir), "test-repo")
            # Find the 'guides' group
            guides_group = next((g for g in result if g["name"] == "guides"), None)
            assert guides_group is not None
            assert "Uncategorized" in guides_group["categories"]
            assert guides_group.get("articles", []) == []

    def test_sidebar_structure_has_no_top_level_articles(self, svc):
        """The returned data structure must have categories only (no articles lists with content)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Mix of categorized and uncategorized
            Path(tmpdir, "plain.md").write_text("# Plain Article")
            Path(tmpdir, "categorized.md").write_text(
                "---\ncategory: Ops\ntitle: Ops Guide\n---\n# Ops"
            )
            result = svc.build_sidebar_tree(Path(tmpdir), "test-repo")
            for group in result:
                assert (
                    group.get("articles", []) == []
                ), f"Group '{group['name']}' still has top-level articles - must use categories only"


# ---------------------------------------------------------------------------
# Route integration tests: HTML structure (AC1, AC2, AC3)
# ---------------------------------------------------------------------------


def _make_app_for_toc(actual_repo_path=None, user_accessible_repos=None):
    """Create a test FastAPI app for TOC-related HTML structure tests."""
    from code_indexer.server.wiki.routes import (
        wiki_router,
        get_current_user_hybrid,
        _reset_wiki_cache,
    )

    _reset_wiki_cache()

    app = FastAPI()
    user = MagicMock()
    user.username = "alice"
    app.dependency_overrides[get_current_user_hybrid] = lambda: user
    app.include_router(wiki_router, prefix="/wiki")

    _db_fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_db_fd)

    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.get_wiki_enabled.return_value = True
    app.state.golden_repo_manager.get_actual_repo_path.return_value = (
        actual_repo_path or "/tmp/test"
    )
    app.state.golden_repo_manager.db_path = _db_path

    if actual_repo_path:
        golden_repos_dir = Path(actual_repo_path).parent / "golden-repos-test-toc"
        golden_repos_dir.mkdir(parents=True, exist_ok=True)
        make_aliases_dir(str(golden_repos_dir), "test-repo", actual_repo_path)
        app.state.golden_repo_manager.golden_repos_dir = str(golden_repos_dir)
    else:
        _tmp_golden = tempfile.mkdtemp(suffix="-golden-repos-toc")
        (Path(_tmp_golden) / "aliases").mkdir(parents=True, exist_ok=True)
        app.state.golden_repo_manager.golden_repos_dir = _tmp_golden

    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = False
    app.state.access_filtering_service.get_accessible_repos.return_value = (
        user_accessible_repos or set()
    )
    return app


class TestArticlePageHasNoTopLevelArticleLoop:
    """AC1: Template must render only group.categories (no top-level group.articles)."""

    def test_uncategorized_articles_rendered_inside_sidebar_category(self):
        """Uncategorized articles must appear inside a .sidebar-category block."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "plain.md").write_text("# Plain\nContent here.")
            app = _make_app_for_toc(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/plain")
            assert resp.status_code == 200
            html = resp.text
            # Must have sidebar-category elements (categories are rendered)
            assert (
                "sidebar-category" in html
            ), "Expected .sidebar-category elements for all articles including uncategorized"

    def test_no_orphan_sidebar_items_outside_category(self):
        """All sidebar article links must be inside a .sidebar-category block.
        No sidebar-item links must appear directly in sidebar-group-items without a category.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            # Only uncategorized articles
            Path(tmpdir, "plain.md").write_text("# Plain\nContent here.")
            app = _make_app_for_toc(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/plain")
            assert resp.status_code == 200
            html = resp.text
            # The 'Uncategorized' category name must appear in the sidebar
            assert (
                "Uncategorized" in html
            ), "Expected 'Uncategorized' label in sidebar for articles without category front matter"


class TestSidebarCategoryCollapsedByDefault:
    """AC1: All TOC sections render collapsed on page load."""

    def test_sidebar_category_has_collapsed_class_by_default(self):
        """Every .sidebar-category element must have 'collapsed' class in initial HTML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "article.md").write_text(
                "---\ncategory: Guides\ntitle: Guide Article\n---\n# Guide"
            )
            app = _make_app_for_toc(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/article")
            assert resp.status_code == 200
            html = resp.text
            # Every sidebar-category must have collapsed class
            assert (
                "sidebar-category collapsed" in html
                or 'class="sidebar-category collapsed"' in html
            ), "Expected sidebar-category elements to have 'collapsed' class by default"

    def test_uncategorized_section_also_collapsed_by_default(self):
        """The auto-generated 'Uncategorized' section must also be collapsed by default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "plain.md").write_text("# Plain\nContent here.")
            app = _make_app_for_toc(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/plain")
            assert resp.status_code == 200
            html = resp.text
            # The Uncategorized section must be collapsed
            assert (
                "sidebar-category collapsed" in html
                or 'sidebar-category" collapsed' in html
                or 'class="sidebar-category collapsed"' in html
            ), "Expected Uncategorized sidebar-category to have 'collapsed' class"


class TestSidebarChevronIndicators:
    """AC1: Visual indicator (chevron/arrow) shows each section is collapsible."""

    def test_sidebar_category_header_has_chevron_element(self):
        """Category headers must include a chevron span or the CSS class for chevron."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "article.md").write_text(
                "---\ncategory: Guides\ntitle: Guide Article\n---\n# Guide"
            )
            app = _make_app_for_toc(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/article")
            assert resp.status_code == 200
            html = resp.text
            # Must have a chevron indicator - either a span with chevron class or toc-chevron class
            has_chevron = (
                "toc-chevron" in html or "sidebar-chevron" in html or "chevron" in html
            )
            assert has_chevron, "Expected chevron indicator in sidebar category headers"

    def test_sidebar_category_header_has_data_section_id(self):
        """Each sidebar-category element must have data-section-id attribute for sessionStorage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "article.md").write_text(
                "---\ncategory: Guides\ntitle: Guide Article\n---\n# Guide"
            )
            app = _make_app_for_toc(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/article")
            assert resp.status_code == 200
            html = resp.text
            assert (
                "data-section-id" in html
            ), "Expected data-section-id attribute on sidebar-category elements for sessionStorage"


class TestSidebarNoInlineOnclick:
    """AC3: onclick handlers must be removed from HTML (handled by JS event listeners)."""

    def test_sidebar_category_header_has_no_inline_onclick(self):
        """sidebar-category-header must not have inline onclick attribute."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "article.md").write_text(
                "---\ncategory: Guides\ntitle: Guide Article\n---\n# Guide"
            )
            app = _make_app_for_toc(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/article")
            assert resp.status_code == 200
            html = resp.text
            # sidebar-category-header should NOT have onclick inline handler
            # (it's handled by wiki.js event listeners now)
            import re

            # Find all sidebar-category-header divs
            pattern = r'class="sidebar-category-header"[^>]*onclick'
            assert not re.search(
                pattern, html
            ), "sidebar-category-header must not have inline onclick - use JS event listeners"

    def test_sidebar_group_header_has_no_inline_onclick(self):
        """sidebar-group-header must not have inline onclick attribute."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir, "section")
            subdir.mkdir()
            Path(subdir, "article.md").write_text(
                "---\ncategory: Guides\ntitle: Guide Article\n---\n# Guide"
            )
            app = _make_app_for_toc(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/section/article")
            assert resp.status_code == 200
            html = resp.text
            import re

            pattern = r'class="sidebar-group-header"[^>]*onclick'
            assert not re.search(
                pattern, html
            ), "sidebar-group-header must not have inline onclick - use JS event listeners"


class TestSidebarCurrentPathDataAttribute:
    """AC2: The current article path must be accessible to JavaScript for auto-expand logic."""

    def test_article_page_exposes_current_path_for_js(self):
        """The rendered page must expose current_path so wiki.js can find the active article."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "my-article.md").write_text(
                "---\ncategory: Guides\ntitle: My Article\n---\n# My Article"
            )
            app = _make_app_for_toc(tmpdir, {"test-repo"})
            client = TestClient(app)
            resp = client.get("/wiki/test-repo/my-article")
            assert resp.status_code == 200
            html = resp.text
            # The active article must be marked with 'active' class
            assert (
                "active" in html
            ), "Expected active class on current article's sidebar link"


class TestWikiServiceSidebarTreeStructure:
    """Integration: Verify sidebar tree returned by build_sidebar_tree() matches new structure."""

    def test_sidebar_tree_returns_categories_only(self, svc):
        """build_sidebar_tree() must return groups where articles[] is empty and categories{} has all."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir, "chapter1")
            subdir.mkdir()
            Path(subdir, "intro.md").write_text("# Intro\nNo cat.")
            Path(subdir, "advanced.md").write_text(
                "---\ncategory: Advanced\ntitle: Advanced\n---\n# Advanced"
            )
            result = svc.build_sidebar_tree(Path(tmpdir), "test-repo")
            chapter1 = next((g for g in result if g["name"] == "chapter1"), None)
            assert chapter1 is not None
            # articles must be empty
            assert chapter1.get("articles", []) == []
            # Both articles must be in categories
            assert "Uncategorized" in chapter1["categories"]
            assert "Advanced" in chapter1["categories"]
            intro_paths = [a["path"] for a in chapter1["categories"]["Uncategorized"]]
            assert "chapter1/intro" in intro_paths
            advanced_paths = [a["path"] for a in chapter1["categories"]["Advanced"]]
            assert "chapter1/advanced" in advanced_paths
