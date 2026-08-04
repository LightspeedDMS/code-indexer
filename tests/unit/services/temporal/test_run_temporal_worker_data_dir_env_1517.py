"""Bug #1517: run_temporal_worker's golden-repo-alias resolution for
temporal embedder selection constructs a bare, no-arg
``ActivatedRepoManager()`` internally. That constructor's OWN default
(``Path.home() / ".cidx-server" / "data"``) never consults
``CIDX_SERVER_DATA_DIR`` -- the environment variable this codebase
otherwise treats as the canonical way to locate the server's configured
data directory when building a standalone service object outside the
normal DI chain (see e.g. ActivatedRepoIndexManager.__init__,
mcp/handlers/_legacy.py, git_operations_service.py).

On any real deployment where the server's data directory does not equal
the OS default (a common production/staging configuration, and the
documented reason CIDX_SERVER_DATA_DIR exists at all -- Bug #879), this
worker's internal ActivatedRepoManager() looks in the WRONG directory for
the golden repo, so GoldenRepoManager.get_actual_repo_path() -- correctly
looking for a repo that genuinely is NOT registered under whatever
Path.home() happens to resolve to in this process -- raises
GoldenRepoNotFoundError. load_golden_temporal_config() swallows this
(fail-open) and logs a WARNING, and the golden repo's OWN current
config.temporal is silently never applied -- exactly the log noise +
embedder-selection correctness gap Bug #1517 reports.

Story #1461's own MCP-path test (test_run_temporal_worker_golden_config_
1461.py) worked around this exact divergence by monkeypatching
``Path.home()`` so its test setup and the worker's internal construction
coincidentally agree -- masking the very bug this file reproduces and
fixes. This test instead sets CIDX_SERVER_DATA_DIR to one directory while
leaving Path.home() pointed at a DIFFERENT, deliberately-empty directory,
so a discriminating RED only passes once the worker actually honors
CIDX_SERVER_DATA_DIR (rather than happening to match Path.home() by
construction).

Real infra throughout: real GoldenRepoManager/ActivatedRepoManager, real
on-disk metadata + config.json files, real execute_temporal_query_with_
fusion dispatch. Only TemporalSearchService.query_temporal and the
coalesced_query_embedding reuse seam (genuine external-service
boundaries) are faked, matching this codebase's own established
convention for these tests.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.services.temporal_snapshot_store import (
    read_temporal_snapshot,
)
from code_indexer.server.services.temporal_worker import run_temporal_worker
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResults,
)
from code_indexer.services.temporal.temporal_worker_input import TemporalWorkerInput


def _write_config(repo_dir: Path, active_embedder: str) -> None:
    (repo_dir / ".code-indexer").mkdir(parents=True, exist_ok=True)
    cfg = {
        "codebase_dir": str(repo_dir),
        "embedding_provider": "voyage-ai",
        "voyage_ai": {"model": active_embedder},
        "temporal": {
            "embedders": [active_embedder],
            "active_embedder": active_embedder,
        },
    }
    (repo_dir / ".code-indexer" / "config.json").write_text(json.dumps(cfg))


def _register_golden_repo(activated_repo_manager, alias: str, golden_dir: Path):
    """Persist the golden repo to the SHARED SQLite backend at
    activated_repo_manager's own configured data_dir -- run_temporal_worker
    constructs its OWN fresh ActivatedRepoManager()/GoldenRepoManager
    internally, so only a row persisted to disk (not merely the in-memory
    cache) is visible to it."""
    golden_repo_manager = activated_repo_manager.golden_repo_manager
    golden_repo = GoldenRepo(
        alias=alias,
        repo_url=f"local://{golden_dir}",
        default_branch="master",
        clone_path=str(golden_dir),
        created_at="2025-01-01T00:00:00Z",
        enable_temporal=True,
        temporal_options=None,
    )
    golden_repo_manager.golden_repos[alias] = golden_repo
    golden_repo_manager._sqlite_backend.add_repo(
        alias=golden_repo.alias,
        repo_url=golden_repo.repo_url,
        default_branch=golden_repo.default_branch,
        clone_path=golden_repo.clone_path,
        created_at=golden_repo.created_at,
        enable_temporal=golden_repo.enable_temporal,
        temporal_options=golden_repo.temporal_options,
    )


def _activate_repo(
    activated_repo_manager: ActivatedRepoManager,
    username: str,
    user_alias: str,
    golden_repo_alias: str,
    repo_path: Path,
) -> None:
    """Write real activated-repo metadata + a real clone dir on disk, the
    SAME on-disk shape ActivatedRepoManager.get_repository() reads."""
    repo_path.mkdir(parents=True, exist_ok=True)
    activated_repo_manager._save_metadata_file(
        username,
        user_alias,
        {
            "user_alias": user_alias,
            "golden_repo_alias": golden_repo_alias,
            "current_branch": "master",
            "activated_at": "2025-01-01T00:00:00Z",
            "last_accessed": "2025-01-01T00:00:00Z",
        },
    )


@pytest.fixture
def payload_cache(tmp_path):
    db_path = tmp_path / "payload_cache.db"
    cache = PayloadCache(db_path=db_path, config=PayloadCacheConfig())
    cache.initialize()
    yield cache
    cache.close()


@pytest.fixture
def _capture_queried_collections(monkeypatch):
    """Real dispatch, never mocked. Fakes only the two genuine external-
    service boundaries: the up-front query-embedding reuse seam and the
    per-shard TemporalSearchService.query_temporal (real embedding + HNSW
    read) -- matching test_temporal_fusion_dispatch.py's own convention."""
    captured: list = []

    def _fake_query_temporal(self, **kwargs):
        captured.append(self.collection_name)
        return TemporalSearchResults(
            results=[], query="auth", filter_type="none", filter_value=None
        )

    monkeypatch.setattr(
        "code_indexer.services.temporal.temporal_fusion_dispatch.coalesced_query_embedding",
        None,
    )
    monkeypatch.setattr(
        "code_indexer.services.temporal.temporal_search_service."
        "TemporalSearchService.query_temporal",
        _fake_query_temporal,
    )
    return captured


