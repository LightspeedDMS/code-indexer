"""One path for every catalog writer's refresh request: requested at once
after the writer released its own catalog write lock, owed to the debouncer
while a lock is still held (another owner's, the refresh's own publish lock,
or the writer's after a failed release)."""

from __future__ import annotations

import logging
from typing import List

import pytest

from code_indexer.global_repos import meta_description_hook
from code_indexer.global_repos.meta_description_hook import (
    owe_cidx_meta_refresh,
    refresh_after_catalog_write,
)


class _Scheduler:
    def __init__(self) -> None:
        self.triggers: List[str] = []

    def trigger_refresh_for_repo(self, alias_name: str) -> str:
        self.triggers.append(alias_name)
        return "job-1"


class _Debouncer:
    def __init__(self) -> None:
        self.signals = 0
        self.tracked: List[str] = []

    def signal_dirty(self) -> None:
        self.signals += 1

    def track_submission(self, job_id: str) -> None:
        self.tracked.append(job_id)


@pytest.fixture(autouse=True)
def no_module_debouncer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta_description_hook, "_debouncer", None)


def test_owed_write_without_a_debouncer_waits_for_a_later_refresh(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        owe_cidx_meta_refresh()

    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = record.getMessage()
    assert "later cidx-meta refresh" in message, message
    assert "holder's own refresh" not in message, message


def test_released_lock_requests_the_refresh_now() -> None:
    scheduler, debouncer = _Scheduler(), _Debouncer()

    refresh_after_catalog_write(scheduler, lock_still_held=False, debouncer=debouncer)

    assert scheduler.triggers == ["cidx-meta-global"]
    assert debouncer.signals == 0
    assert debouncer.tracked == ["job-1"]


def test_held_lock_owes_the_refresh_to_the_given_debouncer() -> None:
    scheduler, debouncer = _Scheduler(), _Debouncer()

    refresh_after_catalog_write(scheduler, lock_still_held=True, debouncer=debouncer)

    assert scheduler.triggers == []
    assert debouncer.signals == 1


def test_held_lock_owes_the_refresh_to_the_module_debouncer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler, debouncer = _Scheduler(), _Debouncer()
    monkeypatch.setattr(meta_description_hook, "_debouncer", debouncer)

    refresh_after_catalog_write(scheduler, lock_still_held=True)

    assert scheduler.triggers == []
    assert debouncer.signals == 1
