"""Temporal status never says "absent" when this pod cannot tell (issue-64616).

Under a delegated IndexStore a status read serves the recorded temporal
version first (``served_temporal_index_root``). A pointer recording NO
temporal version is answered from what this pod holds -- correct when temporal
is off, but for a repository with temporal ON whose pointer predates temporal
publishing an empty root here is "not served here", not "absent".
``require_temporal_answerable`` says so, and each status surface reports it
distinctly: the activated index manager as ``not_served_here``, the REST
index-status and health routes as 503. The local store is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.services import temporal_index_store as tis
from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.server.services.index_materializer import (
    PublishedIndexUnavailableError,
)
from code_indexer.server.startup import index_store_wiring
from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)
from code_indexer.utils.index_store import (
    DelegatedIndexStore,
    LocalIndexStore,
    TEMPORAL_INDEX_STORE_ALIAS_FIELD,
    TEMPORAL_INDEX_VERSION_FIELD,
    temporal_index_store_alias,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

ALIAS = "example-repo"


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "golden-repos"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DelegatedIndexStore:
    delegated = DelegatedIndexStore(FakeArtifacts(tmp_path / "store"))
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: delegated)
    return delegated


def _pointer(golden: Path, *, temporal: bool) -> None:
    extra: Optional[Dict[str, Any]] = None
    if temporal:
        extra = {
            TEMPORAL_INDEX_STORE_ALIAS_FIELD: temporal_index_store_alias(ALIAS),
            TEMPORAL_INDEX_VERSION_FIELD: "v1",
        }
    AliasManager(str(golden / "aliases")).create_alias(
        f"{ALIAS}-global", str(golden / ALIAS), repo_name=ALIAS, extra=extra
    )


def _golden_manager(enabled: bool) -> Any:
    manager = MagicMock()
    manager.get_golden_repo.return_value = SimpleNamespace(enable_temporal=enabled)
    return manager


def _answerable(golden: Path, enabled: bool, store: Any = None) -> None:
    tis.require_temporal_answerable(
        golden, ALIAS, golden_repo_manager=_golden_manager(enabled), store=store
    )


# ── the rule ────────────────────────────────────────────────────────────────


def test_enabled_unrecorded_and_nothing_here_is_not_served_here(
    golden: Path, store: DelegatedIndexStore
) -> None:
    _pointer(golden, temporal=False)
    with pytest.raises(tis.TemporalNotServedHereError, match="not served on this pod"):
        _answerable(golden, True)


def test_not_served_here_is_a_published_index_error(
    golden: Path, store: DelegatedIndexStore
) -> None:
    _pointer(golden, temporal=False)
    with pytest.raises(PublishedIndexUnavailableError):
        _answerable(golden, True)


def test_temporal_off_is_answerable_absent(
    golden: Path, store: DelegatedIndexStore
) -> None:
    _pointer(golden, temporal=False)
    _answerable(golden, False)


def test_a_recorded_version_is_answerable_it_is_served(
    golden: Path, store: DelegatedIndexStore
) -> None:
    _pointer(golden, temporal=True)
    _answerable(golden, True)


def test_a_root_this_pod_holds_is_answerable(
    golden: Path, store: DelegatedIndexStore
) -> None:
    _pointer(golden, temporal=False)
    server_temporal_index_root(golden, ALIAS).mkdir(parents=True)
    _answerable(golden, True)


def test_the_local_store_is_unchanged(golden: Path) -> None:
    """Nothing is read under the local store -- not even the golden repo."""
    _pointer(golden, temporal=False)
    tis.require_temporal_answerable(
        golden, ALIAS, golden_repo_manager=object(), store=LocalIndexStore()
    )


def test_no_golden_manager_cannot_tell_so_it_is_not_absent(
    golden: Path, store: DelegatedIndexStore
) -> None:
    _pointer(golden, temporal=False)
    assert tis.golden_temporal_enabled(None, ALIAS) is True
    with pytest.raises(tis.TemporalNotServedHereError):
        tis.require_temporal_answerable(golden, ALIAS, golden_repo_manager=None)


def test_temporal_enabled_comes_from_the_golden_repo() -> None:
    manager = MagicMock()
    manager.get_golden_repo.return_value = SimpleNamespace(enable_temporal=True)
    assert tis.golden_temporal_enabled(manager, f"{ALIAS}-global") is True
    manager.get_golden_repo.assert_called_with(ALIAS)
    manager.get_golden_repo.return_value = None
    assert tis.golden_temporal_enabled(manager, ALIAS) is False


# ── the status surfaces ─────────────────────────────────────────────────────


def _activated_manager(golden: Path, enabled: bool) -> Any:
    golden_manager = MagicMock()
    golden_manager.get_golden_repo.return_value = SimpleNamespace(
        enable_temporal=enabled
    )
    activated = MagicMock()
    activated.activated_repos_dir = str(golden.parent / "activated-repos")
    activated.golden_repo_manager = golden_manager
    activated.get_repository.return_value = {"golden_repo_alias": ALIAS}
    return activated


def _index_manager(golden: Path, enabled: bool) -> ActivatedRepoIndexManager:
    return ActivatedRepoIndexManager(
        data_dir=str(golden.parent),
        background_job_manager=MagicMock(),
        activated_repo_manager=_activated_manager(golden, enabled),
    )


def test_index_manager_reports_not_served_here_distinctly(
    golden: Path, store: DelegatedIndexStore, tmp_path: Path
) -> None:
    _pointer(golden, temporal=False)
    copy = tmp_path / "copy"
    (copy / ".code-indexer" / "index").mkdir(parents=True)
    status = _index_manager(golden, True)._get_temporal_status(copy, ALIAS, "alice")
    assert status["status"] == "not_served_here"
    assert "not served on this pod" in status["error"]


def test_index_manager_reports_not_indexed_when_temporal_is_off(
    golden: Path, store: DelegatedIndexStore, tmp_path: Path
) -> None:
    _pointer(golden, temporal=False)
    copy = tmp_path / "copy"
    (copy / ".code-indexer" / "index").mkdir(parents=True)
    status = _index_manager(golden, False)._get_temporal_status(copy, ALIAS, "alice")
    assert status == {"status": "not_indexed"}


def _alice(rh: Any, monkeypatch: pytest.MonkeyPatch, clone: Path) -> Any:
    """The caller, with the route's resolver answering *clone* for ALIAS."""
    monkeypatch.setattr(rh, "_resolve_repository_path", lambda a, u: (ALIAS, clone))
    return SimpleNamespace(username="alice")


