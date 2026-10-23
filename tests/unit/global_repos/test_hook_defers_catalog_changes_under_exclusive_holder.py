"""A repository registered or removed while an exclusive owner (write mode,
the catalog backup) holds the cidx-meta write lock changes nothing in the
catalog: the description write or removal is parked as a durable pending
change outside the catalog and the refresh is owed. Once the holder
releases, the pending change is applied under the catalog lock -- by the
debouncer before it triggers the owed refresh -- so the description always
converges.

Real hook, real RefreshScheduler and lock (file lock, SQLite and, with
TEST_POSTGRES_DSN, PostgreSQL alias-lock stores), real debouncer; only the
Claude CLI boundary and the server config are replaced.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Iterator, List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos import meta_description_hook
from code_indexer.global_repos.catalog_pending_changes import (
    PENDING_DIR_NAME,
    apply_pending_catalog_changes,
)
from code_indexer.global_repos.cidx_meta_refresh_debouncer import (
    CidxMetaRefreshDebouncer,
)
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from tests.utils.catalog_lock_schedulers import (
    CATALOG,
    CATALOG_LOCK_KINDS,
    catalog_lock_scheduler,
)

REPO = "example-repo"
GENERATED = "# example-repo\n\nGenerated description.\n"
EXCLUSIVE = ["mcp_write_mode", "catalog_backup"]
WAIT_SECONDS = 30.0


class _CountingDebouncer:
    def __init__(self) -> None:
        self.signals = 0
        self.tracked: List[Tuple[str, int]] = []

    def signal_dirty(self) -> None:
        self.signals += 1

    def note_direct_write(self) -> int:
        return len(self.tracked) + 1

    def track_submission(self, job_id: str, seq: int) -> None:
        self.tracked.append((job_id, seq))


@pytest.fixture(params=CATALOG_LOCK_KINDS)
def scheduler(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[RefreshScheduler]:
    real = catalog_lock_scheduler(str(request.param), tmp_path)
    (Path(real.golden_repos_dir) / REPO).mkdir()

    # Any: stands in for RefreshScheduler.trigger_refresh_for_repo's signature.
    def _trigger(self: Any, alias: str, *a: Any, **k: Any) -> str:
        return "job-1"

    monkeypatch.setattr(RefreshScheduler, "trigger_refresh_for_repo", _trigger)
    meta_description_hook.set_refresh_scheduler(real)
    meta_description_hook.set_tracking_backend(None)
    meta_description_hook.set_scheduler(None)
    try:
        yield real
    finally:
        meta_description_hook.set_refresh_scheduler(None)
        meta_description_hook.set_debouncer(None)


def _golden(scheduler: RefreshScheduler) -> Path:
    return Path(scheduler.golden_repos_dir)


def _description(scheduler: RefreshScheduler) -> Path:
    return _golden(scheduler) / CATALOG / f"{REPO}.md"


def _pending(scheduler: RefreshScheduler) -> List[str]:
    pending = _golden(scheduler) / PENDING_DIR_NAME
    return sorted(p.name for p in pending.iterdir()) if pending.exists() else []


def _register(scheduler: RefreshScheduler) -> None:
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
        patch.object(
            meta_description_hook,
            "_generate_repo_description",
            lambda *a, **k: GENERATED,
        ),
        patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=config_service,
        ),
    ):
        meta_description_hook.on_repo_added(
            REPO,
            "https://example.com/example-repo.git",
            str(_golden(scheduler) / REPO),
            str(_golden(scheduler)),
        )


@pytest.mark.parametrize("holder", EXCLUSIVE)
def test_registration_under_an_exclusive_holder_is_parked_then_applied(
    scheduler: RefreshScheduler, holder: str
) -> None:
    debouncer = _CountingDebouncer()
    meta_description_hook.set_debouncer(debouncer)  # type: ignore[arg-type]
    locks = scheduler.write_lock_manager
    assert locks.acquire(CATALOG, owner_name=holder)
    try:
        _register(scheduler)
        assert not _description(scheduler).exists(), "written under " + holder
        assert _pending(scheduler) == [f"{REPO}.md"]
        assert debouncer.signals == 1, "the refresh was not owed"
        info = locks.get_lock_info(CATALOG)
        assert info and info["owner"] == holder
        assert apply_pending_catalog_changes(scheduler) is False
    finally:
        assert locks.release(CATALOG, owner_name=holder)

    assert apply_pending_catalog_changes(scheduler) is True
    assert _description(scheduler).read_text() == GENERATED
    assert _pending(scheduler) == []
    assert locks.get_lock_info(CATALOG) is None


@pytest.mark.parametrize("holder", EXCLUSIVE)
def test_removal_under_an_exclusive_holder_is_parked_then_applied(
    scheduler: RefreshScheduler, holder: str
) -> None:
    debouncer = _CountingDebouncer()
    meta_description_hook.set_debouncer(debouncer)  # type: ignore[arg-type]
    _description(scheduler).write_text(GENERATED)
    locks = scheduler.write_lock_manager
    assert locks.acquire(CATALOG, owner_name=holder)
    try:
        meta_description_hook.on_repo_removed(REPO, str(_golden(scheduler)))
        assert _description(scheduler).exists(), "removed under " + holder
        assert _pending(scheduler) == [f"{REPO}.remove"]
        assert debouncer.signals == 1, "the refresh was not owed"
    finally:
        assert locks.release(CATALOG, owner_name=holder)

    assert apply_pending_catalog_changes(scheduler) is True
    assert not _description(scheduler).exists()
    assert _pending(scheduler) == []


def test_the_debouncer_applies_the_parked_change_before_the_refresh(
    scheduler: RefreshScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied_at_trigger: List[bool] = []
    triggered = threading.Event()

    def _trigger(self: Any, alias: str, *a: Any, **k: Any) -> str:
        applied_at_trigger.append(_description(scheduler).exists())
        triggered.set()
        return "job-1"

    monkeypatch.setattr(RefreshScheduler, "trigger_refresh_for_repo", _trigger)
    debouncer = CidxMetaRefreshDebouncer(scheduler, debounce_seconds=0.05)
    meta_description_hook.set_debouncer(debouncer)
    locks = scheduler.write_lock_manager
    try:
        assert locks.acquire(CATALOG, owner_name="mcp_write_mode")
        try:
            _register(scheduler)
            assert not triggered.wait(0.5), "refreshed while the change was parked"
        finally:
            assert locks.release(CATALOG, owner_name="mcp_write_mode")
        assert triggered.wait(WAIT_SECONDS), "the parked change was never applied"
    finally:
        debouncer.shutdown()

    assert applied_at_trigger[0] is True
    assert _description(scheduler).read_text() == GENERATED
    assert _pending(scheduler) == []
