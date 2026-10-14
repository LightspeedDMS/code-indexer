"""Golden repository statistics through the content seam (WP7 listing-stats).

``RepositoryListingManager.get_repository_statistics`` reports a golden repo's
file count, total size and newest mtime (``GET /api/repos/golden/{alias}``,
``GET /api/repositories/{id}``, MCP ``repository_status``). It walked the clone
on this pod. On a mount-less pod (``content_backend: delegated``) that clone
holds index state only, so the walk counted index files and reported wrong
numbers SILENTLY. Under delegated the numbers now come from the backend's
manifest; a backend failure raises instead of reading as zero files.

Also pins the unreachability guards behind this package's ``local-only`` rows.
"""

from __future__ import annotations

import ast
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.repository_listing_manager import (
    RepositoryListingManager,
    RepositoryStatisticsUnavailableError,
)
from code_indexer.server.services import file_service
from tests.unit.server.cidx_meta_seam_fakes import MetaWorld

_SRC = Path(__file__).resolve().parents[4] / "src" / "code_indexer"
_MTIME = 1_700_000_123


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetaWorld:
    # The seam's manifest cache is process-wide; each test starts cold.
    monkeypatch.setattr(file_service, "_collect_cache", OrderedDict())
    w = MetaWorld(tmp_path, monkeypatch)
    w.seed("a.py", "print('a')\n")
    w.seed("src/b.txt", "bee" * 100)
    w.seed("src/deep/c.md", "# c\n")
    for root in (w.local_meta, w.remote_meta):
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                os.utime(os.path.join(dirpath, name), (_MTIME, _MTIME))
    # Index state on the pod's clone: never repository content.
    (w.pod_meta / ".code-indexer" / "index.bin").write_bytes(b"x" * 4096)
    return w


def _manager(clone_path: Path, repo_url: str = "https://example.com/r.git"):
    golden: Dict[str, Any] = {
        "alias": "repo-a",
        "repo_url": repo_url,
        "default_branch": "main",
        "clone_path": str(clone_path),
        "created_at": "2024-01-01T00:00:00+00:00",
    }
    grm = MagicMock()
    grm.list_golden_repos.return_value = [golden]
    arm = MagicMock()
    arm.list_activated_repositories.return_value = []
    manager = RepositoryListingManager(
        golden_repo_manager=grm, activated_repo_manager=arm
    )
    setattr(manager, "get_available_branches", MagicMock(return_value=["main"]))
    return manager


class TestLocalParity:
    def test_local_backend_walks_the_clone_exactly_as_before(self, world):
        manager = _manager(world.local_meta)
        clone = str(world.local_meta)

        stats = manager.get_repository_statistics("repo-a")

        assert stats == {
            "file_count": manager._get_repository_file_count(clone),
            "index_size": manager._get_repository_index_size(clone),
            "last_updated": manager._get_repository_last_modified(clone),
        }
        assert world.backend.manifest_calls == 0

    def test_delegated_reports_what_a_local_walk_of_the_same_files_reports(self, world):
        local = _manager(world.local_meta).get_repository_statistics("repo-a")
        world.delegated()
        delegated = _manager(world.pod_meta).get_repository_statistics("repo-a")

        assert delegated == local
        assert local["file_count"] == 3


class TestDelegated:
    def test_statistics_come_from_the_manifest_never_the_pod(self, world):
        world.delegated()
        manager = _manager(world.pod_meta)

        with world.forbid_local_io():
            details = manager.get_repository_details("repo-a", "alice")

        assert details["file_count"] == 3
        assert details["index_size"] == len("print('a')\n") + 300 + len("# c\n")
        assert world.backend.manifest_calls == 1

    def test_unknown_mtimes_fall_back_to_registration_never_now(
        self, world, monkeypatch
    ):
        world.delegated()
        monkeypatch.setattr(
            file_service,
            "manifest_entries",
            lambda source, prune: (
                MagicMock(size=5, mtime_ns=None),
                MagicMock(size=7, mtime_ns=None),
            ),
        )

        stats = _manager(world.pod_meta).get_repository_statistics("repo-a")

        assert stats == {
            "file_count": 2,
            "index_size": 12,
            "last_updated": "2024-01-01T00:00:00+00:00",
        }

    def test_a_pod_local_repository_is_still_walked_on_this_pod(self, world):
        world.delegated()
        manager = _manager(world.local_meta, repo_url="local://langfuse_x")

        stats = manager.get_repository_statistics("repo-a")

        assert stats["file_count"] == 3
        assert world.backend.manifest_calls == 0


