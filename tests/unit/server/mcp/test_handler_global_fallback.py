"""Per-handler bare-to-global alias fallback tests (Story #1039).

For each Section A handler: a bare alias that the user does NOT have activated
but IS globally active must be silently rewritten to ``"<alias>-global"`` before
the handler routes further.

Strategy
--------
Each test:
1. Patches ``_utils.app_module.activated_repo_manager`` so that
   ``user_has_activated_repo("testuser", "evolution")`` returns ``False``.
2. Patches ``_utils.app_module.golden_repo_manager`` so that
   ``is_globally_active("evolution")`` returns ``True``.
3. Patches the downstream routing/service call so the handler does not actually
   try to open files on disk.
4. Calls the handler with ``repository_alias="evolution"`` (bare alias).
5. Asserts the handler did NOT return a "not found" error -- i.e. the fallback
   was applied and routing continued with ``"evolution-global"``.

Section B regression tests confirm that write/mutation handlers do NOT apply the
fallback (they must continue to return an appropriate error for bare aliases).
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from datetime import datetime

from code_indexer.server.auth.user_manager import User, UserRole

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_user(username: str = "testuser") -> User:
    return User(
        username=username,
        role=UserRole.NORMAL_USER,
        password_hash="dummy",
        created_at=datetime.now(),
    )


def _json_result(handler_response: Dict[str, Any]) -> Dict[str, Any]:
    """Decode the MCP content wrapper to the inner JSON dict."""
    from typing import cast

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


# ---------------------------------------------------------------------------
# Helper: patch app_module managers for read-only handlers
# ---------------------------------------------------------------------------

_ARM_PATH = "code_indexer.server.mcp.handlers._utils.app_module.activated_repo_manager"
_GRM_PATH = "code_indexer.server.mcp.handlers._utils.app_module.golden_repo_manager"


# ===========================================================================
# Section A – Search handlers
# ===========================================================================


class TestSearchCodeFallback:
    """search_code dispatcher applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self):
        """search_code with bare alias + global active -> routes to _search_global_repo."""
        from code_indexer.server.mcp.handlers.search import search_code

        user = _make_user()
        params = {"repository_alias": "evolution", "query_text": "some query"}

        captured = {}

        def _fake_search_global(p, u, repo_alias):
            captured["alias"] = repo_alias
            return {
                "content": [{"type": "text", "text": '{"success":true,"results":[]}'}]
            }

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(
                "code_indexer.server.mcp.handlers.search._search_global_repo",
                side_effect=_fake_search_global,
            ),
        ):
            search_code(params, user)

        # Fallback should have routed to _search_global_repo with evolution-global
        assert captured.get("alias") == "evolution-global", (
            f"Expected routing to 'evolution-global', got {captured.get('alias')!r}"
        )


class TestHandleRegexSearchFallback:
    """handle_regex_search applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self):
        """handle_regex_search bare alias + global active -> _resolve_repo_path called with -global."""
        import asyncio
        from code_indexer.server.mcp.handlers.search import handle_regex_search

        user = _make_user()
        args = {"repository_alias": "evolution", "pattern": "foo"}

        captured_alias = {}

        def _fake_resolve(alias, repos_dir):
            captured_alias["alias"] = alias
            return "/fake/path"

        mock_leg = MagicMock()
        mock_leg._resolve_repo_path.side_effect = _fake_resolve

        async def _fake_execute(args, path, alias, user):
            return (
                [],
                {
                    "reranker_used": False,
                    "reranker_provider": None,
                    "rerank_time_ms": 0,
                },
                MagicMock(truncated=False, search_engine="regex", search_time_ms=1),
            )

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(
                "code_indexer.server.mcp.handlers.search._get_legacy",
                return_value=mock_leg,
            ),
            patch(
                "code_indexer.server.mcp.handlers.search._execute_regex_search",
                side_effect=_fake_execute,
            ),
            patch(
                "code_indexer.server.mcp.handlers.search._get_golden_repos_dir",
                return_value="/fake/golden",
            ),
        ):
            asyncio.get_event_loop().run_until_complete(handle_regex_search(args, user))

        assert captured_alias.get("alias") == "evolution-global", (
            f"Expected 'evolution-global', got {captured_alias.get('alias')!r}"
        )


# ===========================================================================
# Section A – SCIP handlers
# ===========================================================================


class TestScipDefinitionFallback:
    """scip_definition applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self):
        """scip_definition bare alias -> repository_alias rewritten before service call."""
        from code_indexer.server.mcp.handlers.scip import scip_definition

        user = _make_user()
        params = {"symbol": "MyClass", "repository_alias": "evolution"}

        captured = {}

        def _fake_service_find_definition(**kwargs):
            captured["repository_alias"] = kwargs.get("repository_alias")
            return []

        mock_service = MagicMock()
        mock_service.find_definition.side_effect = (
            lambda **kw: captured.update(
                {"repository_alias": kw.get("repository_alias")}
            )
            or []
        )

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(
                "code_indexer.server.mcp.handlers.scip._get_scip_query_service",
                return_value=mock_service,
            ),
            patch(
                "code_indexer.server.mcp.handlers.scip._apply_scip_payload_truncation",
                side_effect=lambda x: x,
            ),
        ):
            result = scip_definition(params, user)

        data = _json_result(result)
        assert data.get("success") is True
        assert captured.get("repository_alias") == "evolution-global", (
            f"Expected 'evolution-global', got {captured.get('repository_alias')!r}"
        )


