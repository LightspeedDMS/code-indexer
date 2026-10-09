"""AccessFilteringService.filter_query_results over the caller's activations.

public #1984: a row from the caller's own activation is kept when the caller
is granted the golden repository the activation was created from, whatever
alias the activation uses. Invariants covered here:

- The activation mapping reads ONLY the caller's own activations.
- A row from an activation whose source grant is gone is filtered, even when
  the activation alias equals another granted golden repository name.
- An activation alias that collides with a golden repository name (bare or
  ``-global``) never grants that golden repository's rows.

Real services throughout (query_repo_access_env): activation metadata on
disk written by the real ActivatedRepoManager, grants in a real
GroupAccessManager.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Set
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.temporal_poll_postprocessor import (
    postprocess_temporal_snapshot,
)
from code_indexer.server.services.temporal_snapshot_store import (
    read_temporal_snapshot,
)
from code_indexer.server.services.temporal_worker import run_temporal_worker
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchResults,
)
from code_indexer.services.temporal.temporal_worker_input import TemporalWorkerInput
from tests.unit.server.query.query_repo_access_env import (
    ADMIN,
    ALL_ROWS_LIMIT,
    GRANTED_REPO,
    GRANTED_REPO_2,
    OWN_ACTIVATION,
    UNGRANTED_REPO,
    USER,
    QueryAccessEnv,
    build_server_db_template,
    global_alias,
)

_SERVICE_LOGGER = "code_indexer.server.services.access_filtering_service"
OTHER_USER = "example_other_user"
COMPOSITE_ALIAS = "my-composite"


@pytest.fixture(scope="module")
def server_db_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_server_db_template(tmp_path_factory.mktemp("server_db_template"))


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_db_template: Path
) -> Iterator[QueryAccessEnv]:
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    e = QueryAccessEnv(tmp_path, server_db_template)
    try:
        yield e
    finally:
        e.close()


def _rows(*aliases: str) -> List[Dict[str, Any]]:
    return [{"repository_alias": a, "file_path": f"src/{a}.py"} for a in aliases]


def _kept(env: QueryAccessEnv, user: str, *aliases: str) -> Set[str]:
    filtered = env.access_service.filter_query_results(_rows(*aliases), user)
    return {r["repository_alias"] for r in filtered}


class TestOwnActivationRows:
    def test_custom_alias_activation_of_granted_repo_is_kept(self, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        assert _kept(env, USER, OWN_ACTIVATION) == {OWN_ACTIVATION}

    def test_default_alias_activation_is_kept(self, env):
        env.activate_for(USER, GRANTED_REPO, GRANTED_REPO)

        assert _kept(env, USER, GRANTED_REPO) == {GRANTED_REPO}

    def test_revoked_source_is_filtered_even_under_a_granted_golden_name(self, env):
        # The activation alias equals a DIFFERENT, still-granted golden repo.
        env.group_manager.grant_repo_access(UNGRANTED_REPO, env.group_id, "test")
        env.activate_for(USER, UNGRANTED_REPO, GRANTED_REPO_2)
        assert _kept(env, USER, GRANTED_REPO_2) == {GRANTED_REPO_2}

        env.group_manager.revoke_repo_access(UNGRANTED_REPO, env.group_id)

        assert _kept(env, USER, GRANTED_REPO_2) == set()
        # The granted golden repo's own global rows are unaffected.
        assert _kept(env, USER, global_alias(GRANTED_REPO_2)) == {
            global_alias(GRANTED_REPO_2)
        }

    def test_revoked_source_is_filtered_under_a_custom_alias(self, env):
        env.group_manager.grant_repo_access(UNGRANTED_REPO, env.group_id, "test")
        env.activate_for(USER, UNGRANTED_REPO, OWN_ACTIVATION)

        env.group_manager.revoke_repo_access(UNGRANTED_REPO, env.group_id)

        assert _kept(env, USER, OWN_ACTIVATION) == set()

    def test_another_users_activation_alias_never_maps_for_the_caller(self, env):
        env.activate_for(OTHER_USER, GRANTED_REPO, "their-repo")

        assert _kept(env, USER, "their-repo") == set()

    def test_global_rows_are_unaffected_by_activations(self, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        kept = _kept(
            env, USER, global_alias(GRANTED_REPO), global_alias(UNGRANTED_REPO)
        )

        assert kept == {global_alias(GRANTED_REPO)}

    def test_admin_sees_every_row(self, env):
        aliases = (OWN_ACTIVATION, global_alias(UNGRANTED_REPO), UNGRANTED_REPO)

        assert _kept(env, ADMIN, *aliases) == set(aliases)


class TestCompositeActivation:
    def _activate_composite(self, env: QueryAccessEnv) -> None:
        job_id = env.activated_repo_manager.activate_repository(
            username=USER,
            golden_repo_aliases=[GRANTED_REPO, GRANTED_REPO_2],
            user_alias=COMPOSITE_ALIAS,
        )
        status = env.wait_for_job(job_id, USER)
        assert status["status"] == "completed", status

    def test_kept_while_every_component_is_granted(self, env):
        self._activate_composite(env)

        rows = [
            {"repository_alias": COMPOSITE_ALIAS, "source_repo": GRANTED_REPO},
            {"repository_alias": COMPOSITE_ALIAS, "source_repo": GRANTED_REPO_2},
        ]
        filtered = env.access_service.filter_query_results(rows, USER)

        assert filtered == rows

    def test_filtered_once_any_component_grant_is_revoked(self, env):
        self._activate_composite(env)
        env.group_manager.revoke_repo_access(GRANTED_REPO_2, env.group_id)

        assert _kept(env, USER, COMPOSITE_ALIAS) == set()


class _CountingActivations:
    """Delegates to the real manager, counting activation lookups."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.golden_repo_manager = real.golden_repo_manager
        self.calls = 0

    def list_activated_repositories(self, username: str) -> List[Dict[str, Any]]:
        self.calls += 1
        result: List[Dict[str, Any]] = self._real.list_activated_repositories(username)
        return result


