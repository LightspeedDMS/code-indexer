"""Golden-repository entry points record one attributed row per request.

Each audited manager method takes the acting user as a required keyword
argument (never a default) and records exactly one ``success`` row after the
job is submitted, or one ``failure`` row when the request is refused.  Rows
carry only verified identifiers and never a repository URL.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Iterator, List

import pytest

from _audit_accounts_support import (
    CAPTURE_LOGGER,
    AuditStore,
    bound_audit_store,
    capture_errors,
)
from _audit_repos_support import (
    EXAMPLE_ALIAS,
    RecordingJobManager,
    make_golden_repo_manager,
    make_refresh_scheduler,
    register_repo,
)

_SECOND_ADMIN = "example-second-admin"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[AuditStore]:
    yield from bound_audit_store(tmp_path / "audit.db")


@pytest.fixture(autouse=True)
def _no_capture_errors(caplog) -> Iterator[None]:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    yield
    assert capture_errors(caplog, phases=["setup", "call"]) == []


@pytest.fixture()
def jobs() -> RecordingJobManager:
    return RecordingJobManager()


@pytest.fixture()
def manager(tmp_path: Path, jobs: RecordingJobManager):
    return make_golden_repo_manager(tmp_path, jobs)


class TestRemoval:
    def test_remove_records_the_real_actor(self, store, manager, jobs) -> None:
        register_repo(manager)
        job_id = manager.remove_golden_repo(
            EXAMPLE_ALIAS, submitter_username=_SECOND_ADMIN
        )
        (row,) = store.rows("golden_repo_removed")
        assert (row.actor, row.target_type, row.target_id, row.outcome) == (
            _SECOND_ADMIN,
            "repo",
            EXAMPLE_ALIAS,
            "success",
        )
        assert row.details == {"job_id": job_id}
        assert jobs.submissions[0]["submitter_username"] == _SECOND_ADMIN


_SENTINEL = "SENTINEL-7f3a-repo"


class TestAdd:
    def test_add_records_host_and_branch_never_the_url(
        self, store, manager, jobs
    ) -> None:
        url = (
            f"https://x:{_SENTINEL}@Git.Example.com/org/r.git?access_token={_SENTINEL}"
        )
        job_id = manager.add_golden_repo(
            repo_url=url,
            alias=EXAMPLE_ALIAS,
            default_branch="main",
            submitter_username=_SECOND_ADMIN,
            skip_pre_flight_git_validation=True,
        )
        (row,) = store.rows("golden_repo_added")
        assert (row.actor, row.target_id, row.outcome) == (
            _SECOND_ADMIN,
            EXAMPLE_ALIAS,
            "success",
        )
        assert row.details == {
            "job_id": job_id,
            "repo_host": "git.example.com",
            "branch": "main",
        }
        assert _SENTINEL not in store.all_raw_text()


class TestIndexAndBranch:
    def test_add_indexes_records_one_row(self, store, manager) -> None:
        register_repo(manager)
        job_id = manager.add_indexes_to_golden_repo(
            EXAMPLE_ALIAS, ["fts", "scip"], submitter_username=_SECOND_ADMIN
        )
        (row,) = store.rows("golden_repo_index_added")
        assert (row.actor, row.target_id, row.outcome) == (
            _SECOND_ADMIN,
            EXAMPLE_ALIAS,
            "success",
        )
        assert row.details == {"index_types": ["fts", "scip"], "job_id": job_id}

    def test_add_single_index_records_one_row(self, store, manager) -> None:
        register_repo(manager)
        manager.add_index_to_golden_repo(
            EXAMPLE_ALIAS, "fts", submitter_username=_SECOND_ADMIN
        )
        assert len(store.rows("golden_repo_index_added")) == 1

    def test_branch_change_records_one_row(self, store, manager) -> None:
        register_repo(manager)
        result = manager.change_branch_async(
            EXAMPLE_ALIAS, "release-1", submitter_username=_SECOND_ADMIN
        )
        (row,) = store.rows("golden_repo_branch_changed")
        assert (row.actor, row.target_id, row.outcome) == (
            _SECOND_ADMIN,
            EXAMPLE_ALIAS,
            "success",
        )
        assert row.details == {"new_branch": "release-1", "job_id": result["job_id"]}

    def test_branch_already_current_changes_nothing_and_writes_no_row(
        self, store, manager
    ) -> None:
        register_repo(manager)
        result = manager.change_branch_async(
            EXAMPLE_ALIAS, "main", submitter_username=_SECOND_ADMIN
        )
        assert result["job_id"] is None
        assert store.rows("golden_repo_branch_changed") == []


class TestRefresh:
    def test_audited_refresh_records_one_row(self, store, tmp_path, jobs) -> None:
        from code_indexer.server.services.golden_repo_audited_ops import (
            request_golden_repo_refresh,
        )

        scheduler = make_refresh_scheduler(tmp_path, jobs)
        job_id = request_golden_repo_refresh(
            scheduler, EXAMPLE_ALIAS, actor=_SECOND_ADMIN, force_reset=True
        )
        (row,) = store.rows("golden_repo_refreshed")
        assert (row.actor, row.target_id, row.outcome) == (
            _SECOND_ADMIN,
            EXAMPLE_ALIAS,
            "success",
        )
        assert row.details == {"job_id": job_id, "force_reset": True}
        assert jobs.submissions[0]["submitter_username"] == _SECOND_ADMIN

    def test_unknown_alias_records_a_failure(self, store, tmp_path, jobs) -> None:
        from code_indexer.server.services.golden_repo_audited_ops import (
            request_golden_repo_refresh,
        )

        scheduler = make_refresh_scheduler(tmp_path, jobs)
        with pytest.raises(ValueError):
            request_golden_repo_refresh(
                scheduler, "missing-repo", actor=_SECOND_ADMIN, force_reset=False
            )
        (row,) = store.rows("golden_repo_refreshed")
        assert (row.target_id, row.outcome) == ("unresolved", "failure")
        assert row.details == {"force_reset": False}

    def test_internal_refresh_writes_no_row(self, store, tmp_path, jobs) -> None:
        scheduler = make_refresh_scheduler(tmp_path, jobs)
        scheduler.trigger_refresh_for_repo(EXAMPLE_ALIAS)
        assert len(jobs.submissions) == 1
        assert store.rows("golden_repo_") == []


class TestFailures:
    def test_removing_an_unknown_alias_records_a_failure(self, store, manager) -> None:
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoError,
        )

        with pytest.raises(GoldenRepoError):
            manager.remove_golden_repo("missing-repo", submitter_username=_SECOND_ADMIN)
        (row,) = store.rows("golden_repo_removed")
        assert (row.actor, row.target_id, row.outcome) == (
            _SECOND_ADMIN,
            "unresolved",
            "failure",
        )
        assert row.details == {}

    def test_adding_a_duplicate_alias_records_a_failure(self, store, manager) -> None:
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoError,
        )

        register_repo(manager)
        with pytest.raises(GoldenRepoError):
            manager.add_golden_repo(
                repo_url="git@git.example.com:org/r.git",
                alias=EXAMPLE_ALIAS,
                submitter_username=_SECOND_ADMIN,
                skip_pre_flight_git_validation=True,
            )
        (row,) = store.rows("golden_repo_added")
        assert (row.target_id, row.outcome) == ("unresolved", "failure")
        assert row.details == {"repo_host": "git.example.com"}

    def test_index_request_for_an_unknown_alias_records_a_failure(
        self, store, manager
    ) -> None:
        with pytest.raises(ValueError):
            manager.add_indexes_to_golden_repo(
                "missing-repo", ["fts"], submitter_username=_SECOND_ADMIN
            )
        (row,) = store.rows("golden_repo_index_added")
        assert (row.target_id, row.outcome) == ("unresolved", "failure")


class TestNoDefaultActor:
    def test_removal_without_an_actor_is_refused_and_removes_nothing(
        self, store, manager, jobs
    ) -> None:
        register_repo(manager)
        with pytest.raises(TypeError):
            manager.remove_golden_repo(EXAMPLE_ALIAS)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            manager.add_golden_repo(  # type: ignore[call-arg]
                repo_url="https://git.example.com/org/r.git", alias="other-repo"
            )
        assert jobs.submissions == []
        assert store.rows("golden_repo_") == []

    def test_no_parameter_of_the_manager_defaults_to_admin(self) -> None:
        import code_indexer.server.repositories.golden_repo_manager as module

        tree = ast.parse(Path(module.__file__).read_text())
        offenders: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults = [*node.args.defaults, *node.args.kw_defaults]
                offenders.extend(
                    f"{node.name}:{node.lineno}"
                    for d in defaults
                    if isinstance(d, ast.Constant) and d.value == "admin"
                )
        assert offenders == []
