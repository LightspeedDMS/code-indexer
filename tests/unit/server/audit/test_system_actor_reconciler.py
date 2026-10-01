"""The golden-repo reconciler's automatic removal carries a system actor.

The removal job keeps its own submitter (the reconciler's job username); the
audit row names the reconciler system component, built by the system
builder (``actor_is_system`` 1, source ``system``), never a human name and
never ``admin``.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Iterator

import pytest

from _audit_accounts_support import (
    CAPTURE_LOGGER,
    AuditStore,
    bound_audit_store,
    capture_errors,
)
from _audit_repos_support import (
    RecordingJobManager,
    make_golden_repo_manager,
    register_repo,
)


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[AuditStore]:
    yield from bound_audit_store(tmp_path / "audit.db")


def test_reconciler_removal_records_the_system_actor(
    store, tmp_path: Path, caplog
) -> None:
    from code_indexer.server.services.golden_repo_reconciler import (
        DEFAULT_RECONCILE_SUBMITTER,
        reconcile_golden_repo_registry,
    )

    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    jobs = RecordingJobManager()
    manager = make_golden_repo_manager(tmp_path, jobs)
    for alias in ("example-a", "example-b", "example-c", "example-orphan"):
        register_repo(manager, alias)
    shutil.rmtree(Path(manager.golden_repos_dir) / "example-orphan")

    result = reconcile_golden_repo_registry(manager)

    assert result.orphans_removed == ["example-orphan"], result
    (row,) = store.rows("golden_repo_removed")
    assert (row.actor, row.actor_is_system, row.source, row.auth_method) == (
        "system:golden-repo-reconciler",
        1,
        "system",
        "system",
    )
    assert (row.target_id, row.outcome) == ("example-orphan", "success")
    assert row.details == {"job_id": jobs.submissions[0]["job_id"]}
    assert jobs.submissions[0]["submitter_username"] == DEFAULT_RECONCILE_SUBMITTER
    assert capture_errors(caplog) == []
