"""Handler-level coverage for the published `<alias>-global` fallbacks.

Guards three things the resolver's own unit tests cannot: that the fallback is
reached, that an activated repo still wins, and that access is enforced before the
golden clone is handed out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from code_indexer.server.mcp.handlers import _global_clone


class _Repo:
    def __init__(self, clone_path: str) -> None:
        self.clone_path = clone_path


class _GRM:
    def __init__(self, repo: Optional[_Repo]) -> None:
        self._repo = repo

    def get_golden_repo(self, alias: str) -> Optional[_Repo]:
        return self._repo


def _patch_registry(monkeypatch: pytest.MonkeyPatch, known: Optional[set]) -> None:
    """Stand in for the global-repos backend registry list_repositories publishes from."""

    def _get(alias: str) -> Optional[dict]:
        return {"alias_name": alias} if known and alias in known else None

    monkeypatch.setattr(_global_clone, "_get_global_repo", _get)


# --- published_global_clone_path -------------------------------------------------


def test_published_alias_resolves_to_the_golden_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_registry(monkeypatch, {"example-repo-global"})
    path = _global_clone.published_global_clone_path(
        "example-repo-global", _GRM(_Repo("/golden/example-repo"))
    )
    assert path == Path("/golden/example-repo")


def test_bare_alias_resolves_too(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_registry(monkeypatch, {"example-repo-global"})
    path = _global_clone.published_global_clone_path(
        "example-repo", _GRM(_Repo("/golden/example-repo"))
    )
    assert path == Path("/golden/example-repo")


def test_alias_the_registry_does_not_publish_does_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate the review asked for: resolution is limited to aliases the same
    registry list_repositories publishes from, so a registered-but-not-published
    golden repo is NOT reachable through this path."""
    _patch_registry(monkeypatch, set())
    assert (
        _global_clone.published_global_clone_path(
            "example-repo-global", _GRM(_Repo("/golden/example-repo"))
        )
        is None
    )


def test_unknown_golden_repo_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_registry(monkeypatch, {"example-repo-global"})
    assert (
        _global_clone.published_global_clone_path("example-repo-global", _GRM(None))
        is None
    )


def test_repo_without_clone_path_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_registry(monkeypatch, {"example-repo-global"})
    assert (
        _global_clone.published_global_clone_path(
            "example-repo-global", _GRM(_Repo(""))
        )
        is None
    )


def test_suffix_only_alias_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_registry(monkeypatch, {"-global"})
    assert (
        _global_clone.published_global_clone_path("-global", _GRM(_Repo("/g/x")))
        is None
    )


def test_missing_manager_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_registry(monkeypatch, {"example-repo-global"})
    assert (
        _global_clone.published_global_clone_path("example-repo-global", None) is None
    )


def test_bare_alias_helper_strips_only_the_suffix() -> None:
    assert _global_clone.bare_alias("x-global") == "x"
    assert _global_clone.bare_alias("x") == "x"
    assert _global_clone.bare_alias("x-global-global") == "x-global"


# --- get_index_status handler ----------------------------------------------------


class _User:
    username = "testuser"


def _patch_handler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_side_effect: Any,
    clone: Optional[Path],
    access_ok: Any = True,
) -> dict:
    """Wire the handler's collaborators; returns a dict recording what was called."""
    from code_indexer.server.mcp.handlers import admin as admin_pkg
    from code_indexer.server.services import activated_repo_index_manager as arim

    seen: dict = {"for_path": None, "access": []}

    class _Mgr:
        def __init__(self, **kw: Any) -> None:
            pass

        def get_index_status(self, repo_alias: str, username: str) -> dict:
            if isinstance(status_side_effect, Exception):
                raise status_side_effect
            return status_side_effect

        def get_index_status_for_path(
            self, repo_path: Path, repo_alias: str, username: str
        ) -> dict:
            seen["for_path"] = repo_path
            return {"semantic": {}, "fts": {}, "temporal": {}, "scip": {}}

    monkeypatch.setattr(arim, "ActivatedRepoIndexManager", _Mgr)
    monkeypatch.setattr(admin_pkg, "published_global_clone_path", lambda a, g: clone)

    def _require(_svc: Any, username: str, alias: Any) -> None:
        seen["access"].append((username, alias))
        if access_ok is not True:
            raise access_ok

    monkeypatch.setattr(admin_pkg, "require_repo_access", _require)
    monkeypatch.setattr(admin_pkg, "_get_access_filtering_service", lambda: object())

    class _AppState:
        activated_repo_manager = None

    class _App:
        state = _AppState()

    class _AppModule:
        app = _App()
        background_job_manager = object()
        golden_repo_manager = object()

    monkeypatch.setattr(admin_pkg._utils, "app_module", _AppModule())
    return seen


