"""Tests for Story #301: Wiki Search Navigation State and Duplicate Category Badge.

RED phase: all tests written BEFORE implementation.

Covers:
 - AC4: suppress_category_badge when visibility and category have the same value (case-insensitive)
 - AC5: show category field when visibility and category differ
 - Edge cases: missing fields, empty strings, case variations
 - Template integration: rendered HTML reflects suppress_category_badge flag
"""
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.wiki.wiki_service import WikiService
from code_indexer.server.wiki.routes import wiki_router, get_wiki_user_hybrid
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_admin_user(username: str = "alice") -> User:
    """Create a real User object with ADMIN role so has_permission() works."""
    return User(
        username=username,
        password_hash="$2b$12$fakehash",
        role=UserRole.ADMIN,
        created_at=datetime(2026, 1, 1),
    )


def _make_app(actual_repo_path, db_path, authenticated_user=None):
    """Build a FastAPI test app with wiki router and minimal app state."""
    from code_indexer.server.wiki.routes import _reset_wiki_cache
    _reset_wiki_cache()

    app = FastAPI()
    if authenticated_user:
        app.dependency_overrides[get_wiki_user_hybrid] = lambda: authenticated_user

    app.include_router(wiki_router, prefix="/wiki")

    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.get_wiki_enabled.return_value = True
    app.state.golden_repo_manager.db_path = db_path

    golden_repos_dir = Path(actual_repo_path).parent / "golden-repos-test-dedup"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    make_aliases_dir(str(golden_repos_dir), "test-repo", actual_repo_path)
    app.state.golden_repo_manager.golden_repos_dir = str(golden_repos_dir)

    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = True
    app.state.access_filtering_service.get_accessible_repos.return_value = {"test-repo"}
    return app


# ---------------------------------------------------------------------------
# Unit tests: WikiService.prepare_metadata_context - suppress_category_badge
# AC4: when visibility and category match (case-insensitive) → suppress category
# AC5: when they differ → show both
# ---------------------------------------------------------------------------


