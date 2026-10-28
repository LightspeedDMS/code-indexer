"""A refresh result states ``covered_source`` only when the refresh covered
the repository's source (it published, or verified nothing changed). Every
early return that skips the source must not state it: the cidx-meta
debouncer settles an owed catalog write only on that statement.

Real scheduler, registry, alias files and write lock (the harness); the
covering returns are proven with the real CLI in
test_local_source_identity_detection.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from tests.utils.golden_repo_metadata_stores import golden_repo_metadata_store
from tests.utils.refresh_fatal_store_harness import (
    ALIAS,
    REPO,
    Harness,
    build_harness,
)

pytestmark = pytest.mark.slow


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    with golden_repo_metadata_store("sqlite", tmp_path) as metadata:
        yield build_harness(tmp_path, metadata, snapshot_mode="clean")


def _assert_not_covering(result: Dict[str, Any], message: str) -> None:
    assert result["message"] == message, result
    assert "covered_source" not in result, result


def test_alias_not_found(harness: Harness) -> None:
    result = harness.scheduler._execute_refresh("absent-repo-global")

    _assert_not_covering(result, "Alias not found, skipped")


def test_repo_not_in_registry(harness: Harness) -> None:
    harness.scheduler.alias_manager.create_alias(
        "unregistered-global", str(harness.source), repo_name="unregistered"
    )

    result = harness.scheduler._execute_refresh("unregistered-global")

    _assert_not_covering(result, "Repo not in registry, skipped")


def test_local_repo_not_yet_initialized(harness: Harness) -> None:
    bare = harness.source.parent / "uninitialized"
    bare.mkdir()
    (bare / "note.md").write_text("note\n")
    harness.registry.register_global_repo(
        "uninitialized",
        "uninitialized-global",
        "local://uninitialized",
        index_path=str(bare),
    )
    harness.scheduler.alias_manager.create_alias(
        "uninitialized-global", str(bare), repo_name="uninitialized"
    )

    result = harness.scheduler._execute_refresh("uninitialized-global")

    _assert_not_covering(result, "Not yet initialized, skipped")


def test_write_lock_held_by_another_owner(harness: Harness) -> None:
    assert harness.scheduler.acquire_write_lock(REPO, "dependency_map_service")
    try:
        result = harness.scheduler._execute_refresh(ALIAS)
    finally:
        harness.scheduler.release_write_lock(REPO, "dependency_map_service")

    _assert_not_covering(result, "Skipped, write lock held")
