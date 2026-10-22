"""A lease renewal racing its release never recreates the released lease.

Real SnapshotReaderLease and real lease files. The renewing thread is held
just before its os.replace (the real os.replace runs once it is let go), so
the release lands exactly inside the renewal's write window.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from code_indexer.global_repos import snapshot_reader_lease
from code_indexer.global_repos.snapshot_reader_lease import (
    SnapshotReaderLease,
    ensure_primary_lease_directory,
)

_TTL_SECONDS = 120.0
_JOIN_TIMEOUT_SECONDS = 10
_RELEASE_HEAD_START_SECONDS = 0.3


def test_release_during_renewal_leaves_no_lease_file(tmp_path: Path, monkeypatch):
    lease_root = tmp_path / "golden-repos" / "cidx-meta"
    lease_root.mkdir(parents=True)
    lease_dir = ensure_primary_lease_directory(lease_root)
    snapshot = tmp_path / "golden-repos" / ".versioned" / "example-repo" / "v_1"
    snapshot.mkdir(parents=True)
    lease = SnapshotReaderLease(str(snapshot), _TTL_SECONDS, lease_root=lease_root)
    lease.acquire()

    in_write_window, let_go = threading.Event(), threading.Event()
    real_replace = os.replace

    def held_replace(src, dst):
        in_write_window.set()
        assert let_go.wait(_JOIN_TIMEOUT_SECONDS)
        real_replace(src, dst)

    monkeypatch.setattr(snapshot_reader_lease.os, "replace", held_replace)
    renewer = threading.Thread(target=lease.renew)
    releaser = threading.Thread(target=lease.release)
    renewer.start()
    assert in_write_window.wait(_JOIN_TIMEOUT_SECONDS)
    releaser.start()
    releaser.join(_RELEASE_HEAD_START_SECONDS)
    let_go.set()
    renewer.join(_JOIN_TIMEOUT_SECONDS)
    releaser.join(_JOIN_TIMEOUT_SECONDS)

    assert not renewer.is_alive() and not releaser.is_alive()
    assert list(lease_dir.glob("*.json")) == []


@pytest.mark.parametrize("renew_first", [True, False])
def test_renew_and_release_in_either_order_end_released(
    tmp_path: Path, renew_first: bool
):
    lease_root = tmp_path / "cidx-meta"
    lease_root.mkdir()
    lease_dir = ensure_primary_lease_directory(lease_root)
    lease = SnapshotReaderLease(
        str(tmp_path / "v_1"), _TTL_SECONDS, lease_root=lease_root
    )
    lease.acquire()
    for step in (
        (lease.renew, lease.release) if renew_first else (lease.release, lease.renew)
    ):
        step()
    assert list(lease_dir.glob("*.json")) == []