def test_health_route_answers_503_not_has_temporal_false(
    golden: Path,
    store: DelegatedIndexStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.routers import repository_health as rh

    _pointer(golden, temporal=False)
    golden_manager = MagicMock()
    golden_manager.get_golden_repo.return_value = SimpleNamespace(enable_temporal=True)
    activated = MagicMock()
    activated.activated_repos_dir = str(golden.parent / "activated-repos")
    monkeypatch.setattr(rh, "_get_golden_repo_manager", lambda: golden_manager)
    monkeypatch.setattr(rh, "_get_activated_repo_manager", lambda: activated)
    clone = tmp_path / "served-clone"
    (clone / ".code-indexer" / "index").mkdir(parents=True)
    with pytest.raises(HTTPException) as raised:
        rh._check_indexes(ALIAS, _alice(rh, monkeypatch, clone))
    assert raised.value.status_code == 503
    assert "not served on this pod" in str(raised.value.detail)


def test_health_route_still_says_false_when_temporal_is_off(
    golden: Path,
    store: DelegatedIndexStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.server.routers import repository_health as rh

    _pointer(golden, temporal=False)
    golden_manager = MagicMock()
    golden_manager.get_golden_repo.return_value = SimpleNamespace(enable_temporal=False)
    activated = MagicMock()
    activated.activated_repos_dir = str(golden.parent / "activated-repos")
    monkeypatch.setattr(rh, "_get_golden_repo_manager", lambda: golden_manager)
    monkeypatch.setattr(rh, "_get_activated_repo_manager", lambda: activated)
    clone = tmp_path / "served-clone"
    (clone / ".code-indexer" / "index").mkdir(parents=True)
    result = rh._check_indexes(ALIAS, _alice(rh, monkeypatch, clone))
    assert result.has_temporal is False
