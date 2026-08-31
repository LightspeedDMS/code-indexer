"""GitHub Issue #1459 AC4: activated_repos.py's get_indexes_status()
temporal-presence detection must route through the shared
TemporalShardResolver-based get_temporal_repo_status() helper instead of
scanning only the local clone path -- so an activated repo whose backing
golden repo's temporal data has relocated to Story #1457's sister location
is still reported as indexed, never as "not indexed".

Follows this test module's own established pattern (see
test_activated_repos_health_async_job_1394.py): a minimal FastAPI app
mounting activated_repos.router, with `_get_activated_repo_manager`
patched to a lightweight test double (never a mock of
AliasManager/TemporalShardResolver/the filesystem itself -- those stay
100% real, Messi Rule #1).
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)
from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers import activated_repos


def _test_user() -> User:
    return User(
        username="alice",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class _FakeActivatedRepoManager:
    def __init__(
        self,
        activated_repos_dir: Path,
        repo_path: Path,
        user_alias: str,
        golden_repo_alias: Optional[str],
    ):
        self.activated_repos_dir = str(activated_repos_dir)
        self._repo_path = repo_path
        self._user_alias = user_alias
        self._golden_repo_alias = golden_repo_alias

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        assert user_alias == self._user_alias
        return str(self._repo_path)

    def get_repository(self, username: str, user_alias: str, *, touch: bool = True):
        if user_alias != self._user_alias:
            return None
        return {
            "user_alias": user_alias,
            "golden_repo_alias": self._golden_repo_alias,
        }


class _AppUnderTest:
    def __init__(self, activated_manager):
        self._activated_manager = activated_manager
        self._stack: Optional[ExitStack] = None

    def __enter__(self) -> TestClient:
        app = FastAPI()
        app.include_router(activated_repos.router)
        app.dependency_overrides[get_current_user_hybrid] = _test_user

        self._stack = ExitStack()
        self._stack.enter_context(
            patch.object(
                activated_repos,
                "_get_activated_repo_manager",
                return_value=self._activated_manager,
            )
        )
        return TestClient(app)

    def __exit__(self, *exc_info):
        assert self._stack is not None
        self._stack.close()


def _write_committed_row(shard_dir: Path) -> None:
    nested = shard_dir / "a"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "vector_abc123.json").write_text('{"point_id": "p1"}')


@pytest.fixture
def layout(tmp_path):
    server_data_dir = tmp_path / "server-data"
    activated_repos_dir = server_data_dir / "activated-repos"
    activated_repos_dir.mkdir(parents=True)
    golden_repos_dir = server_data_dir / "golden-repos"
    repo_path = tmp_path / "activated-clones" / "myactivated"
    repo_path.mkdir(parents=True)
    return {
        "activated_repos_dir": activated_repos_dir,
        "golden_repos_dir": golden_repos_dir,
        "repo_path": repo_path,
    }


def _temporal_index_status(payload: dict) -> dict:
    return next(i for i in payload["indexes"] if i["index_type"] == "temporal")


def test_local_clone_only_temporal_data_still_detected(layout):
    """REGRESSION SAFETY: pre-relocation local-clone temporal data is still
    correctly reported as exists=True, healthy=True."""
    repo_path = layout["repo_path"]
    shard_dir = (
        repo_path / ".code-indexer" / "index" / "code-indexer-temporal-voyage_code_3"
    )
    _write_committed_row(shard_dir)
    (shard_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

    manager = _FakeActivatedRepoManager(
        layout["activated_repos_dir"],
        repo_path,
        user_alias="myactivated",
        golden_repo_alias="backing-golden",
    )

    with _AppUnderTest(manager) as client:
        resp = client.get("/api/activated-repos/myactivated/indexes")

    assert resp.status_code == 200
    temporal = _temporal_index_status(resp.json())
    assert temporal["exists"] is True
    assert temporal["healthy"] is True


def test_sister_relocated_temporal_data_is_detected_not_reported_missing(layout):
    """THE ACTUAL BUG FIX: temporal data relocated to the sister location
    for the backing golden repo (real alias pointer, real hnsw_index.bin),
    ZERO local clone copy -- must be reported exists=True, never False."""
    repo_path = layout["repo_path"]
    golden_repos_dir = layout["golden_repos_dir"]
    (repo_path / ".code-indexer" / "index").mkdir(parents=True)

    # Bug #1529: temporal data lives at the FIXED server-owned root
    # ({golden_repos_dir}/.temporal/{alias}/), not a .versioned
    # snapshot behind an alias pointer. The behavior under test is
    # unchanged: status must detect data OUTSIDE the repo tree.
    sister_version_dir = (
        server_temporal_index_root(golden_repos_dir, "backing-golden")
        / "code-indexer-temporal-voyage_code_3-2024Q1"
    )
    sister_version_dir.mkdir(parents=True)
    (sister_version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    # A real committed row: status keys off DATA presence, not merely
    # the presence of an index file.
    (sister_version_dir / "vector_aaaa1111.json").write_text("{}")

    manager = _FakeActivatedRepoManager(
        layout["activated_repos_dir"],
        repo_path,
        user_alias="myactivated",
        golden_repo_alias="backing-golden",
    )

    with _AppUnderTest(manager) as client:
        resp = client.get("/api/activated-repos/myactivated/indexes")

    assert resp.status_code == 200
    temporal = _temporal_index_status(resp.json())
    assert temporal["exists"] is True
    assert temporal["healthy"] is True


def test_no_golden_repo_alias_falls_back_to_local_scan_only(layout):
    """When golden_repo_alias cannot be resolved (e.g. composite repo),
    behavior gracefully falls back to the pre-existing local-clone-only
    scan rather than raising."""
    repo_path = layout["repo_path"]

    manager = _FakeActivatedRepoManager(
        layout["activated_repos_dir"],
        repo_path,
        user_alias="myactivated",
        golden_repo_alias=None,
    )

    with _AppUnderTest(manager) as client:
        resp = client.get("/api/activated-repos/myactivated/indexes")

    assert resp.status_code == 200
    temporal = _temporal_index_status(resp.json())
    assert temporal["exists"] is False


def test_resolve_golden_repo_alias_get_repository_exception_returns_none():
    """Direct unit test: _resolve_golden_repo_alias_for_activated_repo's
    except-Exception branch (not reachable via the full endpoint tests
    above, since those always succeed)."""

    class _RaisingActivatedRepoManager:
        def get_repository(self, username, user_alias, *, touch=True):
            raise RuntimeError("boom")

    result = activated_repos._resolve_golden_repo_alias_for_activated_repo(
        _RaisingActivatedRepoManager(), "alice", "myrepo"
    )

    assert result is None


def test_resolve_golden_repo_alias_falsy_metadata_returns_none():
    """Direct unit test: _resolve_golden_repo_alias_for_activated_repo's
    `if not metadata: return None` branch."""

    class _EmptyActivatedRepoManager:
        def get_repository(self, username, user_alias, *, touch=True):
            return None

    result = activated_repos._resolve_golden_repo_alias_for_activated_repo(
        _EmptyActivatedRepoManager(), "alice", "myrepo"
    )

    assert result is None
