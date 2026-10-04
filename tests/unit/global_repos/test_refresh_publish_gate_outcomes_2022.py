"""Bug #2022 Gap 4, review round 3 items 2 and 3: outcomes of the
PUBLISH-path integrity gate (the gate every successful indexing pass runs).

- An INCONCLUSIVE check (the check itself could not run: real SQLITE_IOERR)
  is never a strike: it keeps the cycle unpublished and backs off, and three
  of them must not quarantine a healthy repo.
- While an integrity outcome is unresolved (inconclusive pending, or
  strikes), the next eligible cycle re-runs the gate and publishes instead of
  taking the "No changes detected" shortcut -- otherwise the alias stays on
  the old snapshot forever.
- A restore copy happens only while the refresh still owns its write lock.

Real stores, real snapshot, real reflink restore. Only the embedding child
(``_index_source``) is replaced: it needs a provider, and the store it would
write is the real one under test.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from code_indexer.global_repos import refresh_failure_recovery as recovery
from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
)
from tests.utils.fatal_chunk_store_fixtures import (
    corrupt_btree_pages,
    diverge_store,
    make_journal_path_a_directory,
    quick_check_ok,
)
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from tests.utils.refresh_fatal_store_harness import (
    ALIAS,
    Harness,
    build_harness,
    iter_chain,
    sha256_of,
)

INCONCLUSIVE_CYCLES = 3


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


def _refresh(harness: Harness, force_reset: bool = False) -> Any:
    with patch.object(harness.scheduler, "_index_source"):
        return harness.scheduler._execute_refresh(ALIAS, force_reset=force_reset)


def test_inconclusive_checks_never_quarantine_and_recovery_publishes(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    diverge_store(harness.source_db)
    journal = make_journal_path_a_directory(harness.source_db)

    for _ in range(INCONCLUSIVE_CYCLES):
        result = _refresh(harness)
        assert result["success"] is False, result
    assert harness.strikes() == 0, "an unverifiable check counted as corruption"
    assert recovery.active_backoff_until(metadata, ALIAS) is not None
    assert harness.scheduler.alias_manager.read_alias(ALIAS) == str(harness.snapshot)

    shutil.rmtree(journal)
    result = _refresh(harness)

    assert result.get("message") == "Refresh complete", result
    assert harness.scheduler.alias_manager.read_alias(ALIAS) != str(harness.snapshot)
    assert recovery.active_backoff_until(metadata, ALIAS) is None


def test_unresolved_inconclusive_outcome_is_republished_not_shortcut(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean", git_remote=True)
    journal = make_journal_path_a_directory(harness.source_db)
    first = _refresh(harness, force_reset=True)
    assert first["success"] is False, first
    shutil.rmtree(journal)

    result = _refresh(harness)

    assert result.get("message") == "Refresh complete", result
    assert harness.scheduler.alias_manager.read_alias(ALIAS) != str(harness.snapshot)


def test_corrupt_store_without_snapshot_is_regated_not_shortcut(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="none", git_remote=True)
    corrupt_btree_pages(harness.source_db)
    first = _refresh(harness, force_reset=True)
    assert first["success"] is False, first
    assert harness.strikes() == 1

    result = _refresh(harness)

    assert result.get("message") != "No changes detected", result
    assert harness.strikes() == 2


class _LoseLockOnFirstCheck:
    """The real write-lock manager, except that a rival takes the lock right
    before this refresh's first ownership check."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.renew_calls = 0

    def renew(self, alias: str, owner_name: str, owner_token: Any = None) -> bool:
        self.renew_calls += 1
        if self.renew_calls == 1:
            self._real.release(alias, owner_name=owner_name)
        return bool(
            self._real.renew(alias, owner_name=owner_name, owner_token=owner_token)
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_publish_path_restore_requires_lock_ownership(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    corrupt_btree_pages(harness.source_db)
    corrupt_sha = sha256_of(harness.source_db)
    lock = _LoseLockOnFirstCheck(harness.scheduler.write_lock_manager)
    harness.scheduler.write_lock_manager = lock  # type: ignore[assignment]

    with pytest.raises(Exception) as raised:
        _refresh(harness)

    assert any(
        isinstance(e, AliasLockOwnershipLostError) for e in iter_chain(raised.value)
    ), repr(raised.value)
    assert sha256_of(harness.source_db) == corrupt_sha, "restored without the lock"
    assert not quick_check_ok(harness.source_db)
