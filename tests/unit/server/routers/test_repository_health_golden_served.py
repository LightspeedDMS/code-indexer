"""Golden repository health resolves the SERVED target on any pod.

Under ``index_store_backend: delegated`` a golden repo's index may have been
published by another pod; ``_resolve_repository_path`` must make this pod hold
the alias's recorded version (``AliasManager.ensure_served_here``) and check
that path -- not ``get_actual_repo_path``, which only finds what happens to
be on this pod's disk. A store failure is an error. Local: unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import threading
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.routers import repository_health as health
from code_indexer.server.services import index_materializer as im
from code_indexer.server.startup import index_store_wiring
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    DelegatedIndexStore,
    LocalIndexStore,
    published_index_fields,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

REPO = "agent-router"


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: Any) -> Any:
    golden = tmp_path / "pod-b" / "golden-repos"
    manager = MagicMock()
    manager.golden_repos_dir = str(golden)
    manager.get_golden_repo.side_effect = lambda a: object() if a == REPO else None
    manager.get_actual_repo_path.return_value = str(golden / REPO)
    monkeypatch.setattr(health, "_get_golden_repo_manager", lambda: manager)
    monkeypatch.setattr(index_store_wiring, "server_index_store", lambda: store)
    monkeypatch.setattr(im, "run_fix_config", lambda target: None)
    return golden, manager


def _publish(tmp_path: Path, artifacts: FakeArtifacts, golden: Path) -> Path:
    built = tmp_path / "pod-a" / REPO / INDEX_DIRNAME
    (built / "index").mkdir(parents=True)
    (built / "index" / "hnsw.bin").write_bytes(b"published")
    version = DelegatedIndexStore(artifacts).publish_as(REPO, built)
    target = golden / ".versioned" / REPO / "v_1760000000"
    AliasManager(str(golden / "aliases")).create_alias(
        f"{REPO}-global",
        str(target),
        repo_name=REPO,
        extra=published_index_fields(REPO, version),
    )
    return target


def _user() -> Any:
    user = MagicMock()
    user.username = "alice"
    return user


@pytest.mark.parametrize("alias", [REPO, f"{REPO}-global"])
def test_pod_b_health_checks_the_served_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str
) -> None:
    artifacts = FakeArtifacts(tmp_path / "store")
    golden, manager = _world(tmp_path, monkeypatch, DelegatedIndexStore(artifacts))
    target = _publish(tmp_path, artifacts, golden)
    assert not target.exists()
    resolved, path = health._resolve_repository_path(alias, _user())
    assert (resolved, path) == (REPO, target)
    assert (target / INDEX_DIRNAME / "index" / "hnsw.bin").read_bytes() == b"published"
    manager.get_actual_repo_path.assert_not_called()


def test_a_store_failure_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = FakeArtifacts(tmp_path / "store")
    golden, _ = _world(tmp_path, monkeypatch, DelegatedIndexStore(artifacts))
    _publish(tmp_path, artifacts, golden)
    artifacts.fail_checkout = True
    with pytest.raises(im.PublishedIndexUnavailableError):
        health._resolve_repository_path(REPO, _user())


def test_no_pointer_falls_back_to_the_registry_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden, manager = _world(
        tmp_path, monkeypatch, DelegatedIndexStore(FakeArtifacts(tmp_path / "s"))
    )
    assert health._resolve_repository_path(REPO, _user()) == (REPO, golden / REPO)


def test_local_health_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden, manager = _world(tmp_path, monkeypatch, LocalIndexStore())
    monkeypatch.setattr(
        AliasManager, "read_alias", lambda *a: pytest.fail("alias read locally")
    )
    assert health._resolve_repository_path(REPO, _user()) == (REPO, golden / REPO)
    manager.get_actual_repo_path.assert_called_once_with(REPO)


# ── Pod-B /indexes tests ────────────────────────────────────────────


@pytest.mark.parametrize("alias", [REPO, f"{REPO}-global"])
def test_pod_b_check_indexes_uses_served_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str
) -> None:
    """_check_indexes resolves via the served target and reports its indexes."""
    artifacts = FakeArtifacts(tmp_path / "store")
    golden, manager = _world(tmp_path, monkeypatch, DelegatedIndexStore(artifacts))
    target = _publish(tmp_path, artifacts, golden)
    assert not target.exists()

    # Put a semantic index in the published tree so has_semantic is True
    (tmp_path / "pod-a" / REPO / INDEX_DIRNAME / "index" / "voyage-code-3").mkdir(
        parents=True, exist_ok=True
    )
    (
        tmp_path / "pod-a" / REPO / INDEX_DIRNAME / "index" / "voyage-code-3" / "hnsw_index.bin"
    ).write_bytes(b"hnsw")
    version = DelegatedIndexStore(artifacts).publish_as(REPO, tmp_path / "pod-a" / REPO / INDEX_DIRNAME)
    AliasManager(str(golden / "aliases")).create_alias(
        f"{REPO}-global",
        str(target),
        repo_name=REPO,
        extra=published_index_fields(REPO, version),
    )

    result = health._check_indexes(alias, _user())

    assert isinstance(result, health.IndexesStatusResponse)
    assert result.has_semantic is True
    assert (target / INDEX_DIRNAME / "index").exists()
    manager.get_actual_repo_path.assert_not_called()


def test_check_indexes_store_failure_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = FakeArtifacts(tmp_path / "store")
    golden, _ = _world(tmp_path, monkeypatch, DelegatedIndexStore(artifacts))
    _publish(tmp_path, artifacts, golden)
    artifacts.fail_checkout = True
    with pytest.raises(im.PublishedIndexUnavailableError):
        health._check_indexes(REPO, _user())


# ── Thread-offload tests ────────────────────────────────────────────


@pytest.mark.anyio
async def test_resolve_repository_path_runs_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """check_repository_health_async offloads _resolve_repository_path
    to a worker thread, not the event-loop thread."""
    golden, _ = _world(tmp_path, monkeypatch, LocalIndexStore())
    captured_idents: list[int] = []
    original = health._resolve_repository_path

    def recording_resolve(*args: Any, **kwargs: Any) -> Any:
        captured_idents.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(health, "_resolve_repository_path", recording_resolve)
    monkeypatch.setattr(health, "_enforce_repo_access", lambda *a: None)
    monkeypatch.setattr(
        health, "_get_background_job_manager",
        lambda: MagicMock(submit_job=MagicMock(return_value="job-1")),
    )

    loop_ident = threading.get_ident()
    await health.check_repository_health_async(REPO, current_user=_user())
    assert captured_idents, "resolver was never called"
    assert captured_idents[0] != loop_ident, \
        "_resolve_repository_path ran on the event-loop thread"


@pytest.mark.anyio
async def test_check_indexes_runs_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_repository_indexes offloads _check_indexes to a worker thread,
    not the event-loop thread."""
    golden, _ = _world(tmp_path, monkeypatch, LocalIndexStore())
    captured_idents: list[int] = []
    original = health._check_indexes

    def recording_check(*args: Any, **kwargs: Any) -> Any:
        captured_idents.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(health, "_check_indexes", recording_check)
    monkeypatch.setattr(health, "_enforce_repo_access", lambda *a: None)

    loop_ident = threading.get_ident()
    await health.get_repository_indexes(REPO, current_user=_user())
    assert captured_idents, "_check_indexes was never called"
    assert captured_idents[0] != loop_ident, \
        "_check_indexes ran on the event-loop thread"
