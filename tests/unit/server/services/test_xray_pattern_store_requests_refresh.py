"""Storing an X-Ray pattern is a catalog write: it requests a catalog
refresh, after releasing the catalog write lock (a refresh that starts while
the lock is held skips and owes nothing).

Real XrayPatternService on a real non-git catalog directory; the scheduler
double keeps real write-lock state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List, Optional

import pytest

from code_indexer.global_repos.meta_description_hook import set_debouncer
from code_indexer.server.services.xray_pattern_service import XrayPatternService

MINIMAL_PATTERN_YAML = """\
name: test-pattern
description: "A test pattern"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }
"""


class _LockingScheduler:
    def __init__(self) -> None:
        self.holder: Optional[str] = None
        self.lock_held_at_trigger: List[bool] = []

    def is_write_locked(self, alias: str) -> bool:
        return self.holder is not None

    def acquire_write_lock(self, alias: str, owner: str) -> bool:
        if self.holder is not None:
            return False
        self.holder = owner
        return True

    def release_write_lock(self, alias: str, owner: str) -> None:
        assert self.holder == owner
        self.holder = None

    def trigger_refresh_for_repo(self, alias_name: str) -> Optional[str]:
        assert alias_name == "cidx-meta-global"
        self.lock_held_at_trigger.append(self.holder is not None)
        return "job-1"


def test_store_requests_refresh_after_releasing_the_lock(tmp_path: Path) -> None:
    catalog = tmp_path / "cidx-meta"
    catalog.mkdir()
    scheduler = _LockingScheduler()
    svc = XrayPatternService(catalog, refresh_scheduler=scheduler)

    result = svc.store_xray_pattern_audited(
        scope="__any__",
        pattern_yaml=MINIMAL_PATTERN_YAML,
        overwrite=False,
        actor="alice",
        may_replace_global=True,
    )

    assert result.get("success") is True, result
    assert (catalog / str(result["path"])).is_file()
    assert scheduler.lock_held_at_trigger == [False]
    assert scheduler.holder is None


def test_delete_requests_refresh_after_releasing_the_lock(tmp_path: Path) -> None:
    catalog = tmp_path / "cidx-meta"
    pattern = catalog / "xray-patterns" / "__any__" / "test-pattern.yaml"
    pattern.parent.mkdir(parents=True)
    pattern.write_text(MINIMAL_PATTERN_YAML, encoding="utf-8")
    scheduler = _LockingScheduler()
    svc = XrayPatternService(catalog, refresh_scheduler=scheduler)

    result = svc.delete_pattern("__any__", "test-pattern")

    assert result == {"success": True}
    assert not pattern.exists()
    assert scheduler.lock_held_at_trigger == [False]


class _CountingDebouncer:
    def __init__(self) -> None:
        self.signals = 0

    def signal_dirty(self) -> None:
        self.signals += 1


@pytest.fixture
def module_debouncer() -> Iterator[_CountingDebouncer]:
    debouncer = _CountingDebouncer()
    set_debouncer(debouncer)  # type: ignore[arg-type]
    try:
        yield debouncer
    finally:
        set_debouncer(None)


def test_piggyback_store_owes_refresh_to_debouncer(
    tmp_path: Path, module_debouncer: _CountingDebouncer
) -> None:
    # Another writer holds the catalog lock: a refresh requested now would
    # skip on that lock and owe nothing, so the write is owed to the
    # debouncer instead.
    catalog = tmp_path / "cidx-meta"
    catalog.mkdir()
    scheduler = _LockingScheduler()
    scheduler.holder = "dependency-map-writer"
    svc = XrayPatternService(catalog, refresh_scheduler=scheduler)

    result = svc.store_xray_pattern_audited(
        scope="__any__",
        pattern_yaml=MINIMAL_PATTERN_YAML,
        overwrite=False,
        actor="alice",
        may_replace_global=True,
    )

    assert result.get("success") is True, result
    assert scheduler.lock_held_at_trigger == []
    assert module_debouncer.signals == 1
    assert scheduler.holder == "dependency-map-writer"


def test_piggyback_delete_owes_refresh_to_debouncer(
    tmp_path: Path, module_debouncer: _CountingDebouncer
) -> None:
    catalog = tmp_path / "cidx-meta"
    pattern = catalog / "xray-patterns" / "__any__" / "test-pattern.yaml"
    pattern.parent.mkdir(parents=True)
    pattern.write_text(MINIMAL_PATTERN_YAML, encoding="utf-8")
    scheduler = _LockingScheduler()
    scheduler.holder = "dependency-map-writer"
    svc = XrayPatternService(catalog, refresh_scheduler=scheduler)

    assert svc.delete_pattern("__any__", "test-pattern") == {"success": True}
    assert scheduler.lock_held_at_trigger == []
    assert module_debouncer.signals == 1
