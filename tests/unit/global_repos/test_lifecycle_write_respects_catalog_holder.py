"""The lifecycle runner's description write either holds the cidx-meta write
lock (as the lifecycle writer) or writes under a cooperative holder; an
exclusive holder (write mode, the catalog backup) refuses it, so nothing is
written and the alias fails for the caller's next run to retry.

Real components: the real RefreshScheduler and its lock over the file lock,
the SQLite alias-lock store and (with TEST_POSTGRES_DSN) the PostgreSQL
store; the real LifecycleBatchRunner. Only the Claude invocation is a
stand-in returning a fixed result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from code_indexer.global_repos.lifecycle_batch_runner import (
    LifecycleBatchRunner,
    LifecycleLockUnavailableError,
)
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.unified_response_parser import UnifiedResult
from tests.utils.catalog_lock_schedulers import (
    CATALOG,
    CATALOG_LOCK_KINDS,
    catalog_lock_scheduler,
)

ALIAS = "example-repo"
EXISTING = "---\nname: example-repo\n---\n\nThe existing description.\n"


class _Quiet:
    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: None


@pytest.fixture(params=CATALOG_LOCK_KINDS)
def scheduler(request: pytest.FixtureRequest, tmp_path: Path) -> RefreshScheduler:
    return catalog_lock_scheduler(str(request.param), tmp_path)


def _runner(scheduler: RefreshScheduler, invoker: Any) -> LifecycleBatchRunner:
    return LifecycleBatchRunner(
        golden_repos_dir=scheduler.golden_repos_dir,
        job_tracker=_Quiet(),
        refresh_scheduler=scheduler,
        debouncer=_Quiet(),
        claude_cli_invoker=invoker,
    )


def _invoker(*args: Any, **kwargs: Any) -> UnifiedResult:
    return UnifiedResult(
        description="A refreshed description.",
        lifecycle={"confidence": "high"},
    )


def _description(scheduler: RefreshScheduler) -> Path:
    path = Path(scheduler.golden_repos_dir) / CATALOG / f"{ALIAS}.md"
    if not path.exists():
        path.write_text(EXISTING)
    return path


@pytest.mark.parametrize("holder", ["mcp_write_mode", "catalog_backup"])
def test_an_exclusive_holder_refuses_the_lifecycle_write(
    scheduler: RefreshScheduler, holder: str
) -> None:
    description = _description(scheduler)
    locks = scheduler.write_lock_manager
    assert locks.acquire(CATALOG, owner_name=holder)
    try:
        with pytest.raises(LifecycleLockUnavailableError) as refused:
            _runner(scheduler, _invoker)._process_one_repo(ALIAS, "job-1")
        assert holder not in str(refused.value)
        assert description.read_text() == EXISTING, "written under an exclusive holder"
        info = locks.get_lock_info(CATALOG)
        assert info and info["owner"] == holder
    finally:
        assert locks.release(CATALOG, owner_name=holder)


def test_a_cooperative_holder_lets_the_lifecycle_write_through(
    scheduler: RefreshScheduler,
) -> None:
    description = _description(scheduler)
    locks = scheduler.write_lock_manager
    assert locks.acquire(CATALOG, owner_name="refresh_scheduler")
    try:
        _runner(scheduler, _invoker)._process_one_repo(ALIAS, "job-1")
        assert "A refreshed description." in description.read_text()
    finally:
        assert locks.release(CATALOG, owner_name="refresh_scheduler")


def test_a_free_catalog_is_locked_by_the_lifecycle_writer_while_it_writes(
    scheduler: RefreshScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.global_repos import lifecycle_batch_runner

    description = _description(scheduler)
    holders: List[Any] = []
    real_write = lifecycle_batch_runner._do_write

    def _observed(*args: Any, **kwargs: Any) -> None:
        info = scheduler.write_lock_manager.get_lock_info(CATALOG)
        holders.append(info["owner"] if info else None)
        real_write(*args, **kwargs)

    monkeypatch.setattr(lifecycle_batch_runner, "_do_write", _observed)
    _runner(scheduler, _invoker)._process_one_repo(ALIAS, "job-1")

    assert holders == ["lifecycle_writer"]
    assert "A refreshed description." in description.read_text()
    assert scheduler.write_lock_manager.get_lock_info(CATALOG) is None
