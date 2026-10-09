"""Bulk provider-index add parses its filter strictly (public Bug #2038).

The only supported filter is ``category:<name>``.  An unsupported filter, or
an empty category, is refused at the shared service before any config write
or job submission, on BOTH front doors (REST and MCP).  The category name is
matched EXACTLY (case-sensitive), the way category names are stored (UNIQUE
TEXT, so ``Backend`` and ``backend`` can coexist) and the way
``list_repositories`` filters by category; the ``category:`` prefix itself is
case-insensitive.

Driven through the real REST and MCP doors of ``front_door_env`` with a real
GoldenRepoManager, a real RepoCategoryService and real SQLite databases.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from _audit_front_doors import DoorsEnv, front_door_env
from _audit_repos_support import EXAMPLE_ALIAS

_PROVIDER = "voyage-ai"
_BULK_PATH = "/api/admin/provider-indexes/bulk-add"
_CATEGORY = "abc"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[DoorsEnv]:
    yield from front_door_env(tmp_path, monkeypatch)


@pytest.fixture()
def categorized_env(env: DoorsEnv) -> DoorsEnv:
    """``example-repo`` is assigned to the real category ``abc``."""
    from code_indexer.server.services.repo_category_service import (
        RepoCategoryService,
    )
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(env.manager.db_path).initialize_database()
    service = RepoCategoryService(env.manager.db_path)
    category_id = service.create_category(_CATEGORY, "^never-auto-matches$")
    service.update_repo_category(EXAMPLE_ALIAS, category_id)
    env.manager._repo_category_service = service
    return env


def _base_clone_config(env: DoorsEnv) -> str:
    path = Path(env.manager.golden_repos_dir) / EXAMPLE_ALIAS / ".code-indexer"
    return str((path / "config.json").read_text())


def _rest(env: DoorsEnv, filter_value: Any):
    return env.rest(
        "POST", _BULK_PATH, json={"provider": _PROVIDER, "filter": filter_value}
    )


def _mcp(env: DoorsEnv, filter_value: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = env.mcp(
        "bulk_add_provider_index", {"provider": _PROVIDER, "filter": filter_value}
    )
    return result


_UNSUPPORTED = ["categry:abc", "tag:abc", "abc", "  ", "category"]
_EMPTY_CATEGORY = ["category:", "category:   ", "CATEGORY:"]


class TestUnsupportedFilterIsRefused:
    @pytest.mark.parametrize("filter_value", _UNSUPPORTED + _EMPTY_CATEGORY)
    def test_rest_returns_400_and_creates_no_job(
        self, categorized_env: DoorsEnv, filter_value: str
    ) -> None:
        before = _base_clone_config(categorized_env)
        resp = _rest(categorized_env, filter_value)
        assert resp.status_code == 400, resp.text
        assert "filter" in resp.json()["detail"].lower()
        assert categorized_env.jobs.submissions == []
        assert _base_clone_config(categorized_env) == before
        row = categorized_env.only_row("provider_index_bulk_added")
        assert row.outcome == "failure"

    @pytest.mark.parametrize("filter_value", _UNSUPPORTED + _EMPTY_CATEGORY)
    def test_mcp_returns_error_and_creates_no_job(
        self, categorized_env: DoorsEnv, filter_value: str
    ) -> None:
        before = _base_clone_config(categorized_env)
        result = _mcp(categorized_env, filter_value)
        assert result["success"] is False, result
        assert "filter" in result["error"].lower()
        assert categorized_env.jobs.submissions == []
        assert _base_clone_config(categorized_env) == before


class TestCategoryIsMatchedExactly:
    @pytest.mark.parametrize("filter_value", ["category:abc", "Category:abc"])
    def test_exact_category_selects_the_repo_on_rest(
        self, categorized_env: DoorsEnv, filter_value: str
    ) -> None:
        resp = _rest(categorized_env, filter_value)
        assert resp.status_code == 202, resp.text
        assert [j["alias"] for j in resp.json()["jobs"]] == [f"{EXAMPLE_ALIAS}-global"]
        assert len(categorized_env.jobs.submissions) == 1

    def test_exact_category_selects_the_repo_on_mcp(
        self, categorized_env: DoorsEnv
    ) -> None:
        result = _mcp(categorized_env, "category:abc")
        assert result["success"] is True, result
        assert result["jobs_created"] == 1

    @pytest.mark.parametrize("filter_value", ["category:a", "category:ABC"])
    def test_only_the_exact_category_name_matches(
        self, categorized_env: DoorsEnv, filter_value: str
    ) -> None:
        before = _base_clone_config(categorized_env)
        resp = _rest(categorized_env, filter_value)
        assert resp.status_code == 202, resp.text
        assert resp.json()["jobs_created"] == 0
        assert categorized_env.jobs.submissions == []
        assert _base_clone_config(categorized_env) == before
        # The exact name DOES select the repository in the same setup.
        exact = _rest(categorized_env, f"category:{_CATEGORY}")
        assert exact.status_code == 202, exact.text
        assert [j["alias"] for j in exact.json()["jobs"]] == [f"{EXAMPLE_ALIAS}-global"]


class TestNoFilterStillTargetsEveryRepo:
    @pytest.mark.parametrize("filter_value", [None, ""])
    def test_absent_filter_selects_all(
        self, categorized_env: DoorsEnv, filter_value: Any
    ) -> None:
        resp = _rest(categorized_env, filter_value)
        assert resp.status_code == 202, resp.text
        assert resp.json()["jobs_created"] == 1


class TestCategoryServiceUnavailable:
    def test_category_filter_is_refused_without_a_category_service(
        self, env: DoorsEnv
    ) -> None:
        env.manager._repo_category_service = None
        resp = _rest(env, "category:abc")
        assert resp.status_code == 503, resp.text
        assert env.jobs.submissions == []
        result = _mcp(env, "category:abc")
        assert result["success"] is False, result
        assert env.jobs.submissions == []

    def test_category_lookup_failure_is_refused_on_both_doors(
        self, env: DoorsEnv
    ) -> None:
        """A wired service whose map query fails (here: a real SQLite error,
        the category schema is missing) gives the same clean refusal."""
        from code_indexer.server.services.repo_category_service import (
            RepoCategoryService,
        )

        env.manager._repo_category_service = RepoCategoryService(env.manager.db_path)
        before = _base_clone_config(env)

        resp = _rest(env, "category:abc")
        assert resp.status_code == 503, resp.text
        assert "no such table" not in resp.text
        result = _mcp(env, "category:abc")
        assert result["success"] is False, result
        assert "no such table" not in result["error"]
        assert env.jobs.submissions == []
        assert _base_clone_config(env) == before
