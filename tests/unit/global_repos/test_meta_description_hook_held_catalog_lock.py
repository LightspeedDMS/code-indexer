"""A registered or removed repository's description change is never lost to a
held catalog write lock, and the catalog is refreshed afterwards.

Whoever holds the cidx-meta write lock past the hook's short bounded retry (a
catalog refresh across indexing and publish, an operator's write mode, the
dependency map...), the description is written (or removed) atomically and
the refresh that follows is owed to the debouncer: a refresh requested while
the lock is held would skip and owe nothing. The same holds when the hook's
own lock release fails. The temp file of a description write is staged
outside cidx-meta, so neither a snapshot copy nor a backup commit sees it.

Real hook, real files, a real WriteLockManager lock file; only the Claude CLI
boundary and the server config are replaced.
"""

from __future__ import annotations

import logging
import os
import stat
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos import meta_description_hook
from code_indexer.global_repos.write_lock_manager import WriteLockManager

REPO = "example-repo"
HOLDERS = ["refresh_scheduler", "mcp_write_mode"]


class _Scheduler:
    """Scheduler double whose catalog lock is a real WriteLockManager."""

    def __init__(self, golden: Path, release: Optional[Callable[[], bool]] = None):
        self.write_lock_manager = WriteLockManager(golden)
        self.triggers: List[str] = []
        self._release = release

    def acquire_write_lock(self, alias: str, owner_name: str) -> bool:
        return self.write_lock_manager.acquire(alias, owner_name=owner_name)

    def release_write_lock(self, alias: str, owner_name: str) -> bool:
        if self._release is not None:
            return self._release()
        return self.write_lock_manager.release(alias, owner_name=owner_name)

    def trigger_refresh_for_repo(self, alias_name: str) -> Optional[str]:
        self.triggers.append(alias_name)
        return "job-1"


class _CountingDebouncer:
    def __init__(self) -> None:
        self.signals = 0

    def signal_dirty(self) -> None:
        self.signals += 1


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    root = tmp_path / "golden-repos"
    (root / "cidx-meta").mkdir(parents=True)
    (root / REPO).mkdir()
    return root


@pytest.fixture
def debouncer() -> Iterator[_CountingDebouncer]:
    counting = _CountingDebouncer()
    meta_description_hook.set_debouncer(counting)  # type: ignore[arg-type]
    meta_description_hook.set_tracking_backend(None)
    meta_description_hook.set_scheduler(None)
    try:
        yield counting
    finally:
        meta_description_hook.set_refresh_scheduler(None)
        meta_description_hook.set_debouncer(None)


def _wire(scheduler: _Scheduler) -> _Scheduler:
    meta_description_hook.set_refresh_scheduler(scheduler)
    return scheduler


def _register(golden: Path, generated: List[str]) -> None:
    def _generate(*args: Any, **kwargs: Any) -> str:
        generated.append(REPO)
        return "# example-repo\n\nGenerated description.\n"

    cli_manager = MagicMock()
    cli_manager.check_cli_available.return_value = True
    server_config = MagicMock()
    server_config.claude_integration_config.dep_map_fact_check_enabled = False
    config_service = MagicMock()
    config_service.get_config.return_value = server_config
    with (
        patch.object(
            meta_description_hook, "get_claude_cli_manager", return_value=cli_manager
        ),
        patch.object(meta_description_hook, "_generate_repo_description", _generate),
        patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=config_service,
        ),
    ):
        meta_description_hook.on_repo_added(
            REPO,
            "https://example.com/example-repo.git",
            str(golden / REPO),
            str(golden),
        )


def _description(golden: Path) -> Path:
    return golden / "cidx-meta" / f"{REPO}.md"


@pytest.mark.parametrize("holder", HOLDERS)
def test_registration_under_a_held_lock_writes_and_owes_the_refresh(
    golden: Path, debouncer: _CountingDebouncer, holder: str
) -> None:
    scheduler = _wire(_Scheduler(golden))
    assert scheduler.write_lock_manager.acquire("cidx-meta", owner_name=holder)
    generated: List[str] = []

    _register(golden, generated)

    assert _description(golden).read_text(encoding="utf-8").startswith("# example-repo")
    assert generated == [REPO]
    assert scheduler.triggers == []
    assert debouncer.signals == 1
    info = scheduler.write_lock_manager.get_lock_info("cidx-meta")
    assert info is not None and info["owner"] == holder


