"""get_file_content bare-to-global alias fallback + graceful not-found tests.

Staging bug: a bare (non-``-global``) alias passed to ``get_file_content`` threw a
server-side ERROR + traceback (``[CACHE-GENERAL-011]`` / "Unexpected error in
get_file_content") instead of resolving via the Story #1039 global fallback the
way ``search_code`` does.

Root cause: when the user has a STALE/BROKEN own-activation record (present in
metadata, but the on-disk dir is gone), ``user_has_activated_repo`` returns True,
the Story #1039 fallback is correctly skipped (activated-repo precedence), the
own-activation path resolution raises ``FileNotFoundError``, and that escalates
to the unhandled "Unexpected error" traceback.

These tests assert ``get_file_content`` behaves CONSISTENTLY with ``search_code``:

* Bare alias NOT activated + globally active -> resolves to ``-global``, content.
* Bare alias with a BROKEN own-activation + globally active -> recovers via
  ``-global`` (own-activation resolution failed; global recovery applies).
* Bare alias genuinely absent everywhere -> clean not-found MCP error, NO
  exception/traceback, NO success.
* Bare alias with a WORKING own-activation -> own repo wins (precedence).
* Explicit ``-global`` alias -> unchanged (routes to the global path).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_ARM_PATH = "code_indexer.server.mcp.handlers._utils.app_module.activated_repo_manager"
_GRM_PATH = "code_indexer.server.mcp.handlers._utils.app_module.golden_repo_manager"
_FILE_SVC_PATH = "code_indexer.server.mcp.handlers._utils.app_module.file_service"


def _make_user(username: str = "testuser") -> User:
    return User(
        username=username,
        role=UserRole.NORMAL_USER,
        password_hash="dummy",
        created_at=datetime.now(),
    )


def _json_result(handler_response: Dict[str, Any]) -> Dict[str, Any]:
    text = handler_response["content"][0]["text"]
    return cast(Dict[str, Any], json.loads(text))


def _make_arm(has_repo: bool = False) -> MagicMock:
    arm = MagicMock()
    arm.user_has_activated_repo.return_value = has_repo
    arm.list_activated_repositories.return_value = []
    return arm


def _make_grm(globally_active: bool = True) -> MagicMock:
    grm = MagicMock()
    grm.is_globally_active.return_value = globally_active
    return grm


_OK_RESULT = {
    "content": "hello world\n",
    "metadata": {"offset": 1, "total_lines": 1},
}


# ===========================================================================
# 1. Bare alias NOT activated + globally active -> resolves to -global
# ===========================================================================


class TestBareAliasNotActivatedFallsBackToGlobal:
    def test_resolves_to_global_and_returns_content(self):
        from code_indexer.server.mcp.handlers.files import get_file_content

        user = _make_user()
        params = {"repository_alias": "evolution", "file_path": "src/main.py"}

        captured: Dict[str, Any] = {}

        def _fake_resolve_global(alias, u, extra):
            captured["alias"] = alias
            return "/fake/global/path", None

        file_svc = MagicMock()

        def _fake_by_path(**kwargs):
            captured["by_path_repo"] = kwargs.get("repo_path")
            return dict(_OK_RESULT)

        file_svc.get_file_content_by_path.side_effect = _fake_by_path

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(_FILE_SVC_PATH, file_svc),
            patch(
                "code_indexer.server.mcp.handlers.files._resolve_global_repo_target",
                side_effect=_fake_resolve_global,
            ),
        ):
            result = get_file_content(params, user)

        data = _json_result(result)
        assert data.get("success") is True, data
        # Must have routed through the -global path.
        assert captured.get("alias") == "evolution-global", captured
        assert captured.get("by_path_repo") == "/fake/global/path", captured
        # The own-activation path must NOT have been used.
        file_svc.get_file_content.assert_not_called()


# ===========================================================================
# 2. Broken own-activation + globally active -> recovers via -global
# ===========================================================================


class TestBrokenOwnActivationRecoversViaGlobal:
    def test_filenotfound_on_own_repo_recovers_to_global(self):
        """Stale own-activation: record present, disk gone -> recover via -global."""
        from code_indexer.server.mcp.handlers.files import get_file_content

        user = _make_user()
        params = {"repository_alias": "fastapi", "file_path": "src/main.py"}

        captured: Dict[str, Any] = {}

        # User HAS an activation record for "fastapi" (stale/broken).
        arm = _make_arm(has_repo=True)

        file_svc = MagicMock()

        def _fake_own(**kwargs):
            # Own-activation path resolution fails (disk dir missing).
            raise FileNotFoundError(
                "Repository 'fastapi' not found for user 'testuser'"
            )

        file_svc.get_file_content.side_effect = _fake_own

        def _fake_by_path(**kwargs):
            captured["by_path_repo"] = kwargs.get("repo_path")
            return dict(_OK_RESULT)

        file_svc.get_file_content_by_path.side_effect = _fake_by_path

        def _fake_resolve_global(alias, u, extra):
            captured["alias"] = alias
            return "/fake/global/path", None

        with (
            patch(_ARM_PATH, arm),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(_FILE_SVC_PATH, file_svc),
            patch(
                "code_indexer.server.mcp.handlers.files._resolve_global_repo_target",
                side_effect=_fake_resolve_global,
            ),
        ):
            result = get_file_content(params, user)

        data = _json_result(result)
        assert data.get("success") is True, data
        assert captured.get("alias") == "fastapi-global", captured
        assert captured.get("by_path_repo") == "/fake/global/path", captured


# ===========================================================================
# 3. Bare alias genuinely absent everywhere -> clean not-found, no traceback
# ===========================================================================


class TestGenuineNotFoundIsCleanError:
    def test_not_found_everywhere_returns_clean_error_no_exception(self):
        from code_indexer.server.mcp.handlers.files import get_file_content

        user = _make_user()
        params = {"repository_alias": "ghost", "file_path": "src/main.py"}

        # No own-activation; NOT globally active.
        file_svc = MagicMock()
        file_svc.get_file_content.side_effect = FileNotFoundError(
            "Repository 'ghost' not found for user 'testuser'"
        )

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=False)),
            patch(_FILE_SVC_PATH, file_svc),
            patch("code_indexer.server.mcp.handlers.files.logger") as mock_logger,
        ):
            result = get_file_content(params, user)

        data = _json_result(result)
        assert data.get("success") is False, data
        assert "error" in data and data["error"], data
        # The expected not-found must NOT escalate as an unhandled exception:
        # no logger.exception("Unexpected error ...") traceback spam.
        mock_logger.exception.assert_not_called()

    def test_broken_own_activation_not_globally_active_clean_error(self):
        """Stale own-activation, NOT globally active -> clean not-found, no traceback."""
        from code_indexer.server.mcp.handlers.files import get_file_content

        user = _make_user()
        params = {"repository_alias": "fastapi", "file_path": "src/main.py"}

        arm = _make_arm(has_repo=True)
        file_svc = MagicMock()
        file_svc.get_file_content.side_effect = FileNotFoundError(
            "Repository 'fastapi' not found for user 'testuser'"
        )

        with (
            patch(_ARM_PATH, arm),
            patch(_GRM_PATH, _make_grm(globally_active=False)),
            patch(_FILE_SVC_PATH, file_svc),
            patch("code_indexer.server.mcp.handlers.files.logger") as mock_logger,
        ):
            result = get_file_content(params, user)

        data = _json_result(result)
        assert data.get("success") is False, data
        mock_logger.exception.assert_not_called()


# ===========================================================================
# 4. Working own-activation -> own repo wins (precedence preserved)
# ===========================================================================


class TestWorkingOwnActivationWins:
    def test_own_activation_takes_precedence(self):
        from code_indexer.server.mcp.handlers.files import get_file_content

        user = _make_user()
        params = {"repository_alias": "fastapi", "file_path": "src/main.py"}

        captured: Dict[str, Any] = {}
        arm = _make_arm(has_repo=True)

        file_svc = MagicMock()

        def _fake_own(**kwargs):
            captured["own_alias"] = kwargs.get("repository_alias")
            return dict(_OK_RESULT)

        file_svc.get_file_content.side_effect = _fake_own

        with (
            patch(_ARM_PATH, arm),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(_FILE_SVC_PATH, file_svc),
        ):
            result = get_file_content(params, user)

        data = _json_result(result)
        assert data.get("success") is True, data
        # Own activation used, NOT the -global form.
        assert captured.get("own_alias") == "fastapi", captured
        # Global recovery path must NOT have been used.
        file_svc.get_file_content_by_path.assert_not_called()


# ===========================================================================
# 5. Explicit -global alias -> unchanged (routes to global path)
# ===========================================================================


class TestExplicitGlobalUnchanged:
    def test_explicit_global_routes_to_global_path(self):
        from code_indexer.server.mcp.handlers.files import get_file_content

        user = _make_user()
        params = {"repository_alias": "evolution-global", "file_path": "src/main.py"}

        captured: Dict[str, Any] = {}

        def _fake_resolve_global(alias, u, extra):
            captured["alias"] = alias
            return "/fake/global/path", None

        file_svc = MagicMock()
        file_svc.get_file_content_by_path.side_effect = lambda **kw: dict(_OK_RESULT)

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(_FILE_SVC_PATH, file_svc),
            patch(
                "code_indexer.server.mcp.handlers.files._resolve_global_repo_target",
                side_effect=_fake_resolve_global,
            ),
        ):
            result = get_file_content(params, user)

        data = _json_result(result)
        assert data.get("success") is True, data
        assert captured.get("alias") == "evolution-global", captured
        file_svc.get_file_content.assert_not_called()


# ===========================================================================
# 6. _recover_file_content_via_global defensive guard branches
# ===========================================================================


class TestRecoverHelperGuards:
    """Direct tests of the recovery helper's None-return guard branches."""

    def test_returns_none_when_golden_repo_manager_absent(self):
        from code_indexer.server.mcp.handlers.files import (
            _recover_file_content_via_global,
        )

        user = _make_user()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module.golden_repo_manager",
            None,
        ):
            out = _recover_file_content_via_global(
                repository_alias="fastapi",
                file_path="src/main.py",
                user=user,
                offset=None,
                limit=None,
            )
        assert out is None

    def test_returns_none_when_global_resolution_errors(self):
        from code_indexer.server.mcp.handlers.files import (
            _recover_file_content_via_global,
        )

        user = _make_user()

        def _fake_resolve_global(alias, u, extra):
            # Globally active, but the alias/versioned path fails to resolve.
            return None, _mcp_error_response()

        with (
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(
                "code_indexer.server.mcp.handlers.files._resolve_global_repo_target",
                side_effect=_fake_resolve_global,
            ),
        ):
            out = _recover_file_content_via_global(
                repository_alias="fastapi",
                file_path="src/main.py",
                user=user,
                offset=None,
                limit=None,
            )
        assert out is None

    def test_returns_none_for_already_global_alias(self):
        from code_indexer.server.mcp.handlers.files import (
            _recover_file_content_via_global,
        )

        user = _make_user()
        out = _recover_file_content_via_global(
            repository_alias="fastapi-global",
            file_path="src/main.py",
            user=user,
            offset=None,
            limit=None,
        )
        assert out is None


def _mcp_error_response() -> Dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps({"success": False, "error": "nope"})}
        ]
    }