class TestFailLoud:
    def test_backend_failure_raises_instead_of_zero_files(self, world):
        world.delegated()
        world.backend.fail = RuntimeError("workspace-mcp down")
        manager = _manager(world.pod_meta)

        with pytest.raises(RepositoryStatisticsUnavailableError, match="down"):
            manager.get_repository_statistics("repo-a")

    def test_details_do_not_swallow_the_failure_into_zeros(self, world):
        world.delegated()
        world.backend.fail = RuntimeError("workspace-mcp down")
        manager = _manager(world.pod_meta)

        with pytest.raises(RepositoryStatisticsUnavailableError):
            manager.get_repository_details("repo-a", "alice")

    def test_local_failures_keep_degrading_to_zeros(self, world, monkeypatch):
        manager = _manager(world.local_meta)
        monkeypatch.setattr(
            manager,
            "_get_repository_file_count",
            MagicMock(side_effect=OSError("disk")),
        )

        details = manager.get_repository_details("repo-a", "alice")

        assert details["file_count"] == 0


def _references(name: str, *, skip: List[str]) -> List[str]:
    """Files under src/code_indexer (minus *skip*) whose code names *name*."""
    hits = []
    for path in _SRC.rglob("*.py"):
        rel = str(path.relative_to(_SRC))
        if rel in skip:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == name) or (
                isinstance(node, ast.Name) and node.id == name
            ):
                hits.append(rel)
                break
    return hits


class TestUnreachabilityGuards:
    """The guards behind this package's ``local-only`` inventory rows."""

    @pytest.mark.parametrize(
        "name, home",
        [
            # ProxyConfigManager.add_repository / validate_repositories
            ("add_repository", "proxy/config_manager.py"),
            ("validate_repositories", "proxy/config_manager.py"),
            # RepositoryStatsService.get_embedding_count -> vector_store_client
            # -> _build_vector_store_client
            ("get_embedding_count", "server/services/stats_service.py"),
        ],
    )
    def test_method_has_no_production_caller(self, name, home):
        # pydantic validators in server/multi share the name; they are not calls.
        callers = [
            rel
            for rel in _references(name, skip=[home])
            if not rel.startswith("server/multi/")
        ]
        assert callers == []

    def test_repository_prefix_formatter_is_only_exported(self):
        assert (
            _references(
                "RepositoryPrefixFormatter",
                skip=["proxy/repository_formatter.py", "proxy/__init__.py"],
            )
            == []
        )

    def test_server_spawned_indexing_never_talks_to_a_daemon(self):
        """daemon/socket_helper is reached through cli_daemon_delegation only
        when config.daemon.enabled; every server spawn of `cidx index` goes
        through enforce_server_managed_provider_settings, which turns it off."""
        from code_indexer.config import Config, DaemonConfig
        from code_indexer.server.utils.server_managed_provider_settings import (
            enforce_server_managed_provider_settings,
        )

        config = Config(daemon=DaemonConfig(enabled=True))
        enforce_server_managed_provider_settings(config)

        assert config.daemon is not None and config.daemon.enabled is False


class TestRoutesSurfaceTheFailure:
    """A backend failure reaches the REST caller as an error with its message,
    never as zero files or "not found"."""

    @pytest.fixture
    def client_and_listing(self, monkeypatch):
        from datetime import datetime, timezone

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from code_indexer.server.auth import dependencies
        from code_indexer.server.auth.user_manager import User, UserRole
        from code_indexer.server.routers import inline_repos, repo_access_http

        monkeypatch.setattr(
            repo_access_http, "enforce_repo_access", lambda *a, **k: None
        )
        listing = MagicMock()
        arm = MagicMock()
        arm.list_activated_repositories.return_value = []
        app = FastAPI()
        inline_repos.register_repo_routes(
            app,
            activated_repo_manager=arm,
            golden_repo_manager=MagicMock(),
            repository_listing_manager=listing,
            background_job_manager=MagicMock(),
        )
        app.dependency_overrides[dependencies.get_current_user] = lambda: User(
            username="alice",
            password_hash="$2b$12$x",
            role=UserRole.POWER_USER,
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        return TestClient(app), listing

    def test_general_sync_alias_resolution_is_an_error_not_not_found(
        self, client_and_listing
    ):
        client, listing = client_and_listing
        listing.get_repository_details.side_effect = (
            RepositoryStatisticsUnavailableError("workspace-mcp down")
        )

        response = client.post("/api/repos/sync", json={"repository_alias": "r"})

        assert response.status_code == 500
        assert "workspace-mcp down" in response.json()["detail"]

    def test_general_sync_unknown_alias_is_still_not_found(self, client_and_listing):
        from code_indexer.server.repositories.repository_listing_manager import (
            RepositoryListingError,
        )

        client, listing = client_and_listing
        listing.get_repository_details.side_effect = RepositoryListingError(
            "Repository 'r' not found"
        )

        response = client.post("/api/repos/sync", json={"repository_alias": "r"})

        assert response.status_code == 404

    def test_golden_details_is_an_error_with_the_message(self, client_and_listing):
        client, listing = client_and_listing
        listing.get_repository_details.side_effect = (
            RepositoryStatisticsUnavailableError("workspace-mcp down")
        )

        response = client.get("/api/repos/golden/r")

        assert response.status_code == 500
        assert "workspace-mcp down" in response.json()["detail"]