class TestScipReferencesFallback:
    """scip_references applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self):
        """scip_references bare alias -> repository_alias rewritten before service call."""
        from code_indexer.server.mcp.handlers.scip import scip_references

        user = _make_user()
        params = {"symbol": "MyClass", "repository_alias": "evolution"}

        captured = {}
        mock_service = MagicMock()
        mock_service.find_references.side_effect = (
            lambda **kw: captured.update(
                {"repository_alias": kw.get("repository_alias")}
            )
            or []
        )

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(
                "code_indexer.server.mcp.handlers.scip._get_scip_query_service",
                return_value=mock_service,
            ),
            patch(
                "code_indexer.server.mcp.handlers.scip._apply_scip_payload_truncation",
                side_effect=lambda x: x,
            ),
        ):
            result = scip_references(params, user)

        data = _json_result(result)
        assert data.get("success") is True
        assert captured.get("repository_alias") == "evolution-global", (
            f"Expected 'evolution-global', got {captured.get('repository_alias')!r}"
        )


class TestScipDependenciesFallback:
    """scip_dependencies applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self):
        """scip_dependencies bare alias -> repository_alias rewritten before service call."""
        from code_indexer.server.mcp.handlers.scip import scip_dependencies

        user = _make_user()
        params = {"symbol": "MyClass", "repository_alias": "evolution"}

        captured = {}
        mock_service = MagicMock()
        mock_service.get_dependencies.side_effect = (
            lambda **kw: captured.update(
                {"repository_alias": kw.get("repository_alias")}
            )
            or []
        )

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(
                "code_indexer.server.mcp.handlers.scip._get_scip_query_service",
                return_value=mock_service,
            ),
            patch(
                "code_indexer.server.mcp.handlers.scip._apply_scip_payload_truncation",
                side_effect=lambda x: x,
            ),
        ):
            result = scip_dependencies(params, user)

        data = _json_result(result)
        assert data.get("success") is True
        assert captured.get("repository_alias") == "evolution-global", (
            f"Expected 'evolution-global', got {captured.get('repository_alias')!r}"
        )


class TestScipDependentsFallback:
    """scip_dependents applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self):
        """scip_dependents bare alias -> repository_alias rewritten before service call."""
        from code_indexer.server.mcp.handlers.scip import scip_dependents

        user = _make_user()
        params = {"symbol": "MyClass", "repository_alias": "evolution"}

        captured = {}
        mock_service = MagicMock()
        mock_service.get_dependents.side_effect = (
            lambda **kw: captured.update(
                {"repository_alias": kw.get("repository_alias")}
            )
            or []
        )

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(
                "code_indexer.server.mcp.handlers.scip._get_scip_query_service",
                return_value=mock_service,
            ),
            patch(
                "code_indexer.server.mcp.handlers.scip._apply_scip_payload_truncation",
                side_effect=lambda x: x,
            ),
        ):
            result = scip_dependents(params, user)

        data = _json_result(result)
        assert data.get("success") is True
        assert captured.get("repository_alias") == "evolution-global", (
            f"Expected 'evolution-global', got {captured.get('repository_alias')!r}"
        )


class TestScipImpactFallback:
    """scip_impact applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self):
        """scip_impact bare alias -> repository_alias rewritten before service call."""
        from code_indexer.server.mcp.handlers.scip import scip_impact

        user = _make_user()
        params = {"symbol": "MyClass", "repository_alias": "evolution"}

        captured = {}

        def _fake_analyze_impact(**kw):
            captured["repository_alias"] = kw.get("repository_alias")
            return {"affected_symbols": [], "affected_files": []}

        mock_service = MagicMock()
        mock_service.analyze_impact.side_effect = _fake_analyze_impact

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(
                "code_indexer.server.mcp.handlers.scip._get_scip_query_service",
                return_value=mock_service,
            ),
        ):
            scip_impact(params, user)

        assert captured.get("repository_alias") == "evolution-global", (
            f"Expected 'evolution-global', got {captured.get('repository_alias')!r}"
        )


