"""GitHub Issue #1459 AC4: repository_health.py's get_repository_indexes()
temporal-presence detection must route through the shared
TemporalShardResolver-based get_temporal_repo_status() helper instead of
scanning only the local clone path -- so a repository whose temporal data
has relocated to Story #1457's golden-owned sister location is still
reported as indexed, never as "not indexed".

Follows this test module's own established pattern (see
test_repository_health_async_job_1394.py): a minimal FastAPI app mounting
repository_health.router, with `_get_golden_repo_manager` /
`_get_activated_repo_manager` patched to lightweight test doubles (never
mocks of AliasManager/TemporalShardResolver/the filesystem itself -- those
stay 100% real, Messi Rule #1).
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
from code_indexer.server.routers import repository_health


def _test_user(username: str = "alice") -> User:
    return User(
        username=username,
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class _FakeGoldenRepoManager:
    """Resolves ONE known golden alias -> clone_path; everything else is a
    miss (mirrors the real manager's contract, no logic under test here)."""

    def __init__(self, clone_path: Path, known_alias: str):
        self._clone_path = clone_path
        self._known_alias = known_alias

    def get_golden_repo(self, alias: str):
        return object() if alias == self._known_alias else None

    def get_actual_repo_path(self, alias: str) -> str:
        assert alias == self._known_alias
        return str(self._clone_path)


class _FakeActivatedRepoManager:
    """Resolves ONE known activated user_alias -> clone_path + metadata
    (including golden_repo_alias, the field the router must now read to
    build the resolver). Real activated_repos_dir on disk so
    golden_repos_dir = Path(activated_repos_dir).parent / "golden-repos"
    resolves to a real, usable directory."""

    def __init__(
        self,
        activated_repos_dir: Path,
        clone_path: Path,
        known_alias: str,
        golden_repo_alias: Optional[str],
    ):
        self.activated_repos_dir = str(activated_repos_dir)
        self._clone_path = clone_path
        self._known_alias = known_alias
        self._golden_repo_alias = golden_repo_alias

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        if user_alias != self._known_alias:
            raise FileNotFoundError(user_alias)
        return str(self._clone_path)

    def get_repository(self, username: str, user_alias: str, *, touch: bool = True):
        if user_alias != self._known_alias:
            return None
        return {
            "user_alias": user_alias,
            "golden_repo_alias": self._golden_repo_alias,
        }


class _AppUnderTest:
    def __init__(self, golden_manager=None, activated_manager=None):
        self._golden_manager = golden_manager
        self._activated_manager = activated_manager
        self._stack: Optional[ExitStack] = None

    def __enter__(self) -> TestClient:
        app = FastAPI()
        app.include_router(repository_health.router)
        app.dependency_overrides[get_current_user_hybrid] = _test_user

        self._stack = ExitStack()
        self._stack.enter_context(
            patch.object(
                repository_health,
                "_get_golden_repo_manager",
                return_value=self._golden_manager,
            )
        )
        self._stack.enter_context(
            patch.object(
                repository_health,
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
def golden_layout(tmp_path):
    """clone_path (repo root) and golden_repos_dir (sister root, sibling
    of the activated-repos dir) laid out the way production wiring
    derives them: golden_repos_dir = Path(activated_repos_dir).parent /
    "golden-repos"."""
    server_data_dir = tmp_path / "server-data"
    activated_repos_dir = server_data_dir / "activated-repos"
    activated_repos_dir.mkdir(parents=True)
    golden_repos_dir = server_data_dir / "golden-repos"
    clone_path = tmp_path / "clones" / "myrepo"
    clone_path.mkdir(parents=True)
    return {
        "activated_repos_dir": activated_repos_dir,
        "golden_repos_dir": golden_repos_dir,
        "clone_path": clone_path,
    }


class TestGoldenRepoBranchTemporalDetection:
    """repo_alias resolves via golden_repo_manager.get_golden_repo (Strategy 1)."""

    def test_local_clone_only_temporal_data_still_detected(self, golden_layout):
        """REGRESSION SAFETY: pre-relocation local-clone temporal data is
        still correctly reported as present (has_temporal=True)."""
        clone_path = golden_layout["clone_path"]
        shard_dir = (
            clone_path
            / ".code-indexer"
            / "index"
            / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        _write_committed_row(shard_dir)
        (shard_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

        golden_manager = _FakeGoldenRepoManager(clone_path, known_alias="myrepo")
        activated_manager = _FakeActivatedRepoManager(
            golden_layout["activated_repos_dir"],
            clone_path,
            known_alias="unused",
            golden_repo_alias=None,
        )

        with _AppUnderTest(golden_manager, activated_manager) as client:
            resp = client.get("/api/repositories/myrepo/indexes")

        assert resp.status_code == 200
        assert resp.json()["has_temporal"] is True

    def test_sister_relocated_temporal_data_is_detected_not_reported_missing(
        self, golden_layout
    ):
        """THE ACTUAL BUG FIX: temporal data relocated to the sister
        location (real alias pointer, real hnsw_index.bin), ZERO local
        clone copy -- must be reported has_temporal=True, never False."""
        clone_path = golden_layout["clone_path"]
        golden_repos_dir = golden_layout["golden_repos_dir"]
        # Local index dir exists but is empty -- no local temporal copy.
        (clone_path / ".code-indexer" / "index").mkdir(parents=True)

        # Bug #1529: temporal data lives at the FIXED server-owned root
        # ({golden_repos_dir}/.temporal/{alias}/), not a .versioned
        # snapshot behind an alias pointer. The behavior under test is
        # unchanged: status must detect data OUTSIDE the repo tree.
        sister_version_dir = (
            server_temporal_index_root(golden_repos_dir, "myrepo")
            / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        sister_version_dir.mkdir(parents=True)
        (sister_version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
        # A real committed row: status keys off DATA presence, not merely
        # the presence of an index file.
        (sister_version_dir / "vector_aaaa1111.json").write_text("{}")

        golden_manager = _FakeGoldenRepoManager(clone_path, known_alias="myrepo")
        activated_manager = _FakeActivatedRepoManager(
            golden_layout["activated_repos_dir"],
            clone_path,
            known_alias="unused",
            golden_repo_alias=None,
        )

        with _AppUnderTest(golden_manager, activated_manager) as client:
            resp = client.get("/api/repositories/myrepo/indexes")

        assert resp.status_code == 200
        assert resp.json()["has_temporal"] is True


class TestActivatedRepoBranchTemporalDetection:
    """repo_alias resolves via activated_repo_manager (Strategy 3) -- the
    golden_repo_alias needed for the resolver comes from
    activated_repo_manager.get_repository(...)["golden_repo_alias"]."""

    def test_sister_relocated_temporal_data_is_detected_for_activated_repo(
        self, golden_layout
    ):
        """THE ACTUAL BUG FIX for the activated-repo resolution branch:
        golden_repo_alias is resolved from get_repository() metadata, then
        used to query the resolver against the sister location."""
        clone_path = golden_layout["clone_path"]
        golden_repos_dir = golden_layout["golden_repos_dir"]
        (clone_path / ".code-indexer" / "index").mkdir(parents=True)

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

        golden_manager = _FakeGoldenRepoManager(clone_path, known_alias="__none__")
        activated_manager = _FakeActivatedRepoManager(
            golden_layout["activated_repos_dir"],
            clone_path,
            known_alias="my-activated-repo",
            golden_repo_alias="backing-golden",
        )

        with _AppUnderTest(golden_manager, activated_manager) as client:
            resp = client.get("/api/repositories/my-activated-repo/indexes")

        assert resp.status_code == 200
        assert resp.json()["has_temporal"] is True


class TestResolveGoldenRepoAliasForActivatedRepoHelper:
    """Direct unit tests for _resolve_golden_repo_alias_for_activated_repo's
    error-handling branches (not reachable via the full endpoint tests
    above, since those always succeed)."""

    def test_get_repository_exception_is_logged_and_returns_none(self):
        from code_indexer.server.routers import repository_health

        class _RaisingActivatedRepoManager:
            def get_repository(self, username, user_alias, *, touch=True):
                raise RuntimeError("boom")

        with patch.object(
            repository_health,
            "_get_activated_repo_manager",
            return_value=_RaisingActivatedRepoManager(),
        ):
            result = repository_health._resolve_golden_repo_alias_for_activated_repo(
                "alice", "myrepo"
            )

        assert result is None

    def test_get_repository_returns_falsy_metadata_returns_none(self):
        from code_indexer.server.routers import repository_health

        class _EmptyActivatedRepoManager:
            def get_repository(self, username, user_alias, *, touch=True):
                return None

        with patch.object(
            repository_health,
            "_get_activated_repo_manager",
            return_value=_EmptyActivatedRepoManager(),
        ):
            result = repository_health._resolve_golden_repo_alias_for_activated_repo(
                "alice", "myrepo"
            )

        assert result is None
