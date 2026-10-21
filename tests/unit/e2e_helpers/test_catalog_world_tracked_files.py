"""The e2e catalog world never leaves a TRACKED change in the live catalog.

On a live server whose catalog is a git repository (the backup bootstrap
makes it one and commits ``.gitignore``), any tracked change the world
leaves in the golden working tree is copied by an activation's CoW clone,
which then logs "unexpected TRACKED changes" and fails the phase log audit.
These tests drive the real seed/restore helpers against a real git catalog
shaped like the one a live server holds after the backup bootstrap, and the
server's real catalog write lock.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import List

import pytest

from code_indexer.global_repos.write_lock_manager import WriteLockManager
from tests.e2e.cidx_meta_world import (
    CATALOG,
    catalog_head,
    restore_catalog,
    seed_catalog,
    snapshot_catalog,
)

_IDENTITY = ["-c", "user.name=e2e", "-c", "user.email=e2e@example.com"]
# What the backup bootstrap commits as the catalog's .gitignore.
_BOOTSTRAP_GITIGNORE = ".code-indexer/\n.snapshot-reader-leases/\n"
_SERVER_PATTERN = Path("xray-patterns") / "alpha-global" / "sample-pattern.yaml"
_WRITER = "xray-pattern:store:sample-pattern"
# Bounded wait proving the teardown is still blocked while the lock is held.
_BLOCKED_PROBE_SECONDS = 2.0
_JOIN_SECONDS = 60.0


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout


def _tracked_catalog(tmp_path: Path) -> Path:
    root = tmp_path / CATALOG
    root.mkdir()
    (root / ".gitignore").write_text(_BOOTSTRAP_GITIGNORE, encoding="utf-8")
    (root / "cidx-meta.md").write_text("# cidx-meta\n", encoding="utf-8")
    (root / "_domains.json").write_text("[]\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "master")
    _git(root, "add", "-A")
    _git(root, *_IDENTITY, "commit", "-qm", "auto: initial cidx-meta state")
    return root


def _tracked_changes(repo: Path) -> List[str]:
    """The activation's own check: refresh the index, then the tracked
    lines of ``git status --porcelain``."""
    refresh = subprocess.run(
        ["git", "update-index", "--refresh"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # ^ why exit 1 = a tracked file needs update (listed below); other = git failed
    assert refresh.returncode in (0, 1), (refresh.returncode, refresh.stderr)
    status = _git(repo, "status", "--porcelain")
    return [line for line in status.splitlines() if not line.startswith("??")]


def _server_commit(root: Path, relative: Path, content: str, message: str) -> None:
    """What a server writer does: write one file and commit exactly it."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(root, "add", "--", str(relative))
    _git(root, *_IDENTITY, "commit", "-qm", message, "--", str(relative))


def test_seeding_changes_no_tracked_catalog_file(tmp_path: Path) -> None:
    root = _tracked_catalog(tmp_path)

    seed_catalog(root)

    assert _tracked_changes(root) == []
    assert (root / ".gitignore").read_text(encoding="utf-8") == _BOOTSTRAP_GITIGNORE


def test_activation_copy_of_a_seeded_catalog_is_clean(tmp_path: Path) -> None:
    root = _tracked_catalog(tmp_path)
    seed_catalog(root)

    clone = tmp_path / "activated" / CATALOG
    shutil.copytree(root, clone, symlinks=True)

    assert _tracked_changes(clone) == []


def test_restore_after_a_server_commit_leaves_no_tracked_change(
    tmp_path: Path,
) -> None:
    root = _tracked_catalog(tmp_path)
    head = catalog_head(root)
    snapshot = snapshot_catalog(root)
    seed_catalog(root)
    _server_commit(root, _SERVER_PATTERN, "name: sample\n", "xray-patterns: add")

    restore_catalog(root, snapshot, head)

    assert not (root / _SERVER_PATTERN).exists()
    assert _tracked_changes(root) == []
    assert snapshot_catalog(root) == snapshot