def test_get_index_status_falls_back_to_the_golden_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.mcp.handlers import get_index_status

    seen = _patch_handler(
        monkeypatch,
        status_side_effect=FileNotFoundError("Repository directory not found"),
        clone=Path("/golden/example-repo"),
    )
    result = get_index_status({"repository_alias": "example-repo-global"}, _User())
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is True
    assert seen["for_path"] == Path("/golden/example-repo")


def test_get_index_status_enforces_access_before_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The security half: the activated lookup is user-scoped, the golden clone is
    not, so a denied caller must never reach it."""
    from code_indexer.server.mcp.handlers import get_index_status
    from code_indexer.server.services.repo_access_guard import RepoAccessDeniedError

    seen = _patch_handler(
        monkeypatch,
        status_side_effect=FileNotFoundError("nope"),
        clone=Path("/golden/example-repo"),
        access_ok=RepoAccessDeniedError("example-repo-global", "testuser"),
    )
    result = get_index_status({"repository_alias": "example-repo-global"}, _User())
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is False
    assert "denied" in data["error"].lower()
    assert seen["for_path"] is None  # never resolved


def test_get_index_status_fails_closed_when_access_service_is_unwired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.mcp.handlers import get_index_status
    from code_indexer.server.services.repo_access_guard import (
        AccessFilteringServiceUnavailableError,
    )

    seen = _patch_handler(
        monkeypatch,
        status_side_effect=FileNotFoundError("nope"),
        clone=Path("/golden/example-repo"),
        access_ok=AccessFilteringServiceUnavailableError("unwired"),
    )
    result = get_index_status({"repository_alias": "example-repo-global"}, _User())
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is False
    assert seen["for_path"] is None


def test_get_index_status_activated_repo_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No FileNotFoundError -> the activated path answers and the fallback (and its
    access check) never runs."""
    from code_indexer.server.mcp.handlers import get_index_status

    seen = _patch_handler(
        monkeypatch,
        status_side_effect={"semantic": {}, "fts": {}, "temporal": {}, "scip": {}},
        clone=Path("/golden/example-repo"),
    )
    result = get_index_status({"repository_alias": "example-repo"}, _User())
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is True
    assert seen["for_path"] is None
    assert seen["access"] == []


def test_get_index_status_reraises_when_not_a_published_global_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.mcp.handlers import get_index_status

    _patch_handler(
        monkeypatch,
        status_side_effect=FileNotFoundError("Repository directory not found"),
        clone=None,
    )
    result = get_index_status({"repository_alias": "not-global"}, _User())
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is False


def test_missing_repo_still_reports_not_found_not_an_access_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order guard: resolve first, enforce access second.

    A repo that simply does not exist must keep reporting "not found". Checking
    access first turned that into "access filtering unavailable" wherever the
    service is unwired -- a regression caught by the existing reindex handler test.
    """
    from code_indexer.server.mcp.handlers import get_index_status
    from code_indexer.server.services.repo_access_guard import (
        AccessFilteringServiceUnavailableError,
    )

    _patch_handler(
        monkeypatch,
        status_side_effect=FileNotFoundError("Repository 'x' not found"),
        clone=None,  # not a published global repo
        access_ok=AccessFilteringServiceUnavailableError("unwired"),
    )
    result = get_index_status({"repository_alias": "nonexistent-repo"}, _User())
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is False
    assert "not found" in data["error"].lower()


def test_unreachable_registry_means_not_published_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry that raises (no backend wired) must not surface as the caller's
    error -- it means "cannot confirm", so the original outcome stands."""

    def _boom(alias: str) -> Any:
        raise AttributeError("'State' object has no attribute 'backend_registry'")

    monkeypatch.setattr(_global_clone, "_get_global_repo", _boom)
    assert (
        _global_clone.published_global_clone_path(
            "example-repo-global", _GRM(_Repo("/golden/example-repo"))
        )
        is None
    )


