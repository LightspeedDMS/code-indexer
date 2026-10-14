"""``add_index_to_golden_repo``'s post-loop publish swaps the alias WITH the
published index version (``RefreshScheduler.published_swap_extra``), so the
new snapshot is servable on every pod under a delegated IndexStore; under the
local store the swap writes today's document.

Reuses the real add-index background worker harness of
test_golden_repo_manager_add_index_chunk_store_cache_eviction_1775.py
(real GoldenRepoManager, real RefreshScheduler + AliasManager; only the cidx
subprocesses and ``_create_snapshot`` are doubled).
"""

from pathlib import Path
from typing import Any

import pytest

from code_indexer.utils.index_store import (
    DelegatedIndexStore,
    published_index_fields,
)
from tests.unit.server.index_store_fakes import FakeArtifacts
from tests.unit.server.repositories.test_golden_repo_manager_add_index_chunk_store_cache_eviction_1775 import (
    _fake_subprocess_boundaries as _cidx_subprocess_doubles,
    _make_manager_with_real_repo_and_scheduler,
    _make_versioned_snapshot,
)

ALIAS = "click-global"

# The 1775 harness's autouse fixture (doubles the cidx init/index
# subprocesses), registered in this module under its own name.
_fake_subprocess_boundaries = _cidx_subprocess_doubles
pytestmark = pytest.mark.usefixtures("_fake_subprocess_boundaries")


def _run(tmp_path: Path, delegated: bool) -> Any:
    _, _, old_dir = _make_versioned_snapshot(tmp_path, "click", "v_1", "p1")
    new_versioned = str(tmp_path / ".versioned" / "click" / "v_2000000")
    manager = _make_manager_with_real_repo_and_scheduler(
        tmp_path, old_dir, new_versioned
    )
    scheduler = manager._refresh_scheduler
    if delegated:
        scheduler._index_store = DelegatedIndexStore(FakeArtifacts(tmp_path / "s"))
        scheduler._pending_index_publications[ALIAS] = (
            new_versioned,
            published_index_fields("click", "v7"),
        )
    manager.add_index_to_golden_repo(
        alias="click", index_type="semantic", submitter_username="admin"
    )
    return scheduler.alias_manager.read_record(ALIAS), new_versioned


def test_a_delegated_add_index_swap_records_the_version(
    tmp_path: Path,
) -> None:
    document, new_versioned = _run(tmp_path, True)
    assert document["target_path"] == new_versioned
    assert document["index_version"] == "v7"
    assert document["index_store_alias"] == "click"


def test_a_local_add_index_swap_writes_todays_document(
    tmp_path: Path,
) -> None:
    document, new_versioned = _run(tmp_path, False)
    assert document["target_path"] == new_versioned
    assert "index_version" not in document and "index_store_alias" not in document
