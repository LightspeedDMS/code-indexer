"""Collision-safe version-id generation, standalone-CLI fallback (Story #1457 AC9).

GoldenRepoManager._cb_cow_snapshot's standalone-CLI fallback branch (used
when no VersionedSnapshotManager is wired -- e.g. test contexts or CLI usage)
builds ``v_{int(time.time())}`` with NO existence check, mirroring the same
collision bug snapshot_manager.py's create_snapshot had before its AC9 fix.
Two snapshot creations for the same alias within the same wall-clock second
must not collide/nest into an existing directory.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "golden-repos").mkdir()
    return str(d)


def test_cb_cow_snapshot_retries_to_fresh_path_on_collision(data_dir):
    manager = GoldenRepoManager(data_dir=data_dir)
    assert manager._snapshot_manager is None  # standalone-CLI fallback branch

    colliding_path = (
        Path(manager.golden_repos_dir) / ".versioned" / "my-repo" / "v_1700000000"
    )
    colliding_path.mkdir(parents=True)
    (colliding_path / "sentinel.txt").write_text("pre-existing snapshot content")

    with (
        patch("code_indexer.server.repositories.golden_repo_manager.time") as mock_time,
        patch("subprocess.run") as mock_run,
    ):
        mock_time.time.return_value = 1700000000
        result = manager._cb_cow_snapshot(
            "my-repo", "/some/base/clone", cow_timeout=60, cidx_fix_timeout=30
        )

    assert result != str(colliding_path), (
        "_cb_cow_snapshot must not reuse a colliding v_{ts} destination path"
    )
    assert Path(result).name != "v_1700000000"
    assert (colliding_path / "sentinel.txt").read_text() == (
        "pre-existing snapshot content"
    )
    # First subprocess.run call is the "cp --reflink=auto" invocation; its
    # destination argument must be the fresh, non-colliding path.
    cp_call_args = mock_run.call_args_list[0][0][0]
    assert cp_call_args[-1] == result
