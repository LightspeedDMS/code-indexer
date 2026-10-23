"""The admin "generate missing descriptions" backfill writes each
description through catalog admission and the staged writer, like a
repository registration: under an exclusive holder (write mode) nothing is
written into cidx-meta -- the description is parked and the refresh owed;
with the catalog free it is put in place whole, at the catalog-wide mode.

Real components: the real diagnostics callback, the real description hook,
the real RefreshScheduler and its lock (file lock, SQLite and, with
TEST_POSTGRES_DSN, PostgreSQL alias-lock stores). Only the Claude
description generator is a stand-in.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from code_indexer.global_repos import meta_description_hook
from code_indexer.global_repos.catalog_pending_changes import pending_operation
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.routers.diagnostics import _make_description_callback
from tests.utils.catalog_lock_schedulers import (
    CATALOG,
    CATALOG_LOCK_KINDS,
    catalog_lock_scheduler,
)

REPO = "example-repo"
GENERATED = "# example-repo\n\nGenerated description.\n"


class _Debouncer:
    def __init__(self) -> None:
        self.signals = 0

    def signal_dirty(self) -> None:
        self.signals += 1

    def note_direct_write(self) -> int:
        return 1

    def track_submission(self, job_id: str, seq: int) -> None:
        pass


@pytest.fixture(params=CATALOG_LOCK_KINDS)
def scheduler(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[RefreshScheduler]:
    real = catalog_lock_scheduler(str(request.param), tmp_path)
    monkeypatch.setattr(
        RefreshScheduler, "trigger_refresh_for_repo", lambda self, *a, **k: "job-1"
    )
    meta_description_hook.set_refresh_scheduler(real)
    try:
        yield real
    finally:
        meta_description_hook.set_refresh_scheduler(None)
        meta_description_hook.set_debouncer(None)


def _backfill(scheduler: RefreshScheduler) -> Path:
    md_path = Path(scheduler.golden_repos_dir) / CATALOG / f"{REPO}.md"
    callback = _make_description_callback(
        REPO,
        md_path,
        "https://example.com/example-repo.git",
        "/nonexistent",
        lambda *args: GENERATED,
    )
    callback(True, "ok")
    return md_path


def test_backfill_parks_the_description_under_write_mode(
    scheduler: RefreshScheduler,
) -> None:
    debouncer = _Debouncer()
    meta_description_hook.set_debouncer(debouncer)  # type: ignore[arg-type]
    locks = scheduler.write_lock_manager
    assert locks.acquire(CATALOG, owner_name="mcp_write_mode")
    try:
        md_path = _backfill(scheduler)
        assert not md_path.exists(), "written into cidx-meta under write mode"
        assert pending_operation(scheduler.golden_repos_dir, REPO) == "add"
        assert debouncer.signals >= 1, "the refresh was not owed"
    finally:
        assert locks.release(CATALOG, owner_name="mcp_write_mode")


def test_backfill_puts_the_description_in_place_when_free(
    scheduler: RefreshScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta_description_hook.set_debouncer(_Debouncer())  # type: ignore[arg-type]
    holders: List[Any] = []
    real_write = meta_description_hook.write_catalog_file

    def _observed(*args: Any, **kwargs: Any) -> bool:
        info = scheduler.write_lock_manager.get_lock_info(CATALOG)
        holders.append(info["owner"] if info else None)
        return bool(real_write(*args, **kwargs))

    monkeypatch.setattr(meta_description_hook, "write_catalog_file", _observed)

    md_path = _backfill(scheduler)

    assert md_path.read_text() == GENERATED
    assert holders == ["lifecycle_writer"], "written without the catalog lock"
    assert stat.S_IMODE(md_path.stat().st_mode) == 0o644
    assert scheduler.write_lock_manager.get_lock_info(CATALOG) is None
