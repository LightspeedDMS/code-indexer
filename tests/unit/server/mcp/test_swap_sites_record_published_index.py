"""Every swap to a snapshot ``RefreshScheduler._create_snapshot`` built carries
``published_swap_extra`` -- the published index version under a delegated
IndexStore -- so no swap site moves the pointer to a path other pods cannot
check out. And golden-repo path resolution for readers checks the index out
(``AliasManager.ensure_served_here``)."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

from code_indexer.server.mcp.handlers import repos

EXTRA: Dict[str, Any] = {"extra": {"index_store_alias": "r", "index_version": "v9"}}


def test_the_provider_index_swap_records_the_published_index() -> None:
    scheduler = MagicMock()
    scheduler._create_snapshot.return_value = "/g/.versioned/r/v_2"
    scheduler.published_swap_extra.return_value = EXTRA
    with patch.object(repos, "_get_app_refresh_scheduler", return_value=scheduler):
        repos._post_provider_index_snapshot("r-global", "/g/r", "/g/.versioned/r/v_1")
    scheduler.published_swap_extra.assert_called_once_with(
        "r-global", "/g/.versioned/r/v_2"
    )
    scheduler.alias_manager.swap_alias.assert_called_once_with(
        alias_name="r-global",
        new_target="/g/.versioned/r/v_2",
        old_target="/g/.versioned/r/v_1",
        **EXTRA,
    )


def test_golden_path_resolution_reads_for_serving(tmp_path: Any) -> None:
    manager = MagicMock()
    manager.read_alias.side_effect = lambda a: "/served" if a == "r-global" else None
    with (
        patch.object(repos, "_get_golden_repos_dir", return_value=str(tmp_path)),
        patch(
            "code_indexer.server.mcp.handlers.repos.AliasManager",
            return_value=manager,
        ),
    ):
        assert repos._resolve_golden_repo_path("r") == "/served"
    manager.ensure_served_here.assert_called_once_with("r-global", "/served")


def test_an_unresolved_golden_path_serves_nothing(tmp_path: Any) -> None:
    manager = MagicMock()
    manager.read_alias.return_value = None
    with (
        patch.object(repos, "_get_golden_repos_dir", return_value=str(tmp_path)),
        patch(
            "code_indexer.server.mcp.handlers.repos.AliasManager",
            return_value=manager,
        ),
    ):
        assert repos._resolve_golden_repo_path("r") is None
    manager.ensure_served_here.assert_not_called()


def test_a_global_semantic_query_serves_the_target_before_searching(
    tmp_path: Any,
) -> None:
    from code_indexer.server.query.semantic_query_manager import (
        SemanticQueryManager,
    )

    activated = tmp_path / "data" / "activated-repos"
    activated.mkdir(parents=True)
    repo_manager = MagicMock()
    repo_manager.activated_repos_dir = str(activated)
    manager = SemanticQueryManager(
        data_dir=str(tmp_path / "data"),
        activated_repo_manager=repo_manager,
        background_job_manager=MagicMock(),
    )
    aliases = MagicMock()
    aliases.read_alias.return_value = "/g/.versioned/r/v_2"
    order: list = []
    aliases.ensure_served_here.side_effect = lambda a, t: order.append(("serve", a, t))
    with (
        patch(
            "code_indexer.global_repos.alias_manager.AliasManager",
            return_value=aliases,
        ),
        patch.object(
            manager,
            "_search_single_repository",
            side_effect=lambda *a, **k: order.append(("search",)) or [],
        ),
    ):
        manager._perform_search(
            username="u",
            user_repos=[{"user_alias": "r-global", "is_global": True}],
            query_text="q",
            limit=5,
            min_score=None,
            file_extensions=None,
        )
    assert order[0] == ("serve", "r-global", "/g/.versioned/r/v_2")
    assert ("search",) in order
