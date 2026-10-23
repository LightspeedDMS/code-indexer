"""Parked catalog changes never erase each other.

Each alias has ONE parked record (latest wins, written atomically), and an
applier claims a record with an atomic rename before applying it, so a
newer change parked meanwhile stays for the next apply. A change whose
apply fails stays parked; a claim abandoned by a crashed applier is
recovered once stale.

Real components: the real pending store and staged writer on the real
filesystem, the real RefreshScheduler and its lock (file lock, SQLite and,
with TEST_POSTGRES_DSN, PostgreSQL alias-lock stores). Interleavings are
injected by wrapping the module's real writer.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, List

import pytest

from code_indexer.global_repos import catalog_pending_changes as pending
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from tests.utils.catalog_lock_schedulers import (
    CATALOG,
    CATALOG_LOCK_KINDS,
    catalog_lock_scheduler,
)

ALIAS = "example-repo"
OLD = "# example-repo\n\nThe old description.\n"


class _Crash(BaseException):
    """Stands for the applier's process dying mid-apply."""


@pytest.fixture(params=CATALOG_LOCK_KINDS)
def scheduler(request: pytest.FixtureRequest, tmp_path: Path) -> RefreshScheduler:
    return catalog_lock_scheduler(str(request.param), tmp_path)


def _golden(scheduler: RefreshScheduler) -> Path:
    return Path(scheduler.golden_repos_dir)


def _description(scheduler: RefreshScheduler) -> Path:
    return _golden(scheduler) / CATALOG / f"{ALIAS}.md"


def _wrap_writer(monkeypatch: pytest.MonkeyPatch, around: Any) -> None:
    real = pending.write_catalog_file

    def _wrapped(root: Path, target: Path, content: str, **kwargs: Any) -> bool:
        return bool(around(real, root, target, content, **kwargs))

    monkeypatch.setattr(pending, "write_catalog_file", _wrapped)


def test_interleaved_parkers_leave_exactly_one_change(
    scheduler: RefreshScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _golden(scheduler)
    _description(scheduler).write_text(OLD)
    overtaken: List[bool] = []

    def _removal_overtakes_the_add(
        real: Any, root: Path, target: Path, content: str, **kwargs: Any
    ) -> bool:
        durable = real(root, target, content, **kwargs)
        if not overtaken:
            overtaken.append(True)
            pending.defer_removal(golden, ALIAS)
        return bool(durable)

    _wrap_writer(monkeypatch, _removal_overtakes_the_add)
    pending.defer_description(golden, ALIAS, "# example-repo\n\nNew.\n")
    monkeypatch.undo()

    assert pending.apply_pending_catalog_changes(scheduler) is True
    final = _description(scheduler)
    assert not (final.exists() and final.read_text() == OLD), (
        "both parked changes were erased; the old description survived"
    )


def test_a_change_parked_during_an_apply_survives_for_the_next(
    scheduler: RefreshScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _golden(scheduler)
    pending.defer_description(golden, ALIAS, "v1\n")
    parked_during: List[bool] = []

    def _park_newer_during_apply(
        real: Any, root: Path, target: Path, content: str, **kwargs: Any
    ) -> bool:
        durable = real(root, target, content, **kwargs)
        if target.parent.name == CATALOG and not parked_during:
            parked_during.append(True)
            pending.defer_description(golden, ALIAS, "v2\n")
        return bool(durable)

    _wrap_writer(monkeypatch, _park_newer_during_apply)
    pending.apply_pending_catalog_changes(scheduler)
    monkeypatch.undo()

    assert _description(scheduler).read_text() == "v1\n"
    assert pending.apply_pending_catalog_changes(scheduler) is True
    assert _description(scheduler).read_text() == "v2\n"


def test_a_failed_apply_keeps_the_change_parked(
    scheduler: RefreshScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _golden(scheduler)
    pending.defer_description(golden, ALIAS, "v1\n")

    def _disk_full(
        real: Any, root: Path, target: Path, content: str, **kwargs: Any
    ) -> bool:
        if target.parent.name == CATALOG:
            raise OSError("no space left on device")
        return bool(real(root, target, content, **kwargs))

    _wrap_writer(monkeypatch, _disk_full)
    with pytest.raises(OSError):
        pending.apply_pending_catalog_changes(scheduler)
    monkeypatch.undo()

    assert pending.apply_pending_catalog_changes(scheduler) is True
    assert _description(scheduler).read_text() == "v1\n"


def test_a_claim_abandoned_by_a_crash_is_recovered_once_stale(
    scheduler: RefreshScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    golden = _golden(scheduler)
    pending.defer_description(golden, ALIAS, "v1\n")

    def _crash(
        real: Any, root: Path, target: Path, content: str, **kwargs: Any
    ) -> bool:
        if target.parent.name == CATALOG:
            raise _Crash()
        return bool(real(root, target, content, **kwargs))

    _wrap_writer(monkeypatch, _crash)
    with pytest.raises(_Crash):
        pending.apply_pending_catalog_changes(scheduler)
    monkeypatch.undo()
    assert pending.apply_pending_catalog_changes(scheduler) is False, (
        "a fresh claim (a live applier) must be left alone"
    )
    stale = time.time() - pending.STALE_CLAIM_SECONDS - 60
    for leftover in (golden / pending.PENDING_DIR_NAME).iterdir():
        os.utime(leftover, (stale, stale))

    assert pending.apply_pending_catalog_changes(scheduler) is True
    assert _description(scheduler).read_text() == "v1\n"
