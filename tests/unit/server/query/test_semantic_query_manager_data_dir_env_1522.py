"""Bug #1522: SemanticQueryManager's default (bare, no-arg) construction
does not honor CIDX_SERVER_DATA_DIR -- the sibling of Bug #1517, which
fixed the identical defect in services/temporal_worker.py's
run_temporal_worker.

SemanticQueryManager.__init__ falls back to
``self.activated_repo_manager = activated_repo_manager or
ActivatedRepoManager(data_dir)`` when the caller omits
``activated_repo_manager``. If ``data_dir`` is ALSO omitted (the bare
``SemanticQueryManager()`` construction), the local ``data_dir`` parameter
passed into ``ActivatedRepoManager(...)`` is None, and that constructor's
own default resolves purely from ``Path.home()`` -- never consulting
``CIDX_SERVER_DATA_DIR``, the environment variable this codebase otherwise
treats as canonical for locating the server's configured data directory
outside the normal DI chain (see ActivatedRepoIndexManager.__init__,
mcp/handlers/_legacy.py, git_operations_service.py, and now
temporal_worker.py's run_temporal_worker per Bug #1517).

On any deployment where the server's data directory differs from the OS
default, a bare SemanticQueryManager() would silently resolve its internal
GoldenRepoManager against the WRONG (OS-default) metadata store, causing
load_golden_temporal_config() to fail-open (logging a WARNING) and losing
Story #1461's "use the golden repo's own current config for embedder
selection" correctness fix.

This test deliberately pins Path.home() to an EMPTY directory while
CIDX_SERVER_DATA_DIR points at the REAL server data dir (holding the
golden repo's own, current config) -- mirroring
test_run_temporal_worker_data_dir_env_1517.py's own established,
discriminating pattern -- so a passing RED-then-GREEN genuinely proves the
env var is honored, rather than coincidentally matching Path.home() by
construction (the exact masking Bug #1517's own test file documents about
an earlier, insufficiently-discriminating test).

Real infra throughout: real GoldenRepoManager/ActivatedRepoManager, real
on-disk config.json files, real execute_temporal_query_with_fusion
dispatch. Only the two genuine external-service boundaries
(coalesced_query_embedding and TemporalSearchService.query_temporal) are
faked, matching this codebase's own established convention for these
tests (see test_semantic_query_manager_golden_temporal_config_1461.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.server.query.semantic_query_manager import SemanticQueryManager
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResults,
)


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
    activated_repo_manager's own configured data_dir -- a bare
    SemanticQueryManager() constructs its OWN fresh ActivatedRepoManager/
    GoldenRepoManager internally, so only a row persisted to disk (not
    merely an in-memory dict on a DIFFERENT manager instance) is visible to
    it."""
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


@pytest.fixture
def _capture_queried_collections(monkeypatch):
    """Real dispatch (execute_temporal_query_with_fusion), never mocked.
    Fakes only the two genuine external-service boundaries, matching
    test_semantic_query_manager_golden_temporal_config_1461.py's own
    established convention."""
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


class TestSemanticQueryManagerHonorsServerDataDirEnvVar:
    def test_bare_construction_selects_golden_embedder_when_data_dir_set_via_env_var(
        self,
        tmp_path,
        monkeypatch,
        _capture_queried_collections,
    ):
        """A bare SemanticQueryManager() that (buggy) resolves its internal
        ActivatedRepoManager purely from Path.home() can never find the
        golden repo's own current config, silently falling back to the
        stale clone config (embedder A); a manager that correctly honors
        CIDX_SERVER_DATA_DIR resolves the golden repo's OWN current config
        and selects its embedder (embedder B).
        """
        empty_home = tmp_path / "unrelated-home"
        empty_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: empty_home)

        real_server_dir = tmp_path / "configured-server-dir"
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(real_server_dir))
        data_dir = real_server_dir / "data"

        clone_dir = tmp_path / "activated-clone"
        golden_dir = data_dir / "golden-repos" / "my-repo"

        _write_config(clone_dir, "voyage-code-3")  # stale clone: embedder A
        _write_config(golden_dir, "voyage-large-2")  # golden NOW: embedder B
        (
            clone_dir
            / ".code-indexer"
            / "index"
            / "code-indexer-temporal-voyage_large_2-2024Q1"
        ).mkdir(parents=True)

        # Bare construction -- NEITHER data_dir NOR activated_repo_manager
        # provided. This is the exact defect: SemanticQueryManager.__init__
        # must resolve its own default data_dir from CIDX_SERVER_DATA_DIR,
        # not silently default to Path.home().
        manager = SemanticQueryManager()
        _register_golden_repo(manager.activated_repo_manager, "my-repo", golden_dir)

        manager._execute_temporal_query(
            repo_path=clone_dir,
            repository_alias="my-repo-activated",
            query_text="auth logic",
            limit=10,
            min_score=None,
            time_range=None,
            time_range_all=True,
            golden_repo_alias="my-repo",
        )

        assert len(_capture_queried_collections) == 1
        assert "voyage_large_2" in _capture_queried_collections[0]
        assert "voyage_code_3" not in _capture_queried_collections[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