# --- check_hnsw_health handler ---------------------------------------------------


def _patch_hnsw(
    monkeypatch: pytest.MonkeyPatch,
    *,
    golden_repo: Optional[_Repo],
    clone: Optional[Path],
    access_ok: Any = True,
) -> dict:
    """Wire check_hnsw_health's collaborators; returns what was observed."""
    from code_indexer.server.mcp.handlers import repos as repos_mod

    seen: dict = {"index_base_path": None, "submitted": None, "access": []}

    class _BJM:
        def submit_job(self, **kw: Any) -> str:
            seen["submitted"] = kw
            # The job closes over index_base_path; run it to capture what it would read.
            return "job-1"

    class _GRMStub:
        def get_golden_repo(self, alias: str) -> Optional[_Repo]:
            return golden_repo

    class _AppModule:
        golden_repo_manager = _GRMStub()
        background_job_manager = _BJM()

    monkeypatch.setattr(repos_mod._utils, "app_module", _AppModule())
    monkeypatch.setattr(repos_mod, "published_global_clone_path", lambda a, g: clone)

    def _require(_svc: Any, username: str, alias: Any) -> None:
        seen["access"].append((username, alias))
        if access_ok is not True:
            raise access_ok

    monkeypatch.setattr(repos_mod, "require_repo_access", _require)
    monkeypatch.setattr(repos_mod, "_get_access_filtering_service", lambda: object())
    return seen


def test_check_hnsw_health_global_alias_uses_the_golden_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The published "<alias>-global" resolves, and the submitted job is scoped to
    the GOLDEN clone -- not to an activated path that does not exist."""
    from code_indexer.server.mcp.handlers import check_hnsw_health

    seen = _patch_hnsw(
        monkeypatch, golden_repo=None, clone=Path("/golden/example-repo")
    )
    result = check_hnsw_health({"repository_alias": "example-repo-global"}, _User())
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is True
    assert seen["submitted"]["repo_alias"] == "example-repo-global"
    assert seen["submitted"]["operation_type"] == "repository_health_check"


def test_check_hnsw_health_bare_alias_still_uses_the_direct_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing behaviour: a bare golden alias resolves without the fallback."""
    from code_indexer.server.mcp.handlers import check_hnsw_health

    seen = _patch_hnsw(
        monkeypatch, golden_repo=_Repo("/golden/example-repo"), clone=None
    )
    result = check_hnsw_health({"repository_alias": "example-repo"}, _User())
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is True
    assert seen["submitted"] is not None


def test_check_hnsw_health_unpublished_alias_reports_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.mcp.handlers import check_hnsw_health

    seen = _patch_hnsw(monkeypatch, golden_repo=None, clone=None)
    result = check_hnsw_health({"repository_alias": "example-repo-global"}, _User())
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is False
    assert "not found" in data["error"].lower()
    assert seen["submitted"] is None


def test_check_hnsw_health_denies_before_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This handler previously accepted a bare alias with NO access check at all."""
    from code_indexer.server.mcp.handlers import check_hnsw_health
    from code_indexer.server.services.repo_access_guard import RepoAccessDeniedError

    seen = _patch_hnsw(
        monkeypatch,
        golden_repo=_Repo("/golden/example-repo"),
        clone=None,
        access_ok=RepoAccessDeniedError("example-repo", "testuser"),
    )
    result = check_hnsw_health({"repository_alias": "example-repo"}, _User())
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is False
    assert seen["submitted"] is None


def test_check_hnsw_health_fails_closed_when_access_service_unwired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.mcp.handlers import check_hnsw_health
    from code_indexer.server.services.repo_access_guard import (
        AccessFilteringServiceUnavailableError,
    )

    seen = _patch_hnsw(
        monkeypatch,
        golden_repo=_Repo("/golden/example-repo"),
        clone=None,
        access_ok=AccessFilteringServiceUnavailableError("unwired"),
    )
    result = check_hnsw_health({"repository_alias": "example-repo"}, _User())
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is False
    # Assert the SPECIFIC refusal: a generic error path also yields success=False,
    # so anything weaker passes even when the fail-closed branch is gone.
    assert "access filtering unavailable" in data["error"].lower()
    assert seen["submitted"] is None
