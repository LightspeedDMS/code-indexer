"""REST /api/query's temporal front door must read the FIXED temporal root.

Originally Bug #1482 (REOPENED): the REST door's live temporal dispatch did
not thread query_tracker into execute_live_temporal_search, so
run_temporal_worker's resolver gate never fired and discovery fell back to an
in-repo scan -- returning 0 results for a golden repo whose temporal data
lived elsewhere.

Bug #1529 removed the resolver entirely (a shard's path is now fixed from
first creation), so the resolver/query_tracker gate no longer exists. The
USER-FACING contract this test guards is unchanged and still worth guarding:
a REST temporal query against a golden repo whose data lives at the fixed
server-owned root must return that data, never zero results because the read
was rooted at the repo's own tree.

This test reproduces the defect end-to-end with REAL infrastructure: a real
BackgroundJobManager (temporal lane), real PayloadCache, real QueryTracker,
real AliasManager, and a real ChunkStore/HNSW-backed shard at the fixed
temporal root, and drives the query through the ACTUAL production REST route
(POST /api/query -> register_query_routes's closure ->
_execute_temporal_via_live_dispatch_rest -> execute_live_temporal_search ->
run_temporal_worker -> execute_temporal_query_with_fusion) via a real
FastAPI TestClient. Only the two genuine external-service/network
boundaries -- the up-front embedding reuse-seam
(coalesced_query_embedding/_create_embedding_provider_for_collection) and
the innermost per-shard TemporalSearchService call (_query_single_provider)
-- are faked. Discovery, path resolution, the AliasManager registration and
the ChunkStore/HNSW-backed shard are all real and never mocked.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.routers.inline_query import register_query_routes
from code_indexer.server.services.config_service import (
    ConfigService,
    reset_config_service,
    set_config_service,
)
from code_indexer.server.services.maintenance_service import (
    _reset_maintenance_state,
    get_maintenance_state,
)
from code_indexer.server.storage.postgres.temporal_child_wiring import (
    CIDX_SERVER_REFRESH_CONTEXT_ENV,
)
from code_indexer.server.utils.config_manager import BackgroundJobsConfig
from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchResults,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

REPO_ALIAS = "evolution"
EMBEDDER = "voyage-context-4"
SHARD_NAME = "code-indexer-temporal-voyage_context_4-2024Q1"


def _make_user() -> User:
    return User(
        username="alice",
        password_hash="irrelevant",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


def _build_fixed_root_shard(index_root: Path, point_id: str) -> Path:
    """Build a REAL consolidated (chunks.db) temporal shard at the FIXED
    server-owned root -- the location Bug #1529's write path targets."""
    store = FilesystemVectorStore(
        base_path=index_root, use_chunks_db_for_new_collections=True
    )
    store.create_collection(SHARD_NAME, vector_size=4)
    store.begin_indexing(SHARD_NAME)
    store.upsert_points(
        SHARD_NAME,
        [
            {
                "id": point_id,
                "vector": [0.11, 0.22, 0.33, 0.44],
                "payload": {
                    "commit_hash": "c0",
                    "path": "src/auth.py",
                    "content": "def authenticate(user): ...",
                },
            }
        ],
    )
    store.end_indexing(SHARD_NAME)
    return index_root / SHARD_NAME