def _make_worker_input(repo_path: Path, **overrides) -> TemporalWorkerInput:
    base = dict(
        repo_path=str(repo_path),
        repository_alias="my-clone",
        username="alice",
        query_text="auth logic",
        requested_limit=10,
        fusion_fetch_limit=30,
        time_range=("0001-01-01", "9999-12-31"),
        time_range_raw=None,
        time_range_all=True,
        file_path_filter=None,
        provider_filter=None,
        at_commit=None,
        language=None,
        exclude_language=None,
        exclude_path=None,
        diff_types=None,
        author=None,
        chunk_type=None,
        no_embedding_cache_shortcut=False,
        temporal_embedder=None,
        rerank_query=None,
        rerank_instruction=None,
        min_score_ignored_for_temporal=None,
        file_extensions_ignored_for_temporal=None,
    )
    base.update(overrides)
    return TemporalWorkerInput(**base)


@dataclass
class _ScenarioContext:
    worker_input: TemporalWorkerInput


def _build_split_home_and_server_dir_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _ScenarioContext:
    """Pin Path.home() to a directory with NO golden-repos metadata at all,
    and separately point CIDX_SERVER_DATA_DIR at the REAL server data dir
    (holding the golden repo + activated clone). A worker that ignores the
    env var and falls back to Path.home() can never find the golden repo.
    """
    empty_home = tmp_path / "unrelated-home"
    empty_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: empty_home)

    real_server_dir = tmp_path / "configured-server-dir"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(real_server_dir))
    data_dir = real_server_dir / "data"
    golden_dir = data_dir / "golden-repos" / "my-repo"

    activated_repo_manager = ActivatedRepoManager(data_dir=str(data_dir))
    clone_dir = Path(
        activated_repo_manager.get_activated_repo_path("alice", "my-clone")
    )

    _write_config(clone_dir, "voyage-code-3")  # stale clone: embedder A
    _write_config(golden_dir, "voyage-large-2")  # golden NOW: embedder B
    (
        clone_dir
        / ".code-indexer"
        / "index"
        / "code-indexer-temporal-voyage_large_2-2024Q1"
    ).mkdir(parents=True)

    _register_golden_repo(activated_repo_manager, "my-repo", golden_dir)
    _activate_repo(activated_repo_manager, "alice", "my-clone", "my-repo", clone_dir)

    return _ScenarioContext(worker_input=_make_worker_input(clone_dir))


