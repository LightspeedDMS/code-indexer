"""A re-created account never adopts the previous account's repositories.

Deleting an account submits removal of every repository it activated, through
the admin deactivation path; the name cannot be created again while any of
those repositories remain.  Real ``ActivatedRepoManager`` over a temporary
data directory; the background job runner is a double that runs the real
deactivation job when the test says so.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.services.account_activations import AccountActivations
from tests.unit.server._account_rows import (
    OTHER_PASSWORD,
    PASSWORD,
    Stores,
    build_stores,
)

ALIASES = ("example-repo", "example-docs")
# BackgroundJobManager.submit_job's own parameters: never passed to the job.
_RUNNER_PARAMS = frozenset({"submitter_username", "repo_alias", "actor_username"})


class _JobRunner:
    """Background job runner double: records submissions, runs them on demand
    with the same argument split as BackgroundJobManager.submit_job."""

    def __init__(self) -> None:
        self.jobs: List[Tuple[str, Any, Dict[str, Any]]] = []

    def submit_job(self, operation_type: str, func: Any, *args: Any, **kwargs: Any):
        self.jobs.append((operation_type, func, kwargs))
        return f"job-{len(self.jobs)}"

    def run_all(self) -> None:
        for _, func, kwargs in self.jobs:
            func(**{k: v for k, v in kwargs.items() if k not in _RUNNER_PARAMS})


def _activate(arm: ActivatedRepoManager, username: str, alias: str) -> None:
    """Lay out an activated repository the way activation leaves it."""
    user_dir = Path(arm.activated_repos_dir) / username
    (user_dir / alias).mkdir(parents=True)
    (user_dir / alias / "README.md").write_text("example\n")
    (user_dir / f"{alias}_metadata.json").write_text(
        json.dumps(
            {
                "user_alias": alias,
                "golden_repo_alias": "example-golden",
                "current_branch": "main",
                "activated_at": "2026-01-01T00:00:00+00:00",
                "last_accessed": "2026-01-01T00:00:00+00:00",
            }
        )
    )


@pytest.fixture
def setup(tmp_path: Path) -> Tuple[Stores, ActivatedRepoManager, _JobRunner]:
    stores = build_stores(tmp_path)
    runner = _JobRunner()
    arm = ActivatedRepoManager(
        data_dir=str(stores.server_dir / "data"),
        background_job_manager=runner,  # type: ignore[arg-type]
    )
    stores.user_manager.set_account_activations(AccountActivations(arm))
    stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    for alias in ALIASES:
        _activate(arm, "alice", alias)
    assert len(arm.list_activated_repositories("alice")) == len(ALIASES)
    return stores, arm, runner


def test_deleting_account_submits_removal_of_its_repositories(setup) -> None:
    stores, _, runner = setup

    assert stores.user_manager.delete_user_audited("alice", actor="admin")

    submitted = sorted(
        (op, kw["username"], kw["user_alias"], kw["actor_username"])
        for op, _, kw in runner.jobs
    )
    assert submitted == sorted(
        ("deactivate_repository", "alice", alias, "admin") for alias in ALIASES
    )


def test_recreating_name_refused_while_repositories_remain(setup) -> None:
    stores, _, _ = setup
    assert stores.user_manager.delete_user_audited("alice", actor="admin")

    with pytest.raises(ValueError, match="still being removed"):
        stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)
    assert stores.user_manager.get_user("alice") is None


def test_recreating_name_allowed_once_repositories_are_removed(setup) -> None:
    stores, arm, runner = setup
    assert stores.user_manager.delete_user_audited("alice", actor="admin")

    runner.run_all()
    stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)

    assert arm.list_activated_repositories("alice") == []
    user_dir = Path(arm.activated_repos_dir) / "alice"
    assert not user_dir.exists() or os.listdir(user_dir) == []
