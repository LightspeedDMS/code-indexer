"""Bug #2022 Gap 4, review round 1 item 2: the persisted refresh failure
backoff belongs to the repo, not to its name. Removing a golden repo must
delete its backoff in the same transaction, so a repo later registered
under the same alias starts clean. Real stores: SQLite always, PostgreSQL
when ``TEST_POSTGRES_DSN`` is set."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)

BARE = "repo-x"
GLOBAL = "repo-x-global"
OTHER_GLOBAL = "repo-y-global"


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


def _register(metadata: Any, repo_url: str) -> None:
    metadata.add_repo(
        alias=BARE,
        repo_url=repo_url,
        default_branch="main",
        clone_path=f"/data/golden-repos/{BARE}",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def test_removed_alias_does_not_bequeath_its_backoff(metadata: Any) -> None:
    _register(metadata, "https://git.example.com/org/first.git")
    metadata.record_refresh_failure_backoff(GLOBAL, "disk full")
    metadata.record_refresh_failure_backoff(BARE, "disk full")
    metadata.record_refresh_failure_backoff(OTHER_GLOBAL, "disk full")

    assert metadata.remove_repo(BARE) is True
    _register(metadata, "https://git.example.com/org/second.git")

    assert metadata.get_refresh_failure_backoff_state(GLOBAL) is None
    assert metadata.get_refresh_failure_backoff_state(BARE) is None
    assert metadata.get_refresh_failure_backoff_state(OTHER_GLOBAL) is not None


def test_removing_unknown_alias_keeps_other_backoff(metadata: Any) -> None:
    metadata.record_refresh_failure_backoff(OTHER_GLOBAL, "disk full")

    assert metadata.remove_repo(BARE) is False

    assert metadata.get_refresh_failure_backoff_state(OTHER_GLOBAL) is not None