def _write_golden_repo_config(codebase_dir: Path) -> None:
    (codebase_dir / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
    cfg = {
        "codebase_dir": str(codebase_dir),
        "embedding_provider": "voyage-ai",
        "voyage_ai": {"model": EMBEDDER},
        "temporal": {
            "embedders": [EMBEDDER],
            "active_embedder": EMBEDDER,
        },
    }
    (codebase_dir / ".code-indexer" / "config.json").write_text(json.dumps(cfg))


class _FakeAccessFilteringService:
    """Real-shape stand-in matching test_temporal_live_dispatch_1400.py's
    own double: implements filter_query_results(results, user_id) exactly
    like the real AccessFilteringService, admin bypass included. This is
    NOT mocking the code under test -- access control filtering is an
    orthogonal concern to the resolver/discovery mechanism this test
    proves, and this codebase's own test suite uses the identical double
    for the identical reason."""

    def __init__(self, admins=None):
        self._admins = admins or set()

    def is_admin_user(self, user_id):
        return user_id in self._admins

    def filter_query_results(self, results, user_id):
        return results  # this suite's single user is always authorized

    def calculate_over_fetch_limit(self, requested_limit):
        return requested_limit * 2  # matches the real DEFAULT_OVER_FETCH_FACTOR


_STUB_CALL_COLLECTIONS: list = []


def _stub_query_single_provider(cfg, vs_, coll_name, query_text, *a, **kw):
    """Real-content-bearing stand-in for the innermost per-shard query --
    only the network-bound embedding/HNSW-search internals are faked; the
    result carries the SAME row the real sister-relocated chunks.db holds,
    proving genuine relocated data reaches the response when this function
    is even invoked at all. Whether it is invoked (>=1 time) or never
    invoked (0 times) is exactly the discovery-level behavior this bug
    controls."""
    _STUB_CALL_COLLECTIONS.append(coll_name)
    return TemporalSearchResults(
        results=[
            TemporalSearchResult(
                file_path="src/auth.py",
                chunk_index=0,
                content="def authenticate(user): ...",
                score=0.91,
                metadata={"commit_hash": "c0"},
                temporal_context={"commit_hash": "c0"},
            )
        ],
        query=query_text,
        filter_type="none",
        filter_value=None,
        total_found=1,
    )


@pytest.fixture(autouse=True)
def _clean_maintenance_state():
    """Mirrors test_temporal_live_dispatch_1400.py's fixture: guarantees the
    real, process-wide MaintenanceState singleton is never left active
    across tests in this shared pytest process."""
    _reset_maintenance_state()
    yield
    get_maintenance_state().exit_maintenance_mode()
    _reset_maintenance_state()


@pytest.fixture
def _sandboxed_temporal_repo(tmp_path, monkeypatch):
    """Builds a real golden repo whose temporal quarter shard lives ONLY at
    the fixed server-owned root ({golden_repos_dir}/.temporal/{alias}/), with
    the clone's own .code-indexer/index/ deliberately holding no temporal
    shard at all -- so a read rooted at the repo tree finds nothing and the
    Bug #1482 zero-results symptom would reappear.

    Sandboxes ActivatedRepoManager's default (Path.home()-derived) data_dir
    entirely inside tmp_path via the HOME env var, so run_temporal_worker's
    own internal `ActivatedRepoManager()` (unconditional, un-injectable)
    construction never touches the real developer machine's
    ~/.cidx-server state.

    Bug #1522: also explicitly pins CIDX_SERVER_DATA_DIR to the SAME
    tmp_path-derived directory HOME's fallback would produce. Without
    this, run_temporal_worker's construction (fixed by Bug #1517 to prefer
    CIDX_SERVER_DATA_DIR over Path.home() when the env var is set) silently
    diverges from this fixture's own data_dir whenever CIDX_SERVER_DATA_DIR
    happens to be set in the ambient shell environment -- exactly what
    server-fast-automation.sh's per-chunk isolation does (each parallel
    chunk sets its own CIDX_SERVER_DATA_DIR for the whole pytest
    invocation), which is why this test failed reliably under that script
    but never under a bare `pytest <this file>` invocation (where the var
    is normally unset and the two resolutions coincidentally agree).
    Pinning both env vars in lockstep removes that ambient dependency.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / ".cidx-server"))
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")

    real_config_service = ConfigService(server_dir_path=str(tmp_path / "cfgsvc"))
    set_config_service(real_config_service)

    data_dir = tmp_path / ".cidx-server" / "data"
    golden_repos_dir = data_dir / "golden-repos"
    codebase_dir = golden_repos_dir / REPO_ALIAS

    _write_golden_repo_config(codebase_dir)

    # Bug #1529: the shard is built at the FIXED server-owned root, outside
    # the golden repo's own cloned tree -- exactly where the write path puts
    # it. The clone's own .code-indexer/index/ deliberately holds NO temporal
    # shard, so a read that fell back to the in-repo scan would find zero
    # shards and return zero results (the Bug #1482 symptom this test still
    # guards, now against the fixed-path mechanism).
    _build_fixed_root_shard(
        server_temporal_index_root(golden_repos_dir, REPO_ALIAS),
        "evolution:commit:c0:0",
    )

    # Register the GOLDEN-REPO alias ("evolution-global" -> clone path).
    aliases_dir = golden_repos_dir / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    alias_manager.create_alias(f"{REPO_ALIAS}-global", str(codebase_dir))

    yield {
        "golden_repos_dir": golden_repos_dir,
        "codebase_dir": codebase_dir,
    }

    reset_config_service()


@pytest.fixture
def _app_and_client(_sandboxed_temporal_repo):
    app = FastAPI()

    class _UnusedSemanticQueryManager:
        pass

    class _UnusedActivatedRepoManager:
        activated_repos_dir = "/unused"

    register_query_routes(
        app,
        semantic_query_manager=_UnusedSemanticQueryManager(),
        activated_repo_manager=_UnusedActivatedRepoManager(),
    )
    app.dependency_overrides[dependencies.get_current_user] = _make_user

    bgm = BackgroundJobManager(
        storage_path=str(
            Path(_sandboxed_temporal_repo["golden_repos_dir"]).parent / "jobs.json"
        ),
        background_jobs_config=BackgroundJobsConfig(temporal_lane_concurrency=2),
    )
    payload_cache = PayloadCache(
        db_path=Path(_sandboxed_temporal_repo["golden_repos_dir"]).parent
        / "payload_cache.db",
        config=PayloadCacheConfig(),
    )
    payload_cache.initialize()

    app.state.background_job_manager = bgm
    app.state.payload_cache = payload_cache
    app.state.golden_repos_dir = str(_sandboxed_temporal_repo["golden_repos_dir"])
    app.state.query_tracker = QueryTracker()
    app.state.access_filtering_service = _FakeAccessFilteringService()

    client = TestClient(app)
    yield client
    bgm.shutdown()
    payload_cache.close()


def _temporal_payload(**overrides):
    base = {
        "query_text": "authenticate user",
        "repository_alias": f"{REPO_ALIAS}-global",
        "time_range_all": True,
        "limit": 10,
    }
    base.update(overrides)
    return base


class TestRestTemporalQueryFindsFixedRootData:
    """Bug #1482's regression guard, re-pointed by Bug #1529: the REST
    temporal front door must return rows from the FIXED server-owned root,
    never zero results because it looked in the repo's own tree."""

    def test_temporal_query_returns_fixed_root_rows_not_zero_results(
        self, _app_and_client
    ):
        _STUB_CALL_COLLECTIONS.clear()
        with (
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch"
                ".coalesced_query_embedding",
                side_effect=lambda *a, **k: ([0.11, 0.22, 0.33, 0.44], {}),
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch"
                "._create_embedding_provider_for_collection",
                return_value=object(),
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch"
                "._query_single_provider",
                side_effect=_stub_query_single_provider,
            ),
        ):
            response = _app_and_client.post("/api/query", json=_temporal_payload())

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["results"], (
            "expected the fixed-root temporal row back -- got 0 results "
            f"(the exact Bug #1482 symptom). Full body: {body}"
        )
        assert body["total_results"] == 1
        assert body["results"][0]["file_path"] == "src/auth.py"
        # Proves discovery actually reached the FIXED-root shard -- the
        # clone's own index dir holds no temporal shard at all, so an
        # in-repo-rooted read could never have found one.
        assert _STUB_CALL_COLLECTIONS, (
            "the per-shard query function was never invoked -- discovery "
            "found zero shards, so the read was not rooted at the fixed "
            "temporal location"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
