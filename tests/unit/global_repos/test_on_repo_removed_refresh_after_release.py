"""Removing a golden repository deletes its catalog description under the
catalog write lock and requests the catalog refresh only after releasing
that lock: a refresh starting while the lock is held skips the cycle and
owes nothing, so a trigger sent under the lock would leave the deleted
description in the index.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List, Optional

import pytest

from code_indexer.global_repos import meta_description_hook


class _LockingScheduler:
    def __init__(self) -> None:
        self.holder: Optional[str] = None
        self.lock_held_at_trigger: List[bool] = []

    def acquire_write_lock(self, alias: str, owner_name: str) -> bool:
        assert alias == "cidx-meta"
        if self.holder is not None:
            return False
        self.holder = owner_name
        return True

    def release_write_lock(self, alias: str, owner_name: str) -> bool:
        assert self.holder == owner_name
        self.holder = None
        return True

    def trigger_refresh_for_repo(self, alias_name: str) -> Optional[str]:
        assert alias_name == "cidx-meta-global"
        self.lock_held_at_trigger.append(self.holder is not None)
        return "job-1"


@pytest.fixture
def scheduler() -> Iterator[_LockingScheduler]:
    sched = _LockingScheduler()
    meta_description_hook.set_refresh_scheduler(sched)
    meta_description_hook.set_debouncer(None)
    meta_description_hook.set_tracking_backend(None)
    try:
        yield sched
    finally:
        meta_description_hook.set_refresh_scheduler(None)


def test_removal_requests_refresh_after_releasing_the_lock(
    tmp_path: Path, scheduler: _LockingScheduler
) -> None:
    golden = tmp_path / "golden-repos"
    description = golden / "cidx-meta" / "example-repo.md"
    description.parent.mkdir(parents=True)
    description.write_text("# example-repo\n", encoding="utf-8")

    meta_description_hook.on_repo_removed("example-repo", str(golden))

    assert not description.exists()
    assert scheduler.lock_held_at_trigger == [False]
    assert scheduler.holder is None
