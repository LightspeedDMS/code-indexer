"""Bug #1770: regex_search cannot resolve a valid activated-repo alias.

On staging, `regex_search` on alias `e2e2-act` (a real, listed, user-activated
repository) returned ``Repository 'e2e2-act' not found``, while `search_code`
(semantic/FTS) and `list_repositories` both resolved the identical alias fine.

Root cause: ``handle_regex_search`` (search.py) resolves its single-repo alias
exclusively via the golden-repo-only legacy resolver
(``_get_legacy()._resolve_repo_path``), which has zero knowledge of
user-activated repositories -- it only understands golden-repo alias JSON,
the global registry, and golden-repos/.versioned directories. `search_code`
never hits this problem because ``_search_activated_repo`` hands the bare
alias straight to ``semantic_query_manager.query_user_repositories(...)``,
which resolves activated-repo aliases via ``ActivatedRepoManager`` internally.

The existing Story #1039 bare-to-global fallback in ``handle_regex_search``
is NOT the gap: it already checks ``_arm.user_has_activated_repo(...)`` and
correctly declines to promote when the user genuinely has the repo
activated. The bug is what happens *after* that check succeeds: the code
falls straight into the golden-repo-only resolver instead of resolving via
``ActivatedRepoManager.get_activated_repo_path`` -- the exact mechanism
already used by this same module's ``_resolve_temporal_repo_path`` and by
``_legacy._resolve_git_repo_path`` for git operations.

This test proves the fix: when the user genuinely has the alias activated,
``handle_regex_search`` must resolve the path via
``ActivatedRepoManager.get_activated_repo_path`` and must NOT depend on the
golden-repo-only legacy resolver at all.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole

_ARM_PATH = "code_indexer.server.mcp.handlers._utils.app_module.activated_repo_manager"
_GRM_PATH = "code_indexer.server.mcp.handlers._utils.app_module.golden_repo_manager"


def _make_user(username: str = "testuser") -> User:
    return User(
        username=username,
        role=UserRole.NORMAL_USER,
        password_hash="dummy",
        created_at=datetime.now(),
    )


def _json_result(handler_response: Dict[str, Any]) -> Dict[str, Any]:
    from typing import cast

    text = handler_response["content"][0]["text"]
    return cast(Dict[str, Any], json.loads(text))


class TestRegexSearchActivatedRepoAliasResolution:
    """handle_regex_search must resolve a genuine activated-repo alias
    via ActivatedRepoManager, exactly like search_code does -- never via
    the golden-repo-only legacy resolver.
    """

    def test_activated_repo_alias_resolves_without_golden_repo_lookup(self, tmp_path):
        """Bug #1770 repro: alias 'e2e2-act' IS activated for the user
        (user_has_activated_repo -> True). The golden-repo-only legacy
        resolver is stubbed to return None (simulating that it has no
        knowledge of activated repos, i.e. today's actual bug behavior).
        With the fix, the handler must resolve via
        ActivatedRepoManager.get_activated_repo_path instead and succeed.
        """
        from code_indexer.server.mcp.handlers.search import handle_regex_search

        user = _make_user()
        args = {"repository_alias": "e2e2-act", "pattern": "foo"}

        # Real activated-repo directory on disk (existence check in the fix).
        activated_repo_dir = tmp_path / "activated" / "testuser" / "e2e2-act"
        activated_repo_dir.mkdir(parents=True)

        arm = MagicMock()
        arm.user_has_activated_repo.return_value = True
        arm.get_activated_repo_path.return_value = str(activated_repo_dir)

        grm = MagicMock()
        grm.is_globally_active.return_value = False

        # Golden-repo-only legacy resolver: simulate its real-world behavior
        # of NOT knowing about activated repos -- returns None. If the
        # handler still routes through this, the call fails with
        # "not found", reproducing the actual bug.
        mock_legacy = MagicMock()
        mock_legacy._resolve_repo_path.return_value = None

        captured_path = {}

        async def _fake_execute_regex_search(args, path, alias, user):
            captured_path["path"] = path
            return (
                [],
                {
                    "reranker_used": False,
                    "reranker_provider": None,
                    "rerank_time_ms": 0,
                },
                MagicMock(
                    truncated=False,
                    read_capped=False,
                    search_engine="ripgrep",
                    search_time_ms=1,
                    total_matches=0,
                ),
            )

        with (
            patch(_ARM_PATH, arm),
            patch(_GRM_PATH, grm),
            patch(
                "code_indexer.server.mcp.handlers.search._get_legacy",
                return_value=mock_legacy,
            ),
            patch(
                "code_indexer.server.mcp.handlers.search._get_golden_repos_dir",
                return_value=str(tmp_path / "golden-repos"),
            ),
            patch(
                "code_indexer.server.mcp.handlers.search._execute_regex_search",
                side_effect=_fake_execute_regex_search,
            ),
        ):
            response = asyncio.get_event_loop().run_until_complete(
                handle_regex_search(args, user)
            )

        result = _json_result(response)

        assert result.get("success") is True, (
            f"regex_search failed to resolve a genuinely activated repo "
            f"alias: {result.get('error')!r}"
        )
        assert captured_path.get("path") == Path(str(activated_repo_dir)), (
            f"Expected the activated-repo path {activated_repo_dir!r} to be "
            f"used, got {captured_path.get('path')!r}"
        )
        # The alias was never promoted to -global (user genuinely has it
        # activated) -- no bare-to-global fallback should have fired.
        mock_legacy._resolve_repo_path.assert_not_called()