@pytest.mark.parametrize("holder", HOLDERS)
def test_removal_under_a_held_lock_deletes_and_owes_the_refresh(
    golden: Path, debouncer: _CountingDebouncer, holder: str
) -> None:
    scheduler = _wire(_Scheduler(golden))
    _description(golden).write_text("# example-repo\n", encoding="utf-8")
    assert scheduler.write_lock_manager.acquire("cidx-meta", owner_name=holder)

    meta_description_hook.on_repo_removed(REPO, str(golden))

    assert not _description(golden).exists()
    assert scheduler.triggers == []
    assert debouncer.signals == 1


def _raise_release() -> bool:
    raise OSError("lock store unavailable")


@pytest.mark.parametrize(
    "release", [lambda: False, _raise_release], ids=["returns_false", "raises"]
)
def test_failed_release_after_registration_owes_the_refresh(
    golden: Path, debouncer: _CountingDebouncer, release: Callable[[], bool]
) -> None:
    scheduler = _wire(_Scheduler(golden, release=release))

    _register(golden, [])

    assert _description(golden).is_file()
    assert scheduler.triggers == []
    assert debouncer.signals == 1


@pytest.mark.parametrize(
    "release", [lambda: False, _raise_release], ids=["returns_false", "raises"]
)
def test_failed_release_after_removal_owes_the_refresh(
    golden: Path, debouncer: _CountingDebouncer, release: Callable[[], bool]
) -> None:
    scheduler = _wire(_Scheduler(golden, release=release))
    _description(golden).write_text("# example-repo\n", encoding="utf-8")

    meta_description_hook.on_repo_removed(REPO, str(golden))

    assert not _description(golden).exists()
    assert scheduler.triggers == []
    assert debouncer.signals == 1


def _staging(golden: Path) -> Path:
    return golden / ".cidx-meta-staging"


def test_stale_staging_leftovers_are_swept(
    golden: Path, debouncer: _CountingDebouncer
) -> None:
    # A prior process crashed mid-write and left its temp file behind.
    _wire(_Scheduler(golden))
    staging = _staging(golden)
    staging.mkdir()
    old = time.time() - 16 * 60
    stale = staging / ".other-repo.md.abc123.tmp"
    stale.write_text("partial", encoding="utf-8")
    os.utime(stale, (old, old))
    fresh = staging / ".busy-repo.md.def456.tmp"
    fresh.write_text("being written", encoding="utf-8")
    unrelated = staging / "keep.txt"
    unrelated.write_text("not a staging temp", encoding="utf-8")
    os.utime(unrelated, (old, old))

    _register(golden, [])

    assert not stale.exists()
    assert fresh.exists()
    assert unrelated.exists()
    assert _description(golden).is_file()


def test_description_is_world_readable_under_a_restrictive_umask(
    golden: Path, debouncer: _CountingDebouncer
) -> None:
    _wire(_Scheduler(golden))
    previous = os.umask(0o077)
    try:
        _register(golden, [])
    finally:
        os.umask(previous)

    assert stat.S_IMODE(_description(golden).stat().st_mode) == 0o644


def test_failed_directory_fsync_keeps_the_write_and_owes_the_refresh(
    golden: Path,
    debouncer: _CountingDebouncer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = _wire(_Scheduler(golden))
    real_fsync = os.fsync
    calls: List[int] = []

    def _fsync(fd: int) -> None:
        calls.append(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(meta_description_hook.os, "fsync", _fsync)

    with caplog.at_level(logging.WARNING):
        _register(golden, [])

    assert _description(golden).is_file()
    assert scheduler.triggers == []
    assert debouncer.signals == 1
    assert any("directory fsync failed" in r.getMessage() for r in caplog.records)


def test_description_temp_file_is_staged_outside_cidx_meta(
    golden: Path, debouncer: _CountingDebouncer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(_Scheduler(golden))
    catalog = golden / "cidx-meta"
    seen: Dict[str, Any] = {}
    real_replace = os.replace

    def _spy_replace(src: Any, dst: Any) -> None:
        if Path(dst) == _description(golden):
            seen["src"] = Path(src)
            seen["catalog_at_replace"] = sorted(p.name for p in catalog.iterdir())
        real_replace(src, dst)

    monkeypatch.setattr(meta_description_hook.os, "replace", _spy_replace)

    _register(golden, [])

    assert seen["src"].parent.resolve() != catalog.resolve()
    assert seen["src"].parent.resolve().parent == golden.resolve()
    assert seen["catalog_at_replace"] == []
    assert sorted(p.name for p in catalog.iterdir()) == [f"{REPO}.md"]