class TestSuppressCategoryBadge:
    """Tests for suppress_category_badge logic in prepare_metadata_context (Story #301)."""

    def setup_method(self):
        self.service = WikiService()

    def _make_cache(self, view_count=0):
        cache = MagicMock()
        cache.get_view_count.return_value = view_count
        return cache

    # AC4: Same value suppresses category field
    def test_suppress_category_when_visibility_equals_category_exact(self):
        """AC4: visibility='Internal' and category='Internal' → suppress_category_badge=True."""
        cache = self._make_cache()
        metadata = {"visibility": "Internal", "category": "Internal"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("suppress_category_badge") is True

    def test_suppress_category_when_visibility_lowercase_category_mixed(self):
        """AC4: visibility='internal' and category='Internal' → suppress_category_badge=True (case-insensitive)."""
        cache = self._make_cache()
        metadata = {"visibility": "internal", "category": "Internal"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("suppress_category_badge") is True

    def test_suppress_category_when_visibility_mixed_category_uppercase(self):
        """AC4: visibility='Internal' and category='INTERNAL' → suppress_category_badge=True (case-insensitive)."""
        cache = self._make_cache()
        metadata = {"visibility": "Internal", "category": "INTERNAL"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("suppress_category_badge") is True

    def test_suppress_category_all_uppercase(self):
        """AC4: visibility='INTERNAL' and category='INTERNAL' → suppress_category_badge=True."""
        cache = self._make_cache()
        metadata = {"visibility": "INTERNAL", "category": "INTERNAL"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("suppress_category_badge") is True

    def test_suppress_category_all_lowercase(self):
        """AC4: visibility='internal' and category='internal' → suppress_category_badge=True."""
        cache = self._make_cache()
        metadata = {"visibility": "internal", "category": "internal"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("suppress_category_badge") is True

    def test_suppress_category_public_public(self):
        """AC4: visibility='public' and category='public' → suppress_category_badge=True."""
        cache = self._make_cache()
        metadata = {"visibility": "public", "category": "public"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("suppress_category_badge") is True

    # AC5: Different values → show both (suppress_category_badge=False)
    def test_show_category_when_visibility_and_category_differ(self):
        """AC5: visibility='internal' and category='Architecture' → suppress_category_badge=False."""
        cache = self._make_cache()
        metadata = {"visibility": "internal", "category": "Architecture"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("suppress_category_badge") is False

    def test_show_category_when_visibility_public_category_guides(self):
        """AC5: visibility='public' and category='Guides' → suppress_category_badge=False."""
        cache = self._make_cache()
        metadata = {"visibility": "public", "category": "Guides"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("suppress_category_badge") is False

    def test_show_category_when_visibility_internal_category_operations(self):
        """AC5: visibility='internal' and category='Operations' → suppress_category_badge=False."""
        cache = self._make_cache()
        metadata = {"visibility": "internal", "category": "Operations"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("suppress_category_badge") is False

    # Edge cases: missing fields
    def test_no_suppress_flag_when_only_visibility_present(self):
        """Only visibility present (no category) → suppress_category_badge not set or False."""
        cache = self._make_cache()
        metadata = {"visibility": "internal"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        # When there is no category, the flag should not suppress anything meaningful
        # It should be absent or False - there's nothing to suppress
        assert ctx.get("suppress_category_badge") in (False, None)

    def test_no_suppress_flag_when_only_category_present(self):
        """Only category present (no visibility) → suppress_category_badge not set or False."""
        cache = self._make_cache()
        metadata = {"category": "Architecture"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        # When there is no visibility, the flag should not be True
        assert ctx.get("suppress_category_badge") in (False, None)

    def test_no_suppress_flag_when_neither_present(self):
        """Neither visibility nor category → suppress_category_badge not set or False."""
        cache = self._make_cache()
        ctx = self.service.prepare_metadata_context({}, "repo1", "article", cache)
        assert ctx.get("suppress_category_badge") in (False, None)

    def test_category_still_in_context_when_not_suppressed(self):
        """When suppress_category_badge=False, category key is still in the context."""
        cache = self._make_cache()
        metadata = {"visibility": "internal", "category": "Architecture"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("category") == "Architecture"
        assert ctx.get("suppress_category_badge") is False

    def test_category_still_in_context_even_when_suppressed(self):
        """When suppress_category_badge=True, category key is still present (template decides display)."""
        cache = self._make_cache()
        metadata = {"visibility": "internal", "category": "internal"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        # The category key must still exist in context (template uses the flag, not absence of key)
        assert ctx.get("category") == "internal"
        assert ctx.get("suppress_category_badge") is True

    def test_suppress_category_badge_false_when_different_regardless_of_case(self):
        """'Internal' vs 'ARCHITECTURE' are different → suppress=False regardless of their own casing."""
        cache = self._make_cache()
        metadata = {"visibility": "INTERNAL", "category": "Architecture"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("suppress_category_badge") is False

    def test_draft_flag_sets_visibility_draft_different_from_category(self):
        """draft=True sets visibility='draft'; if category != 'draft' → suppress=False."""
        cache = self._make_cache()
        metadata = {"draft": True, "category": "Operations"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("visibility") == "draft"
        assert ctx.get("suppress_category_badge") is False

    def test_draft_flag_with_matching_category_draft(self):
        """draft=True sets visibility='draft'; if category='draft' → suppress=True."""
        cache = self._make_cache()
        metadata = {"draft": True, "category": "draft"}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("visibility") == "draft"
        assert ctx.get("suppress_category_badge") is True

    def test_whitespace_stripped_before_comparison(self):
        """Whitespace around category values is stripped before comparison."""
        cache = self._make_cache()
        metadata = {"visibility": "internal", "category": "  internal  "}
        ctx = self.service.prepare_metadata_context(metadata, "repo1", "article", cache)
        assert ctx.get("suppress_category_badge") is True


# ---------------------------------------------------------------------------
# Route integration: template rendering of suppress_category_badge
# AC4: same visibility/category → no "Category: X" text field shown
# AC5: different visibility/category → "Category: X" text field shown
# ---------------------------------------------------------------------------


class TestCategoryBadgeTemplateIntegration:
    """Integration tests for AC4/AC5 category badge deduplication in rendered HTML."""

    def _make_article(self, repo_dir, filename, frontmatter_lines, body="# Article\nContent."):
        frontmatter = "---\n" + "\n".join(frontmatter_lines) + "\n---\n"
        (Path(repo_dir) / f"{filename}.md").write_text(frontmatter + body)

    def test_ac4_category_field_absent_when_same_as_visibility(self):
        """AC4: visibility='internal' and category='Internal' → 'Category: Internal' NOT in HTML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                self._make_article(
                    tmpdir, "same",
                    ["title: Same Article", "visibility: internal", "category: Internal"],
                )
                app = _make_app(tmpdir, db_path, authenticated_user=_make_admin_user("alice"))
                client = TestClient(app)

                resp = client.get("/wiki/test-repo/same")
                assert resp.status_code == 200
                # The visibility badge should be present
                assert "internal" in resp.text
                # The duplicate "Category: Internal" field must NOT appear
                assert "Category: Internal" not in resp.text
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

    def test_ac4_case_insensitive_same_value_no_category_field(self):
        """AC4: visibility='internal' and category='INTERNAL' (all caps) → 'Category:' NOT in HTML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                self._make_article(
                    tmpdir, "caps",
                    ["title: Caps Article", "visibility: internal", "category: INTERNAL"],
                )
                app = _make_app(tmpdir, db_path, authenticated_user=_make_admin_user("alice"))
                client = TestClient(app)

                resp = client.get("/wiki/test-repo/caps")
                assert resp.status_code == 200
                assert "Category: INTERNAL" not in resp.text
                assert "Category: Internal" not in resp.text
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

    def test_ac5_category_field_present_when_different_from_visibility(self):
        """AC5: visibility='internal' and category='Architecture' → 'Category: Architecture' IN HTML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                self._make_article(
                    tmpdir, "diff",
                    ["title: Different Article", "visibility: internal", "category: Architecture"],
                )
                app = _make_app(tmpdir, db_path, authenticated_user=_make_admin_user("alice"))
                client = TestClient(app)

                resp = client.get("/wiki/test-repo/diff")
                assert resp.status_code == 200
                # Both the visibility badge and the category field must appear
                assert "internal" in resp.text
                assert "Category: Architecture" in resp.text
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

    def test_ac5_visibility_badge_still_shown_even_with_different_category(self):
        """AC5: visibility badge is still rendered when visibility != category."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                self._make_article(
                    tmpdir, "both",
                    ["title: Both Fields", "visibility: public", "category: Guides"],
                )
                app = _make_app(tmpdir, db_path, authenticated_user=_make_admin_user("alice"))
                client = TestClient(app)

                resp = client.get("/wiki/test-repo/both")
                assert resp.status_code == 200
                assert "metadata-badge" in resp.text
                assert "Category: Guides" in resp.text
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass

    def test_no_category_field_when_category_absent(self):
        """Article with no category front matter → no 'Category:' text in HTML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                self._make_article(
                    tmpdir, "nocat",
                    ["title: No Category", "visibility: public"],
                )
                app = _make_app(tmpdir, db_path, authenticated_user=_make_admin_user("alice"))
                client = TestClient(app)

                resp = client.get("/wiki/test-repo/nocat")
                assert resp.status_code == 200
                # No category text at all
                assert "Category:" not in resp.text
            finally:
                try:
                    os.unlink(db_path)
                except OSError:
                    pass
