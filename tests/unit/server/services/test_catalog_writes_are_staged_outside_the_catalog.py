"""Memory and X-Ray pattern writers put every catalog file in place
atomically: the content is staged in a temp file OUTSIDE the cidx-meta tree,
fsync'd, then moved over the destination with os.replace. Anything that
reads the catalog meanwhile -- the backup's ``git add -A``, a snapshot copy
-- sees the previous file or the new one, never a partial file and never a
temp file.

Real components: the real RefreshScheduler (file lock), the real memory and
X-Ray pattern services and the real filesystem. ``os.replace`` is wrapped by
a spy that inspects the catalog at the instant the file moves in and then
performs the real replace.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Set
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos import meta_description_hook
from code_indexer.global_repos.catalog_staged_write import STAGING_DIR_NAME
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.memory_file_lock_manager import MemoryFileLockManager
from code_indexer.server.services.memory_rate_limiter import (
    MemoryRateLimiter,
    RateLimitConfig,
)
from code_indexer.server.services.memory_store_service import (
    MemoryStoreConfig,
    MemoryStoreService,
)
from code_indexer.server.services.xray_pattern_service import XrayPatternService
from tests.fixtures.refresh_scheduler_stores import real_metadata_store

CATALOG = "cidx-meta"
PATTERN_YAML = """\
name: test-pattern
description: "{description}"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {{ vec![] }}
"""


class _Debouncer:
    def signal_dirty(self) -> None:
        pass

    def note_direct_write(self) -> int:
        return 1

    def track_submission(self, job_id: str, seq: int) -> None:
        pass


def _catalog_files(catalog: Path) -> Set[str]:
    return {
        str(Path(root, name).relative_to(catalog))
        for root, _dirs, names in os.walk(catalog)
        for name in names
    }


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    path = tmp_path / "golden-repos" / CATALOG
    path.mkdir(parents=True)
    return path


@pytest.fixture
def scheduler(tmp_path: Path, catalog: Path) -> RefreshScheduler:
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(catalog.parent),
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=MagicMock(),
        golden_repo_metadata_backend=real_metadata_store(tmp_path / "server-data"),
    )


@pytest.fixture(autouse=True)
def _no_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meta_description_hook, "_debouncer", _Debouncer())
    monkeypatch.setattr(
        RefreshScheduler, "trigger_refresh_for_repo", lambda *a, **k: None
    )


@pytest.fixture
def replaces(
    monkeypatch: pytest.MonkeyPatch, catalog: Path
) -> Callable[[Path, Set[str], str], List[Path]]:
    """Arm a spy on os.replace for *target*: when the target moves in, the
    catalog holds exactly *expected* files and the target still holds
    *previous* ("" when absent). Returns the list of replaced targets."""
    real_replace = os.replace
    seen: List[Path] = []
    armed: Dict[str, Any] = {}

    def _spy(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        destination = Path(os.fspath(dst))
        if destination.resolve() == armed["target"].resolve():
            assert _catalog_files(catalog) == armed["expected"], (
                "a temp file is visible inside the catalog while it is written"
            )
            previous = destination.read_text("utf-8") if destination.exists() else ""
            assert previous == armed["previous"], "the target was written in place"
            seen.append(destination)
        real_replace(src, dst, *args, **kwargs)

    def _arm(target: Path, expected: Set[str], previous: str) -> List[Path]:
        armed.update(target=target, expected=expected, previous=previous)
        return seen

    monkeypatch.setattr(os, "replace", _spy)
    return _arm


def _memory_service(
    tmp_path: Path, catalog: Path, scheduler: RefreshScheduler
) -> MemoryStoreService:
    return MemoryStoreService(
        config=MemoryStoreConfig(memories_dir=catalog / "memories"),
        lock_manager=MemoryFileLockManager(tmp_path / "memory-locks"),
        refresh_scheduler=scheduler,
        refresh_debouncer=_Debouncer(),
        rate_limiter=MemoryRateLimiter(
            RateLimitConfig(capacity=100, refill_per_second=100.0)
        ),
        hostname="testhost",
        id_factory=lambda: "a" * 32,
    )


def test_memory_create_and_edit_are_staged_outside_the_catalog(
    tmp_path: Path, catalog: Path, scheduler: RefreshScheduler, replaces: Any
) -> None:
    service = _memory_service(tmp_path, catalog, scheduler)
    target = catalog / "memories" / f"{'a' * 32}.md"
    payload = {
        "type": "architectural-fact",
        "scope": "global",
        "summary": "example fact",
        "evidence": [{"commit": "abc123"}],
    }

    seen = replaces(target, set(), "")
    created = service.create_memory(payload, username="carol")
    assert seen == [target]

    before = target.read_text("utf-8")
    replaces(target, {f"memories/{target.name}"}, before)
    service.edit_memory(
        created["id"],
        {**payload, "summary": "example fact, revised"},
        created["content_hash"],
        username="carol",
    )
    assert len(seen) == 2
    assert "revised" in target.read_text("utf-8")


def test_xray_pattern_store_is_staged_outside_the_catalog(
    catalog: Path, scheduler: RefreshScheduler, replaces: Any
) -> None:
    service = XrayPatternService(catalog, refresh_scheduler=scheduler)
    target = catalog / "xray-patterns" / "__any__" / "test-pattern.yaml"
    relative = str(target.relative_to(catalog))

    def _store(description: str) -> Dict[str, Any]:
        return service.store_xray_pattern_audited(
            scope="__any__",
            pattern_yaml=PATTERN_YAML.format(description=description),
            overwrite=True,
            actor="carol",
            may_replace_global=True,
        )

    seen = replaces(target, set(), "")
    assert _store("first").get("success") is True
    assert seen == [target]

    replaces(target, {relative}, PATTERN_YAML.format(description="first"))
    assert _store("second").get("success") is True
    assert len(seen) == 2
    assert "second" in target.read_text("utf-8")


@pytest.fixture
def umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


MEMORY_PAYLOAD = {
    "type": "architectural-fact",
    "scope": "global",
    "summary": "example fact",
    "evidence": [{"commit": "abc123"}],
}


def test_memory_files_stay_owner_only(
    tmp_path: Path, catalog: Path, scheduler: RefreshScheduler, umask_022: None
) -> None:
    service = _memory_service(tmp_path, catalog, scheduler)
    target = catalog / "memories" / f"{'a' * 32}.md"

    created = service.create_memory(MEMORY_PAYLOAD, username="carol")
    assert _mode(target) == 0o600, "a memory became readable by other accounts"

    service.edit_memory(
        created["id"],
        {**MEMORY_PAYLOAD, "summary": "example fact, revised"},
        created["content_hash"],
        username="carol",
    )
    assert _mode(target) == 0o600, "an edited memory became readable by others"


def test_xray_pattern_and_description_files_stay_world_readable(
    catalog: Path, scheduler: RefreshScheduler, umask_022: None
) -> None:
    pattern = catalog / "xray-patterns" / "__any__" / "test-pattern.yaml"
    result = XrayPatternService(
        catalog, refresh_scheduler=scheduler
    ).store_xray_pattern_audited(
        scope="__any__",
        pattern_yaml=PATTERN_YAML.format(description="first"),
        overwrite=True,
        actor="carol",
        may_replace_global=True,
    )
    assert result.get("success") is True, result
    assert _mode(pattern) == 0o644

    description = catalog / "example-repo.md"
    meta_description_hook.atomic_write_description(description, "# example-repo\n")
    assert _mode(description) == 0o644


def test_staging_directory_is_no_wider_than_a_plain_mkdir(
    catalog: Path, umask_022: None
) -> None:
    meta_description_hook.atomic_write_description(
        catalog / "example-repo.md", "# example-repo\n"
    )

    assert _mode(catalog.parent / STAGING_DIR_NAME) == 0o755
