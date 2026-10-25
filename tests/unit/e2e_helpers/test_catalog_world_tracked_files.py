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

from code_indexer.global_repos.alias_lock_coordinator import AliasLockCoordinator
from code_indexer.global_repos.write_lock_manager import WriteLockManager
from code_indexer.server.services.alias_lock_store.base import AliasLockStore
from tests.e2e.cidx_meta_world import (
    CATALOG,
    catalog_head,
    catalog_write_lock,
    restore_catalog,
    seed_catalog,
    snapshot_catalog,
    sqlite_alias_lock_store,
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


def _lock_store(tmp_path: Path) -> AliasLockStore:
    """The alias lock store of a solo server whose CIDX_DATA_DIR is in tmp."""
    return sqlite_alias_lock_store(tmp_path / "server-home")


def _server_locks(root: Path, store: AliasLockStore) -> AliasLockCoordinator:
    """The live server's catalog lock: DB-backed, the AliasLockConfig default."""
    return AliasLockCoordinator(
        root.parent, db_backed_enabled_getter=lambda: True, store_resolver=lambda: store
    )


def test_sqlite_store_sees_the_lock_of_a_server_with_that_data_dir(
    tmp_path: Path,
) -> None:
    from code_indexer.server.services.alias_lock_store.sqlite_store import (
        SqliteAliasLockStore,
    )

    home = tmp_path / "server-home"
    # What a solo server with CIDX_DATA_DIR=home builds (factory rule).
    server_store = SqliteAliasLockStore(home / "data" / "alias_locks")
    handle = server_store.try_acquire(
        CATALOG, operation=_WRITER, owner_token="server-token", ttl_seconds=60
    )
    assert handle is not None

    assert sqlite_alias_lock_store(home).is_held(CATALOG)
    server_store.release(handle)
    assert not sqlite_alias_lock_store(home).is_held(CATALOG)


def test_postgres_store_refuses_a_server_not_in_postgres_mode(
    tmp_path: Path,
) -> None:
    import json

    from code_indexer.server.services.alias_lock_store.postgres_store import (
        PostgresAliasLockStore,
    )
    from tests.e2e.cidx_meta_world import postgres_alias_lock_store

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"storage_mode": "sqlite"}), encoding="utf-8")
    with pytest.raises(AssertionError, match="postgres"):
        postgres_alias_lock_store(tmp_path)

    config.write_text(
        json.dumps(
            {"storage_mode": "postgres", "postgres_dsn": "postgresql:///example"}
        ),
        encoding="utf-8",
    )
    assert isinstance(postgres_alias_lock_store(tmp_path), PostgresAliasLockStore)


def test_held_catalog_lock_refuses_the_server_without_a_file_lock(
    tmp_path: Path,
) -> None:
    root = _tracked_catalog(tmp_path)
    store = _lock_store(tmp_path)
    server = _server_locks(root, store)

    with catalog_write_lock(root, store):
        assert not server.acquire(CATALOG, owner_name=_WRITER, heartbeat=False)
        # A file lock makes the server's DB acquire roll back with a WARNING.
        assert not WriteLockManager(root.parent).is_locked(CATALOG)
        holder = store.get_holder(CATALOG)
        assert holder is not None and holder["owner"] == "e2e_catalog_world"

    assert not store.is_held(CATALOG)
    assert server.acquire(CATALOG, owner_name=_WRITER, heartbeat=False)
    assert server.release(CATALOG, owner_name=_WRITER)


def test_teardown_fails_loudly_when_the_lock_row_vanished(tmp_path: Path) -> None:
    import sqlite3

    from code_indexer.server.services.alias_lock_store.sqlite_store import (
        SqliteAliasLockStore,
        _db_path_for_lock_key,
    )

    root = _tracked_catalog(tmp_path)
    store = _lock_store(tmp_path)
    assert isinstance(store, SqliteAliasLockStore)
    lock_db = _db_path_for_lock_key(
        tmp_path / "server-home" / "data" / "alias_locks", CATALOG
    )

    with pytest.raises(RuntimeError, match="lost"):
        with catalog_write_lock(root, store):
            assert store.is_held(CATALOG)
            conn = sqlite3.connect(str(lock_db))
            try:
                with conn:
                    conn.execute("DELETE FROM golden_repo_alias_locks")
            finally:
                conn.close()

    assert not store.is_held(CATALOG)


def test_restore_waits_for_a_db_backed_writer_holding_the_catalog_lock(
    tmp_path: Path,
) -> None:
    root = _tracked_catalog(tmp_path)
    head = catalog_head(root)
    snapshot = snapshot_catalog(root)
    seed_catalog(root)
    store = _lock_store(tmp_path)
    writer = _server_locks(root, store)
    assert writer.acquire(CATALOG, owner_name=_WRITER, heartbeat=False)
    errors: List[BaseException] = []

    def _teardown() -> None:
        try:
            restore_catalog(root, store, snapshot, head)
        except BaseException as exc:  # reported by the assertion below
            errors.append(exc)

    teardown = threading.Thread(target=_teardown, daemon=True)
    try:
        _server_commit(root, _SERVER_PATTERN, "name: sample\n", "xray-patterns: add")
        teardown.start()
        teardown.join(timeout=_BLOCKED_PROBE_SECONDS)
        assert teardown.is_alive(), f"teardown ran under the writer's lock: {errors}"
        assert (root / _SERVER_PATTERN).exists()
    finally:
        assert writer.release(CATALOG, owner_name=_WRITER)
    teardown.join(timeout=_JOIN_SECONDS)

    assert not teardown.is_alive() and errors == []
    assert not (root / _SERVER_PATTERN).exists()
    assert snapshot_catalog(root) == snapshot
    assert not store.is_held(CATALOG)


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

    restore_catalog(root, _lock_store(tmp_path), snapshot, head)

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

    restore_catalog(root, _lock_store(tmp_path), snapshot, head)

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

    restore_catalog(root, _lock_store(tmp_path), snapshot, head)

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
            restore_catalog(root, _lock_store(tmp_path), snapshot, head)
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
    store = _lock_store(tmp_path)
    writer = _server_locks(root, store)
    assert writer.acquire(CATALOG, owner_name=_WRITER, heartbeat=False)
    try:
        with pytest.raises(TimeoutError, match=_WRITER):
            restore_catalog(root, store, snapshot, head, lock_wait_seconds=0.0)
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

    restore_catalog(root, _lock_store(tmp_path), snapshot, head)

    assert head is None
    assert snapshot_catalog(root) == snapshot
    assert not (root / ".git").exists()