class TestScipCallchainFallback:
    """scip_callchain applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self):
        """scip_callchain bare alias -> repository_alias rewritten before service call."""
        from code_indexer.server.mcp.handlers.scip import scip_callchain

        user = _make_user()
        params = {
            "from_symbol": "A.method",
            "to_symbol": "B.method",
            "repository_alias": "evolution",
        }

        captured = {}

        def _fake_trace_callchain(**kw):
            captured["repository_alias"] = kw.get("repository_alias")
            return []

        mock_service = MagicMock()
        mock_service.trace_callchain.side_effect = _fake_trace_callchain

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(
                "code_indexer.server.mcp.handlers.scip._get_scip_query_service",
                return_value=mock_service,
            ),
        ):
            scip_callchain(params, user)

        assert captured.get("repository_alias") == "evolution-global", (
            f"Expected 'evolution-global', got {captured.get('repository_alias')!r}"
        )


class TestScipContextFallback:
    """scip_context applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self):
        """scip_context bare alias -> repository_alias rewritten before service call."""
        from code_indexer.server.mcp.handlers.scip import scip_context

        user = _make_user()
        params = {"symbol": "MyClass", "repository_alias": "evolution"}

        captured = {}

        def _fake_get_context(**kw):
            captured["repository_alias"] = kw.get("repository_alias")
            return {"files": [], "total_files": 0}

        mock_service = MagicMock()
        mock_service.get_context.side_effect = _fake_get_context

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(
                "code_indexer.server.mcp.handlers.scip._get_scip_query_service",
                return_value=mock_service,
            ),
        ):
            scip_context(params, user)

        assert captured.get("repository_alias") == "evolution-global", (
            f"Expected 'evolution-global', got {captured.get('repository_alias')!r}"
        )


# ===========================================================================
# Section A – Git read handlers
# ===========================================================================

_RESOLVE_GIT_PATH = "code_indexer.server.mcp.handlers.git_read._get_legacy"


class TestGitLogFallback:
    """git_log applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self, tmp_path):
        """git_log bare alias + globally active -> resolved using -global form."""
        from code_indexer.server.mcp.handlers.git_read import git_log

        # Create a fake git repo
        repo_dir = tmp_path / "evolution-global"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        user = _make_user()
        args = {"repository_alias": "evolution"}

        captured_alias = {}

        def _fake_resolve(alias, username):
            captured_alias["alias"] = alias
            return str(repo_dir), None

        mock_leg = MagicMock()
        mock_leg._resolve_git_repo_path.side_effect = _fake_resolve

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(_RESOLVE_GIT_PATH, return_value=mock_leg),
            patch(
                "code_indexer.server.mcp.handlers.git_read.git_operations_service"
            ) as mock_ops,
        ):
            mock_ops.git_log.return_value = {"commits": [], "total_count": 0}
            git_log(args, user)

        assert captured_alias.get("alias") == "evolution-global", (
            f"Expected 'evolution-global', got {captured_alias.get('alias')!r}"
        )


class TestGitBlameFallback:
    """handle_git_blame applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self, tmp_path):
        """handle_git_blame bare alias + globally active -> resolved using -global form."""
        from code_indexer.server.mcp.handlers.git_read import handle_git_blame

        repo_dir = tmp_path / "evolution-global"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        user = _make_user()
        args = {"repository_alias": "evolution", "path": "src/main.py"}

        captured_alias = {}

        def _fake_resolve(alias, username):
            captured_alias["alias"] = alias
            return str(repo_dir), None

        mock_leg = MagicMock()
        mock_leg._resolve_git_repo_path.side_effect = _fake_resolve

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(_RESOLVE_GIT_PATH, return_value=mock_leg),
            patch(
                "code_indexer.server.mcp.handlers.git_read.git_operations_service"
            ) as mock_ops,
        ):
            mock_ops.git_blame.return_value = {"lines": []}
            handle_git_blame(args, user)

        assert captured_alias.get("alias") == "evolution-global", (
            f"Expected 'evolution-global', got {captured_alias.get('alias')!r}"
        )


