"""Bug #1482 (REOPENED) -- REST /api/query's live temporal dispatch never
threaded query_tracker into execute_live_temporal_search.

Root cause: both search_code (MCP) and POST /api/query (REST) route
temporal queries through the SAME shared function,
execute_live_temporal_search -> BGM lane="temporal" job ->
run_temporal_worker. run_temporal_worker only constructs a real
TemporalShardResolver (and therefore only ever consults a golden repo's
sister-relocated temporal shards, Story #1457) when BOTH a golden_repo_alias
is known AND a real query_tracker is supplied to it.

mcp/handlers/search.py's _execute_temporal_via_live_dispatch already passes
query_tracker=_get_query_tracker() (fixed for MCP by commit 7ae9b9bb). REST's
sibling call site, server/routers/inline_query.py's
_execute_temporal_via_live_dispatch_rest, called execute_live_temporal_search
WITHOUT a query_tracker kwarg at all -- so run_temporal_worker's resolver
gate (`if golden_repo_alias and query_tracker is not None`) always failed
for the REST door, forcing discovery back onto the legacy in-repo iterdir()
scan. Once a golden repo's temporal shards are relocated to the sister
location (Story #1457 AC1), that in-repo directory is blanked and REST
temporal queries silently return 0 results -- exactly the symptom captured
live on solo staging in the bug's REOPENING comment.

This test reproduces the defect end-to-end with REAL infrastructure: a real
BackgroundJobManager (temporal lane), real PayloadCache, real QueryTracker,
real AliasManager, real TemporalShardResolver, real ChunkStore/HNSW-backed
sister-relocated shard (built via the SAME production relocation trigger,
maybe_relocate_shard_to_sister_location, Story #1457's real write path), and
drives the query through the ACTUAL production REST route
(POST /api/query -> register_query_routes's closure ->
_execute_temporal_via_live_dispatch_rest -> execute_live_temporal_search ->
run_temporal_worker -> execute_temporal_query_with_fusion) via a real
FastAPI TestClient. Only the two genuine external-service/network
boundaries -- the up-front embedding reuse-seam
(coalesced_query_embedding/_create_embedding_provider_for_collection) and
the innermost per-shard TemporalSearchService call (_query_single_provider)
-- are faked, matching this codebase's own established
test_temporal_fusion_dispatch_resolver_e2e_1457.py convention. The resolver,
pin, discovery, AliasManager, and ChunkStore/HNSW-backed sister publish are
all real and never mocked.
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
from code_indexer.services.temporal.temporal_relocation_trigger import (
    maybe_relocate_shard_to_sister_location,
)
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchResults,
)

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


def _write_local_shard_row(shard_dir: Path, hash_prefix: str, point_id: str) -> None:
    shard_dir.mkdir(parents=True, exist_ok=True)
    row = {
        "id": point_id,
        "vector": [0.11, 0.22, 0.33, 0.44],
        "payload": {
            "commit_hash": "c0",
            "path": "src/auth.py",
            "content": "def authenticate(user): ...",
        },
    }
    (shard_dir / f"vector_{hash_prefix}.json").write_text(json.dumps(row))


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
    """Builds a real golden repo whose temporal quarter shard has been
    RELOCATED to the sister location via Story #1457's actual production
    write-path trigger (maybe_relocate_shard_to_sister_location), then
    blanks the in-repo shard directory -- reproducing the exact on-disk
    state captured live in the bug's reopening comment (in-repo directory
    emptied, data queryable only via the sister pointer).

    Sandboxes ActivatedRepoManager's default (Path.home()-derived) data_dir
    entirely inside tmp_path via the HOME env var, so run_temporal_worker's
    own internal `ActivatedRepoManager()` (unconditional, un-injectable)
    construction never touches the real developer machine's
    ~/.cidx-server state.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    monkeypatch.setenv("CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED", "1")

    real_config_service = ConfigService(server_dir_path=str(tmp_path / "cfgsvc"))
    set_config_service(real_config_service)

    data_dir = tmp_path / ".cidx-server" / "data"
    golden_repos_dir = data_dir / "golden-repos"
    codebase_dir = golden_repos_dir / REPO_ALIAS
    local_shard_dir = codebase_dir / ".code-indexer" / "index" / SHARD_NAME

    _write_golden_repo_config(codebase_dir)
    _write_local_shard_row(local_shard_dir, "aaaa1111", "evolution:commit:c0:0")

    # Story #1457 AC1's REAL relocation trigger -- builds+publishes the
    # SAME data to the sister location.
    maybe_relocate_shard_to_sister_location(
        codebase_dir=codebase_dir,
        shard_name=SHARD_NAME,
        local_shard_dir=local_shard_dir,
        new_commit_hashes=["c0"],
        vector_dim=4,
    )

    # Blank the in-repo shard dir -- matches the reopening comment's
    # captured on-disk state exactly (relocation succeeded, in-repo tree
    # emptied).
    import shutil

    shutil.rmtree(local_shard_dir)

    # Register the GOLDEN-REPO alias ("evolution-global" -> clone path) --
    # a SEPARATE namespace from the sister temporal-shard pointer, but the
    # SAME aliases directory (matches maybe_relocate_shard_to_sister_
    # location's own AliasManager construction).
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


class TestRestTemporalQueryFindsSisterRelocatedData:
    """The failing-then-passing regression test for Bug #1482 (REOPENED)."""

    def test_temporal_query_returns_relocated_rows_not_zero_results(
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
            "expected the sister-relocated temporal row back -- got 0 "
            f"results (the exact Bug #1482 symptom). Full body: {body}"
        )
        assert body["total_results"] == 1
        assert body["results"][0]["file_path"] == "src/auth.py"
        # Proves discovery actually reached the sister-published shard
        # (never possible via the legacy in-repo scan, since the in-repo
        # directory was deliberately blanked above).
        assert _STUB_CALL_COLLECTIONS, (
            "the per-shard query function was never invoked -- discovery "
            "found zero shards, meaning the resolver was never consulted"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
