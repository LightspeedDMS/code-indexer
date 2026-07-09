"""Unit tests for _global_fallback helper module (Story #1039).

Tests:
1. try_global_fallback: bare alias + globally active -> returns suffixed alias
2. try_global_fallback: bare alias + NOT globally active -> returns None
3. try_global_fallback: already -global suffix -> returns None (pass-through)
4. try_global_fallback: None / empty alias -> returns None
5. user_has_activated_repo: user has repo -> returns True
6. user_has_activated_repo: user does NOT have repo -> returns False
"""

from unittest.mock import MagicMock


class TestTryGlobalFallback:
    """Tests for try_global_fallback helper."""

    def _make_grm(self, is_globally_active: bool) -> MagicMock:
        """Build a mock GoldenRepoManager with is_globally_active preset."""
        grm = MagicMock()
        grm.is_globally_active.return_value = is_globally_active
        return grm

    def test_bare_alias_globally_active_returns_suffixed(self):
        """Bare alias whose golden repo is globally active -> returns '<alias>-global'."""
        from code_indexer.server.mcp.handlers._global_fallback import (
            try_global_fallback,
        )

        grm = self._make_grm(is_globally_active=True)
        result = try_global_fallback("evolution", grm)

        assert result == "evolution-global"
        grm.is_globally_active.assert_called_once_with("evolution")

    def test_bare_alias_not_globally_active_returns_none(self):
        """Bare alias whose golden repo is NOT globally active -> returns None."""
        from code_indexer.server.mcp.handlers._global_fallback import (
            try_global_fallback,
        )

        grm = self._make_grm(is_globally_active=False)
        result = try_global_fallback("evolution", grm)

        assert result is None

    def test_already_global_suffix_returns_none(self):
        """Alias already ending in '-global' -> returns None (no double-suffix)."""
        from code_indexer.server.mcp.handlers._global_fallback import (
            try_global_fallback,
        )

        grm = self._make_grm(is_globally_active=True)
        result = try_global_fallback("evolution-global", grm)

        assert result is None
        grm.is_globally_active.assert_not_called()

    def test_none_alias_returns_none(self):
        """None alias -> returns None without touching golden_repo_manager."""
        from code_indexer.server.mcp.handlers._global_fallback import (
            try_global_fallback,
        )

        grm = self._make_grm(is_globally_active=True)
        result = try_global_fallback(None, grm)

        assert result is None
        grm.is_globally_active.assert_not_called()

    def test_empty_string_alias_returns_none(self):
        """Empty string alias -> returns None without touching golden_repo_manager."""
        from code_indexer.server.mcp.handlers._global_fallback import (
            try_global_fallback,
        )

        grm = self._make_grm(is_globally_active=True)
        result = try_global_fallback("", grm)

        assert result is None
        grm.is_globally_active.assert_not_called()


class TestUserHasActivatedRepo:
    """Tests for user_has_activated_repo method on ActivatedRepoManager."""

    def _make_arm(self, aliases: list) -> MagicMock:
        """Build a mock ActivatedRepoManager returning given list of repo dicts."""
        arm = MagicMock()
        arm.list_activated_repositories.return_value = [
            {"user_alias": a} for a in aliases
        ]
        return arm

    def test_user_has_repo_returns_true(self):
        """User with an activated repo matching alias -> returns True."""
        from code_indexer.server.repositories.activated_repo_manager import (
            ActivatedRepoManager,
        )

        arm = self._make_arm(["evolution", "other-repo"])
        # Temporarily bind the method and call it directly
        result = ActivatedRepoManager.user_has_activated_repo(
            arm, "testuser", "evolution"
        )

        assert result is True
        arm.list_activated_repositories.assert_called_once_with("testuser")

    def test_user_lacks_repo_returns_false(self):
        """User with no matching activated repo -> returns False."""
        from code_indexer.server.repositories.activated_repo_manager import (
            ActivatedRepoManager,
        )

        arm = self._make_arm(["other-repo"])
        result = ActivatedRepoManager.user_has_activated_repo(
            arm, "testuser", "evolution"
        )

        assert result is False
        arm.list_activated_repositories.assert_called_once_with("testuser")