class TestGitStatusFallback:
    """git_status applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self, tmp_path):
        """git_status bare alias + globally active -> resolved using -global form."""
        from code_indexer.server.mcp.handlers.git_read import git_status

        repo_dir = tmp_path / "evolution-global"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        user = _make_user()
        args = {"repository_alias": "evolution"}

        captured_alias = {}

        def _fake_resolve(alias, username):
            captured_alias["alias"] = alias
            return str(repo_dir), None

        mock_leg = MagicMock()
        mock_leg._resolve_git_repo_path.side_effect = _fake_resolve

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(_RESOLVE_GIT_PATH, return_value=mock_leg),
            patch(
                "code_indexer.server.mcp.handlers.git_read.git_operations_service"
            ) as mock_ops,
        ):
            mock_ops.git_status.return_value = {
                "staged": [],
                "unstaged": [],
                "untracked": [],
            }
            git_status(args, user)

        assert captured_alias.get("alias") == "evolution-global", (
            f"Expected 'evolution-global', got {captured_alias.get('alias')!r}"
        )


# ===========================================================================
# Section A – repos.py get_branches
# ===========================================================================


class TestGetBranchesFallback:
    """get_branches applies bare-to-global fallback."""

    def test_bare_alias_rewritten_to_global(self):
        """get_branches bare alias + globally active -> fallback applied."""
        from code_indexer.server.mcp.handlers.repos import get_branches

        user = _make_user()
        params = {"repository_alias": "evolution"}

        captured = {}

        def _fake_resolve(alias, u):
            captured["alias"] = alias
            return "/fake/path", None

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
            patch(
                "code_indexer.server.mcp.handlers.repos._resolve_branch_repo_path",
                side_effect=_fake_resolve,
            ),
            patch("code_indexer.services.git_topology_service.GitTopologyService"),
            patch(
                "code_indexer.server.services.branch_service.BranchService"
            ) as mock_bs,
        ):
            mock_bs.return_value.__enter__.return_value.list_branches.return_value = []
            get_branches(params, user)

        assert captured.get("alias") == "evolution-global", (
            f"Expected 'evolution-global', got {captured.get('alias')!r}"
        )


# ===========================================================================
# Section B regression: write handlers must NOT apply fallback
# ===========================================================================


class TestSectionBHandlersStayStrict:
    """Section B handlers must NOT silently fall back on bare aliases."""

    def test_create_file_no_fallback(self):
        """handle_create_file returns error for bare alias -- no fallback applied."""
        from code_indexer.server.mcp.handlers.files import handle_create_file

        user = _make_user()
        params = {
            "repository_alias": "evolution",
            "path": "test.txt",
            "content": "hello",
        }

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
        ):
            result = handle_create_file(params, user)

        data = _json_result(result)
        # handle_create_file must return an error, not silently use -global form
        assert data.get("success") is False or "error" in data, (
            "handle_create_file must NOT silently apply global fallback"
        )

    def test_edit_file_no_fallback(self):
        """handle_edit_file returns error for bare alias -- no fallback applied."""
        from code_indexer.server.mcp.handlers.files import handle_edit_file

        user = _make_user()
        params = {
            "repository_alias": "evolution",
            "path": "test.txt",
            "old_content": "old",
            "new_content": "new",
        }

        with (
            patch(_ARM_PATH, _make_arm(has_repo=False)),
            patch(_GRM_PATH, _make_grm(globally_active=True)),
        ):
            result = handle_edit_file(params, user)

        data = _json_result(result)
        assert data.get("success") is False or "error" in data, (
            "handle_edit_file must NOT silently apply global fallback"
        )