def test_restore_after_a_server_update_to_a_snapshot_file_leaves_no_tracked_change(
    tmp_path: Path,
) -> None:
    root = _tracked_catalog(tmp_path)
    head = catalog_head(root)
    snapshot = snapshot_catalog(root)
    seed_catalog(root)
    _server_commit(
        root, Path("_domains.json"), '[{"name": "billing"}]\n', "auto: refresh"
    )

    restore_catalog(root, snapshot, head)

    assert _tracked_changes(root) == []
    assert snapshot_catalog(root) == snapshot


def test_restore_commits_no_tracked_change_the_world_did_not_make(
    tmp_path: Path,
) -> None:
    root = _tracked_catalog(tmp_path)
    # A server write not yet committed when the world starts.
    (root / "cidx-meta.md").write_text("# cidx-meta\n\nPending.\n", encoding="utf-8")
    head = catalog_head(root)
    snapshot = snapshot_catalog(root)
    seed_catalog(root)
    _server_commit(root, _SERVER_PATTERN, "name: sample\n", "xray-patterns: add")

    restore_catalog(root, snapshot, head)

    assert _tracked_changes(root) == [" M cidx-meta.md"]
    assert _git(root, "show", "HEAD:cidx-meta.md") == "# cidx-meta\n"
    assert snapshot_catalog(root) == snapshot


def test_restore_waits_for_a_writer_holding_the_catalog_lock(tmp_path: Path) -> None:
    root = _tracked_catalog(tmp_path)
    head = catalog_head(root)
    snapshot = snapshot_catalog(root)
    seed_catalog(root)
    writer = WriteLockManager(root.parent)
    assert writer.acquire(CATALOG, owner_name=_WRITER)
    errors: List[BaseException] = []

    def _teardown() -> None:
        try:
            restore_catalog(root, snapshot, head)
        except BaseException as exc:  # reported by the assertion below
            errors.append(exc)

    teardown = threading.Thread(target=_teardown, daemon=True)
    try:
        # The writer's own write and commit, under its lock.
        _server_commit(root, _SERVER_PATTERN, "name: sample\n", "xray-patterns: add")
        teardown.start()
        teardown.join(timeout=_BLOCKED_PROBE_SECONDS)
        assert teardown.is_alive(), f"teardown ran under the writer's lock: {errors}"
        assert (root / _SERVER_PATTERN).exists()
        assert (root / "README.md").exists()
    finally:
        assert writer.release(CATALOG, owner_name=_WRITER)
    teardown.join(timeout=_JOIN_SECONDS)

    assert not teardown.is_alive() and errors == []
    assert not (root / _SERVER_PATTERN).exists()
    assert _tracked_changes(root) == []
    assert snapshot_catalog(root) == snapshot
    assert not writer.is_locked(CATALOG)


def test_restore_refuses_loudly_when_the_catalog_lock_stays_held(
    tmp_path: Path,
) -> None:
    root = _tracked_catalog(tmp_path)
    head = catalog_head(root)
    snapshot = snapshot_catalog(root)
    seed_catalog(root)
    seeded = snapshot_catalog(root)
    writer = WriteLockManager(root.parent)
    assert writer.acquire(CATALOG, owner_name=_WRITER)
    try:
        with pytest.raises(TimeoutError, match=_WRITER):
            restore_catalog(root, snapshot, head, lock_wait_seconds=0.0)
        assert snapshot_catalog(root) == seeded
        assert catalog_head(root) == head
        assert writer.is_locked(CATALOG)
    finally:
        assert writer.release(CATALOG, owner_name=_WRITER)


def test_restore_of_an_untracked_catalog_needs_no_git(tmp_path: Path) -> None:
    # Phases 3 and 6: the catalog is a plain directory, not a repository.
    root = tmp_path / CATALOG
    root.mkdir()
    (root / "cidx-meta.md").write_text("# cidx-meta\n", encoding="utf-8")
    head = catalog_head(root)
    snapshot = snapshot_catalog(root)
    seed_catalog(root)

    restore_catalog(root, snapshot, head)

    assert head is None
    assert snapshot_catalog(root) == snapshot
    assert not (root / ".git").exists()