class _CountingGoldenRepos:
    """Delegates to the real golden repo manager, counting every lookup."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.lookups = 0
        self.full_listings = 0
        self.requested: List[List[str]] = []

    def golden_repo_exists(self, alias: str) -> bool:
        self.lookups += 1
        return bool(self._real.golden_repo_exists(alias))

    def list_golden_repos(self) -> List[Dict[str, Any]]:
        self.lookups += 1
        self.full_listings += 1
        repos: List[Dict[str, Any]] = self._real.list_golden_repos()
        return repos

    def existing_golden_aliases(self, names: List[str]) -> Set[str]:
        self.lookups += 1
        self.requested.append(sorted(names))
        found: Set[str] = self._real.existing_golden_aliases(names)
        return found


class TestCollisionLookupCost:
    def test_one_bounded_golden_lookup_per_call_whatever_the_activation_count(
        self, env
    ):
        # Two custom aliases plus one equal to a golden repository name.
        aliases = ["alias-one", "alias-two", GRANTED_REPO_2]
        for alias in aliases:
            env.activate_for(USER, GRANTED_REPO, alias)
        counting = _CountingActivations(env.activated_repo_manager)
        golden = _CountingGoldenRepos(env.golden_repo_manager)
        counting.golden_repo_manager = golden
        service = AccessFilteringService(
            env.group_manager,
            activated_repo_manager=counting,  # type: ignore[arg-type]
        )

        filtered = service.filter_query_results(_rows(*aliases), USER)
        assert {r["repository_alias"] for r in filtered} == set(aliases)
        assert golden.lookups == 1
        assert golden.full_listings == 0
        # Bounded to the caller's own activation aliases only.
        assert golden.requested == [sorted(aliases)]

        service.filter_query_results(_rows(*aliases), USER)
        # No cross-request caching: the next call looks up again, once.
        assert golden.lookups == 2
        assert golden.full_listings == 0

    def test_colliding_alias_is_still_recognised_by_the_bounded_lookup(self, env):
        # The activation of a granted source is named like ANOTHER golden
        # repo whose grant is gone: its rows (no provenance) need both.
        env.activate_for(USER, GRANTED_REPO, GRANTED_REPO_2)
        env.group_manager.revoke_repo_access(GRANTED_REPO_2, env.group_id)

        assert _kept(env, USER, GRANTED_REPO_2) == set()


class TestLookupCostAndWiring:
    def test_one_activation_lookup_per_call_not_per_row(self, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        counting = _CountingActivations(env.activated_repo_manager)
        service = AccessFilteringService(
            env.group_manager,
            activated_repo_manager=counting,  # type: ignore[arg-type]
        )

        filtered = service.filter_query_results(_rows(*[OWN_ACTIVATION] * 20), USER)

        assert len(filtered) == 20
        assert counting.calls == 1

    def test_without_activation_manager_only_golden_names_pass(self, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        service = AccessFilteringService(env.group_manager)

        filtered = service.filter_query_results(
            _rows(OWN_ACTIVATION, global_alias(GRANTED_REPO)), USER
        )

        assert {r["repository_alias"] for r in filtered} == {global_alias(GRANTED_REPO)}

    def test_unwired_filtering_logs_one_warning_per_instance(self, env, caplog):
        # Built with the group manager only: no activated_repo_manager.
        unwired = AccessFilteringService(env.group_manager)

        with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
            unwired.filter_query_results(_rows(global_alias(GRANTED_REPO)), USER)
            unwired.filter_query_results(_rows(global_alias(GRANTED_REPO)), USER)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "activated_repo_manager" in warnings[0].getMessage()

    def test_wired_filtering_logs_no_warning(self, env, caplog):
        with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
            env.access_service.filter_query_results(
                _rows(global_alias(GRANTED_REPO)), USER
            )

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def _run_stored_temporal_job(
    env: QueryAccessEnv, monkeypatch: pytest.MonkeyPatch, user_alias: str, job: str
) -> Dict[str, Any]:
    """Run the real temporal worker for USER's activation and return the
    stored snapshot. Only the temporal backend and fusion search (external
    index boundary) are replaced, exactly as the worker's own tests do."""
    monkeypatch.setattr(
        "code_indexer.server.services.temporal_worker.reconstruct_temporal_backend",
        lambda *a, **kw: (MagicMock(), Path("index"), MagicMock()),
    )
    found = TemporalSearchResults(
        results=[
            TemporalSearchResult(
                file_path="src/secret.py",
                chunk_index=0,
                content="content",
                score=0.9,
                metadata={"commit_hash": "abc"},
                temporal_context={"commit_hash": "abc", "commit_timestamp": 1},
            )
        ],
        query="find",
        filter_type="time_range",
        filter_value=("2024-01-01", "2024-12-31"),
        total_found=1,
        shards_total=1,
        shards_attempted=1,
        shards_succeeded=1,
    )
    monkeypatch.setattr(
        "code_indexer.server.services.temporal_worker."
        "execute_temporal_query_with_fusion",
        lambda *a, **kw: found,
    )
    repo_path = env.activated_repo_manager.get_activated_repo_path(USER, user_alias)
    worker_input = TemporalWorkerInput(
        repo_path=str(repo_path),
        repository_alias=user_alias,
        username=USER,
        query_text="find",
        requested_limit=10,
        fusion_fetch_limit=30,
        time_range=("2024-01-01", "2024-12-31"),
        time_range_raw=None,
        time_range_all=False,
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
    run_temporal_worker(
        worker_input,
        env.payload_cache,
        job_id=job,
        activated_repo_manager=env.activated_repo_manager,
    )
    snapshot = read_temporal_snapshot(env.payload_cache, job)
    assert snapshot is not None
    return snapshot


def _poll(env: QueryAccessEnv, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows, _done, _total, _unranked = postprocess_temporal_snapshot(
        snapshot, env.access_service, USER, is_admin=False, terminal=True
    )
    return rows


class TestQueryRowProvenance:
    """The query manager stamps activation provenance where each repository
    is searched, never by matching the alias label afterwards."""

    def _rows(self, env: QueryAccessEnv) -> List[Dict[str, Any]]:
        with env.installed(env.access_service):
            response = env.query_manager.query_user_repositories(
                username=USER,
                query_text="find main",
                query_strategy="primary_only",
                limit=ALL_ROWS_LIMIT,
            )
        rows: List[Dict[str, Any]] = response["results"]
        return rows

    def test_activation_rows_carry_their_source_and_global_rows_do_not(self, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        rows = self._rows(env)

        by_alias = {r["repository_alias"]: r for r in rows}
        assert by_alias[OWN_ACTIVATION]["activation_source_repos"] == [GRANTED_REPO]
        assert "activation_source_repos" not in by_alias[global_alias(GRANTED_REPO)]

    def test_an_activation_named_like_a_global_repo_does_not_stamp_its_rows(self, env):
        # Default-alias activation: label equals the golden name, but the
        # global repo's own rows (labelled -global) stay unstamped.
        env.activate_for(USER, GRANTED_REPO_2, GRANTED_REPO)

        rows = self._rows(env)

        stamped = {
            r["repository_alias"] for r in rows if "activation_source_repos" in r
        }
        assert stamped == {GRANTED_REPO}
        assert all(
            r["activation_source_repos"] == [GRANTED_REPO_2]
            for r in rows
            if r["repository_alias"] == GRANTED_REPO
        )


class TestStoredTemporalRows:
    """Rows stored by a temporal job keep their activation provenance, so a
    later poll never judges them by the alias label alone."""

    def test_rows_of_a_removed_activation_under_a_granted_name_are_dropped(
        self, env, monkeypatch
    ):
        # Ungranted source activated under a granted golden repo's name.
        env.group_manager.grant_repo_access(UNGRANTED_REPO, env.group_id, "test")
        env.activate_for(USER, UNGRANTED_REPO, GRANTED_REPO_2)
        snapshot = _run_stored_temporal_job(env, monkeypatch, GRANTED_REPO_2, "j1")
        assert len(_poll(env, snapshot)) == 1

        env.group_manager.revoke_repo_access(UNGRANTED_REPO, env.group_id)
        job_id = env.activated_repo_manager.deactivate_repository(USER, GRANTED_REPO_2)
        assert env.wait_for_job(job_id, USER)["status"] == "completed"

        assert _poll(env, snapshot) == []

    def test_rows_of_a_granted_activation_survive_the_poll(self, env, monkeypatch):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        snapshot = _run_stored_temporal_job(env, monkeypatch, OWN_ACTIVATION, "j2")

        rows = _poll(env, snapshot)

        assert {r["repository_alias"] for r in rows} == {OWN_ACTIVATION}


class TestAliasCollidingWithGoldenName:
    def test_global_suffixed_activation_alias_never_grants_the_global_repo(self, env):
        colliding = global_alias(UNGRANTED_REPO)
        env.activate_for(USER, GRANTED_REPO, colliding)

        assert _kept(env, USER, colliding) == set()

    def test_bare_golden_name_activation_alias_never_grants_that_repo(self, env):
        env.activate_for(USER, GRANTED_REPO, UNGRANTED_REPO)

        assert _kept(env, USER, UNGRANTED_REPO, global_alias(UNGRANTED_REPO)) == set()