def _build_global_alias_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _ScenarioContext:
    """Direct golden-repo ('-global') temporal query: repo_path IS the
    golden repo's own clone (no separate activated clone). Path.home() is
    pinned to an empty directory; CIDX_SERVER_DATA_DIR points at the real
    server data dir holding the golden repo's metadata row. A worker that
    ignores the env var calls GoldenRepoManager.get_actual_repo_path()
    against the WRONG (empty) metadata store, which raises
    GoldenRepoNotFoundError -- exactly the log line Bug #1517 reports.
    """
    empty_home = tmp_path / "unrelated-home"
    empty_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: empty_home)

    real_server_dir = tmp_path / "configured-server-dir"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(real_server_dir))
    data_dir = real_server_dir / "data"
    golden_dir = data_dir / "golden-repos" / "my-repo"

    _write_config(golden_dir, "voyage-large-2")
    (
        golden_dir
        / ".code-indexer"
        / "index"
        / "code-indexer-temporal-voyage_large_2-2024Q1"
    ).mkdir(parents=True)

    activated_repo_manager = ActivatedRepoManager(data_dir=str(data_dir))
    _register_golden_repo(activated_repo_manager, "my-repo", golden_dir)

    return _ScenarioContext(
        worker_input=_make_worker_input(golden_dir, repository_alias="my-repo-global")
    )


class TestRunTemporalWorkerHonorsServerDataDirEnvVar:
    def test_selects_golden_embedder_when_data_dir_set_via_env_var(
        self,
        tmp_path,
        payload_cache,
        _capture_queried_collections,
        monkeypatch,
    ):
        """A worker that (buggy) constructs ActivatedRepoManager() with no
        args resolves against Path.home() and can never find the golden
        repo's own current config, silently falling back to the stale
        clone config (embedder A); a worker that correctly honors
        CIDX_SERVER_DATA_DIR resolves the golden repo's OWN current config
        and selects its embedder (embedder B).
        """
        scenario = _build_split_home_and_server_dir_scenario(tmp_path, monkeypatch)

        run_temporal_worker(scenario.worker_input, payload_cache, job_id="job-1517")

        assert len(_capture_queried_collections) == 1
        assert "voyage_large_2" in _capture_queried_collections[0]
        assert "voyage_code_3" not in _capture_queried_collections[0]

        snapshot = read_temporal_snapshot(payload_cache, "job-1517")
        assert snapshot["terminal"] is True

    def test_no_golden_repo_not_found_error_logged_for_global_alias(
        self,
        tmp_path,
        payload_cache,
        _capture_queried_collections,
        monkeypatch,
        caplog,
    ):
        """Reproduces the literal log line from Bug #1517: querying a real
        temporal-indexed golden repo via its '-global' alias must never log
        `GoldenRepoNotFoundError: Golden repository '<alias>' not found in
        metadata` -- that error only fires when embedder-selection alias
        resolution looks in the wrong (env-var-ignoring) metadata store.
        """
        scenario = _build_global_alias_scenario(tmp_path, monkeypatch)

        with caplog.at_level(logging.WARNING):
            run_temporal_worker(
                scenario.worker_input, payload_cache, job_id="job-1517b"
            )

        assert "GoldenRepoNotFoundError" not in caplog.text, (
            f"GoldenRepoNotFoundError was logged during embedder-selection "
            f"alias resolution: {caplog.text}"
        )

        # Fail-open correctness is preserved: the query still returns results.
        assert len(_capture_queried_collections) == 1
        assert "voyage_large_2" in _capture_queried_collections[0]
