"""Bug #1529 review item 5: the bare-name legacy monolith blind spot.

``TEMPORAL_COLLECTION_PREFIX`` is ``"code-indexer-temporal-"`` -- WITH the
trailing hyphen -- so ``parse_physical_temporal_name`` returns None for the
bare pre-Story-#1457 monolith directory name ``code-indexer-temporal``. Every
parser-based scan therefore cannot see that shape at all.

That was already fixed for the DESTRUCTIVE decision
(``temporal_reindex_needs_clear`` composes a name-glob scan alongside the
parser-based status helper for exactly this reason), but two surfaces were
left parser-only:

  * ``temporal_status._scan_root`` -- so a row-bearing bare monolith in the
    FIXED server root is invisible to every status surface. It is also the
    residual hole in the destructive decision: the glob half of that
    composition only scans the IN-REPO root, so a bare monolith sitting in
    the fixed root is seen by NEITHER half and ``--clear`` would wipe it.
  * ``completion_gate.repo_temporal_dirs_fully_consolidated`` -- so a repo
    still holding an unmigrated legacy monolith reports "fully consolidated"
    and fires the AC10 post-consolidation snapshot over it.

The rowless bare directory must still be ignored by both: that is the shared
bookkeeping directory ``TemporalIndexer`` creates to anchor the single shared
``TemporalMetadataStore`` (Bug #1405), never a shard. Row presence is the
discriminator, which is why each case below is paired with a rowless control.

Real directories, real files -- nothing mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

from code_indexer.server.services.fleet_migration.completion_gate import (
    repo_temporal_dirs_fully_consolidated,
)
from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)
from code_indexer.services.temporal.temporal_status import get_temporal_repo_status

BARE_MONOLITH_NAME = "code-indexer-temporal"
REPO_ALIAS = "evolution"
VECTOR_DIM = 4


def _write_legacy_row(shard_dir: Path) -> None:
    """Write one real ``vector_*.json`` row in the 4-level hash-sharded shape.

    The shape matters: the row-existence primitive rglobs, so a row parked at
    the top level would pass a check that the real nested layout would fail.
    """
    leaf = shard_dir / "a" / "b" / "c" / "d"
    leaf.mkdir(parents=True, exist_ok=True)
    (leaf / "vector_abc123.json").write_text(
        json.dumps(
            {
                "id": "proj:commit:abc123:0",
                "vector": [0.1] * VECTOR_DIM,
                "chunk_text": "legacy monolith row",
            }
        )
    )


def _make_legacy_collection_meta(shard_dir: Path) -> None:
    """The metadata every real (non-bookkeeping) collection directory has."""
    (shard_dir / "collection_meta.json").write_text(
        json.dumps({"vector_size": VECTOR_DIM, "vector_dim": VECTOR_DIM})
    )


def test_row_bearing_bare_monolith_in_the_fixed_root_is_reported_as_data(
    tmp_path: Path,
) -> None:
    """A bare monolith holding rows is DATA, whatever its name parses as.

    Reported through the fixed server root specifically, because that is the
    location neither half of the destructive ``--clear`` composition can
    currently see.
    """
    golden_repos_dir = tmp_path / "golden-repos"
    fixed_root = server_temporal_index_root(golden_repos_dir, REPO_ALIAS)
    monolith = fixed_root / BARE_MONOLITH_NAME
    monolith.mkdir(parents=True)
    _make_legacy_collection_meta(monolith)
    _write_legacy_row(monolith)

    in_repo_index = tmp_path / "clone" / ".code-indexer" / "index"
    in_repo_index.mkdir(parents=True)

    status = get_temporal_repo_status(golden_repos_dir, REPO_ALIAS, in_repo_index)

    assert status.has_data, (
        "a bare code-indexer-temporal monolith holding real rows in the "
        "fixed server root was reported as no temporal data at all"
    )
    assert status.resolved_path == monolith


def test_rowless_bare_bookkeeping_directory_is_still_not_data(
    tmp_path: Path,
) -> None:
    """Discriminating control: the Bug #1405 bookkeeping dir is NOT a shard.

    Without this, reporting every bare directory as data would satisfy the
    test above while making every temporal-enabled repo look permanently
    populated -- the shared metadata store's anchor directory always exists.
    """
    golden_repos_dir = tmp_path / "golden-repos"
    fixed_root = server_temporal_index_root(golden_repos_dir, REPO_ALIAS)
    (fixed_root / BARE_MONOLITH_NAME).mkdir(parents=True)

    in_repo_index = tmp_path / "clone" / ".code-indexer" / "index"
    in_repo_index.mkdir(parents=True)

    status = get_temporal_repo_status(golden_repos_dir, REPO_ALIAS, in_repo_index)

    assert not status.has_data
    assert status.resolved_path is None


def test_completion_gate_refuses_a_row_bearing_bare_monolith(tmp_path: Path) -> None:
    """An unmigrated legacy monolith must never read as fully consolidated.

    Declaring the repo done here publishes an AC10 snapshot over real,
    still-legacy row data -- the same failure mode the gate already refuses
    for a metadata-less directory that holds rows.
    """
    index_path = tmp_path / ".code-indexer" / "index"
    monolith = index_path / BARE_MONOLITH_NAME
    monolith.mkdir(parents=True)
    _make_legacy_collection_meta(monolith)
    _write_legacy_row(monolith)

    assert not repo_temporal_dirs_fully_consolidated(index_path), (
        "the completion gate reported a repo done while a row-bearing legacy "
        "code-indexer-temporal monolith was still in the sharded layout"
    )


def test_completion_gate_ignores_the_rowless_bookkeeping_directory(
    tmp_path: Path,
) -> None:
    """Discriminating control for the gate.

    The bookkeeping directory exists in EVERY temporal-enabled repo, so
    failing the gate on its mere presence would make completion unreachable
    forever -- the exact trap Bug #1528 rule 5 documents.
    """
    index_path = tmp_path / ".code-indexer" / "index"
    (index_path / BARE_MONOLITH_NAME).mkdir(parents=True)

    assert repo_temporal_dirs_fully_consolidated(index_path)
