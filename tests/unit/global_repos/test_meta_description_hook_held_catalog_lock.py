"""Registering a golden repository while the catalog write lock is held never
loses its description.

* Held by a catalog refresh (owner ``refresh_scheduler``, which holds it
  across indexing and publish): the description is written and the refresh
  it needs is owed to the debouncer -- a refresh requested now would skip
  on the held lock.
* Held by any other owner (an exclusive writer such as write mode): nothing
  is written under that lock; the description is handed to the durable
  description-refresh pipeline (its tracking row is made due now), which
  writes it under the lock later.

Real hook, real files, a real WriteLockManager lock file; only the Claude CLI
boundary and the server config are replaced. The description is generated
exactly once by the hook.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos import meta_description_hook
from code_indexer.global_repos.write_lock_manager import WriteLockManager

REPO = "example-repo"


class _Scheduler:
    """Scheduler double whose catalog lock is a real WriteLockManager."""

    def __init__(self, golden: Path) -> None:
        self.write_lock_manager = WriteLockManager(golden)
        self.triggers: List[str] = []

    def acquire_write_lock(self, alias: str, owner_name: str) -> bool:
        return self.write_lock_manager.acquire(alias, owner_name=owner_name)

    def release_write_lock(self, alias: str, owner_name: str) -> bool:
        return self.write_lock_manager.release(alias, owner_name=owner_name)

    def trigger_refresh_for_repo(self, alias_name: str) -> Optional[str]:
        self.triggers.append(alias_name)
        return "job-1"


class _CountingDebouncer:
    def __init__(self) -> None:
        self.signals = 0

    def signal_dirty(self) -> None:
        self.signals += 1


class _TrackingBackend:
    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}

    def upsert_tracking(self, repo_alias: str, **fields: Any) -> None:
        self.rows.setdefault(repo_alias, {}).update(fields)


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    root = tmp_path / "golden-repos"
    (root / "cidx-meta").mkdir(parents=True)
    (root / REPO).mkdir()
    return root


@pytest.fixture
def wired(golden: Path) -> Iterator[Dict[str, Any]]:
    scheduler = _Scheduler(golden)
    debouncer = _CountingDebouncer()
    tracking = _TrackingBackend()
    meta_description_hook.set_refresh_scheduler(scheduler)
    meta_description_hook.set_debouncer(debouncer)  # type: ignore[arg-type]
    meta_description_hook.set_tracking_backend(tracking)
    meta_description_hook.set_scheduler(None)
    try:
        yield {"scheduler": scheduler, "debouncer": debouncer, "tracking": tracking}
    finally:
        meta_description_hook.set_refresh_scheduler(None)
        meta_description_hook.set_debouncer(None)
        meta_description_hook.set_tracking_backend(None)


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


def test_registration_during_a_refresh_writes_and_owes_the_refresh(
    golden: Path, wired: Dict[str, Any]
) -> None:
    scheduler: _Scheduler = wired["scheduler"]
    assert scheduler.write_lock_manager.acquire(
        "cidx-meta", owner_name="refresh_scheduler"
    )
    generated: List[str] = []

    _register(golden, generated)

    description = golden / "cidx-meta" / f"{REPO}.md"
    assert description.read_text(encoding="utf-8").startswith("# example-repo")
    assert generated == [REPO]
    assert scheduler.triggers == []
    assert wired["debouncer"].signals == 1
    info = scheduler.write_lock_manager.get_lock_info("cidx-meta")
    assert info is not None and info["owner"] == "refresh_scheduler"


def test_removal_during_a_refresh_deletes_and_owes_the_refresh(
    golden: Path, wired: Dict[str, Any]
) -> None:
    scheduler: _Scheduler = wired["scheduler"]
    description = golden / "cidx-meta" / f"{REPO}.md"
    description.write_text("# example-repo\n", encoding="utf-8")
    assert scheduler.write_lock_manager.acquire(
        "cidx-meta", owner_name="refresh_scheduler"
    )

    meta_description_hook.on_repo_removed(REPO, str(golden))

    assert not description.exists()
    assert scheduler.triggers == []
    assert wired["debouncer"].signals == 1


def test_registration_under_an_exclusive_writer_is_handed_to_the_pipeline(
    golden: Path, wired: Dict[str, Any]
) -> None:
    scheduler: _Scheduler = wired["scheduler"]
    assert scheduler.write_lock_manager.acquire(
        "cidx-meta", owner_name="mcp_write_mode"
    )
    generated: List[str] = []
    before = datetime.now(timezone.utc)

    _register(golden, generated)

    assert not (golden / "cidx-meta" / f"{REPO}.md").exists()
    assert generated == [REPO]
    assert scheduler.triggers == []
    row = wired["tracking"].rows[REPO]
    assert row["status"] == "pending"
    assert datetime.fromisoformat(str(row["next_run"])) <= datetime.now(timezone.utc)
    assert datetime.fromisoformat(str(row["next_run"])) >= before
