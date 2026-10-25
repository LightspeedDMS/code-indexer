"""Catalog memory files are readable by the owner and the service group only
across CoW-daemon copies; every other repository keeps group and other read.
Snapshots still contain the catalog files."""

import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.server.utils.config_manager import ServerResourceConfig

_OTHER_BITS = stat.S_IRWXO
_CP = shutil.which("cp") or "cp"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _build_tree(tmp_path: Path, memory_mode: int, memories_dir_mode: int) -> Path:
    source = tmp_path / "source"
    memories = source / "memories"
    memories.mkdir(parents=True)
    memory = memories / "mem-0001.md"
    memory.write_text("---\nid: mem-0001\n---\nnote body\n")
    os.chmod(memory, memory_mode)
    os.chmod(memories, memories_dir_mode)
    description = source / "example-repo.md"
    description.write_text("description")
    os.chmod(description, 0o600)
    return source


class CowDaemonBackend:
    """Class name matches the production gate; copies like the daemon does."""

    def __init__(self, versioned_base: Path) -> None:
        self._versioned_base = str(versioned_base)

    def create_clone(self, source_path: str, alias: str, version_name: str) -> str:
        dest = Path(self._versioned_base) / ".versioned" / alias / version_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([_CP, "-a", source_path, str(dest)], check=True)
        return str(dest)

    def create_clone_at_path(
        self,
        source_path: str,
        dest_path: str,
        preserve_attrs: bool = True,
        timeout: float = 3600,
        cancel_check: Any = None,
    ) -> str:
        return dest_path


def _snapshot(tmp_path: Path, alias: str, source: Path) -> Path:
    # ^ why cast, not a full CloneBackend; the preflight gate reads only the class name
    backend = cast(Any, CowDaemonBackend(tmp_path / "versioned_base"))
    manager = VersionedSnapshotManager(clone_backend=backend)
    return Path(manager.create_snapshot(alias, str(source)))


def _activate(tmp_path: Path, alias: str, source: Path) -> None:
    golden = MagicMock()
    golden.resource_config = ServerResourceConfig()
    dest = tmp_path / "dest"
    dest.mkdir()
    # ^ why cast, not a full CloneBackend; the preflight gate reads only the class name
    backend = cast(Any, CowDaemonBackend(tmp_path / "unused"))
    manager = ActivatedRepoManager(
        data_dir=str(tmp_path / "data"),
        golden_repo_manager=golden,
        background_job_manager=MagicMock(),
        clone_backend=backend,
        index_manager=None,
    )
    manager._clone_with_copy_on_write(str(source), str(dest), golden_repo_alias=alias)


class TestCatalogMemoriesStayGroupOnly:
    def test_catalog_memory_gains_group_read_and_no_other_bits(
        self, tmp_path: Path
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o600, memories_dir_mode=0o700)

        _snapshot(tmp_path, "cidx-meta", source)

        memory = source / "memories" / "mem-0001.md"
        assert _mode(memory) == 0o640, oct(_mode(memory))
        assert _mode(source / "memories") == 0o750

    def test_catalog_memories_converge_to_group_only(self, tmp_path: Path) -> None:
        source = _build_tree(tmp_path, memory_mode=0o644, memories_dir_mode=0o755)

        _snapshot(tmp_path, "cidx-meta-global", source)

        assert _mode(source / "memories" / "mem-0001.md") == 0o640
        assert _mode(source / "memories") == 0o750

    def test_catalog_snapshot_contains_memory_with_group_only_mode(
        self, tmp_path: Path
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o644, memories_dir_mode=0o755)

        snapshot = _snapshot(tmp_path, "cidx-meta", source)

        copied = snapshot / "memories" / "mem-0001.md"
        original = source / "memories" / "mem-0001.md"
        assert copied.read_bytes() == original.read_bytes()
        assert _mode(copied) & _OTHER_BITS == 0, oct(_mode(copied))

    def test_catalog_activation_keeps_memories_group_only(self, tmp_path: Path) -> None:
        source = _build_tree(tmp_path, memory_mode=0o644, memories_dir_mode=0o755)

        _activate(tmp_path, "cidx-meta", source)

        assert _mode(source / "memories" / "mem-0001.md") == 0o640
        assert _mode(source / "memories") == 0o750

    def test_only_top_level_catalog_memories_are_group_only(
        self, tmp_path: Path
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o600, memories_dir_mode=0o700)
        nested = source / "docs" / "memories"
        nested.mkdir(parents=True)
        nested_file = nested / "notes.md"
        nested_file.write_text("x")
        os.chmod(nested_file, 0o600)

        _snapshot(tmp_path, "cidx-meta", source)

        assert _mode(nested_file) == 0o644


def _add_index_tree_at_default_umask(source: Path) -> Path:
    collection = source / ".code-indexer" / "index" / "voyage-code-3"
    collection.mkdir(parents=True)
    chunks = collection / "chunks.db"
    chunks.write_bytes(b"chunk rows")
    os.chmod(chunks, 0o644)
    for directory in (collection, collection.parent, source / ".code-indexer"):
        os.chmod(directory, 0o755)
    return chunks


def _entries_with_other_bits(root: Path) -> list:
    entries = [root, *root.rglob("*")]
    return [p for p in entries if _mode(p) & _OTHER_BITS]


class TestCatalogIndexTreeStaysGroupOnly:
    def test_cow_snapshot_of_catalog_converges_index_tree_to_group_only(
        self, tmp_path: Path
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o600, memories_dir_mode=0o700)
        chunks = _add_index_tree_at_default_umask(source)

        snapshot = _snapshot(tmp_path, "cidx-meta", source)

        assert _entries_with_other_bits(source / ".code-indexer") == []
        assert _mode(chunks) == 0o640
        copied = snapshot / ".code-indexer" / "index" / "voyage-code-3" / "chunks.db"
        assert copied.read_bytes() == chunks.read_bytes()
        assert _entries_with_other_bits(snapshot / ".code-indexer") == []

    def test_cow_snapshot_of_other_repo_widens_its_index_tree(
        self, tmp_path: Path
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o600, memories_dir_mode=0o700)
        chunks = _add_index_tree_at_default_umask(source)

        _snapshot(tmp_path, "example-repo", source)

        assert _mode(chunks) == 0o644


def _local_snapshot(tmp_path: Path, alias: str, source: Path) -> Path:
    backend = LocalCloneBackend(versioned_base=str(tmp_path / "local_versioned"))
    manager = VersionedSnapshotManager(clone_backend=backend)
    return Path(manager.create_snapshot(alias, str(source)))


class TestLocalCloneSnapshotOfCatalog:
    def test_local_snapshot_of_catalog_converges_private_paths_in_place(
        self, tmp_path: Path
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o644, memories_dir_mode=0o755)
        chunks = _add_index_tree_at_default_umask(source)

        snapshot = _local_snapshot(tmp_path, "cidx-meta", source)

        for root in (source, snapshot):
            assert _entries_with_other_bits(root / "memories") == []
            assert _entries_with_other_bits(root / ".code-indexer") == []
        copied = snapshot / ".code-indexer" / "index" / "voyage-code-3" / "chunks.db"
        assert _mode(chunks) == 0o640
        assert _mode(copied) == 0o640
        assert copied.read_bytes() == chunks.read_bytes()

    def test_local_snapshot_of_other_repo_leaves_modes_unchanged(
        self, tmp_path: Path
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o644, memories_dir_mode=0o755)
        _add_index_tree_at_default_umask(source)
        before = {p: _mode(p) for p in source.rglob("*")}

        _local_snapshot(tmp_path, "example-repo", source)

        assert {p: _mode(p) for p in source.rglob("*")} == before


_SNAPSHOTTERS = {"cow": _snapshot, "local": _local_snapshot}


def _no_snapshot_made(tmp_path: Path) -> bool:
    roots = [tmp_path / "versioned_base", tmp_path / "local_versioned"]
    return not any(list(root.rglob("v_*")) for root in roots if root.exists())


class TestCatalogSnapshotStopsWhenModesCannotBeSet:
    @pytest.mark.parametrize("kind", ["cow", "local"])
    def test_unreadable_private_dir_stops_catalog_snapshot(
        self, tmp_path: Path, kind: str
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o600, memories_dir_mode=0o700)
        _add_index_tree_at_default_umask(source)
        sealed = source / ".code-indexer" / "sealed"
        sealed.mkdir()
        os.chmod(sealed, 0)
        try:
            with pytest.raises(RuntimeError):
                _SNAPSHOTTERS[kind](tmp_path, "cidx-meta", source)
        finally:
            os.chmod(sealed, 0o700)
        assert _no_snapshot_made(tmp_path)

    @pytest.mark.parametrize("kind", ["cow", "local"])
    def test_unstartable_find_stops_catalog_snapshot(
        self, tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o600, memories_dir_mode=0o700)
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))

        with pytest.raises(RuntimeError):
            _SNAPSHOTTERS[kind](tmp_path, "cidx-meta", source)
        assert _no_snapshot_made(tmp_path)

    def test_unstartable_find_does_not_stop_other_repo_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o600, memories_dir_mode=0o700)
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))

        snapshot = _snapshot(tmp_path, "example-repo", source)

        assert (snapshot / "example-repo.md").is_file()


def _add_symlinks_to_private_tree(tmp_path: Path, source: Path, link: str) -> Path:
    outside = tmp_path / "outside-target.md"
    outside.write_text("outside the catalog")
    os.chmod(outside, 0o644)
    target = outside if link == "outside" else tmp_path / "no-such-target"
    os.symlink(target, source / "memories" / "linked.md")
    os.symlink(target, source / ".code-indexer" / "linked-index")
    return outside


class TestSymlinksInCatalogPrivateTree:
    @pytest.mark.parametrize("link", ["outside", "dangling"])
    @pytest.mark.parametrize("kind", ["cow", "local"])
    def test_catalog_snapshot_skips_symlinks_and_restricts_real_entries(
        self, tmp_path: Path, kind: str, link: str
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o644, memories_dir_mode=0o755)
        chunks = _add_index_tree_at_default_umask(source)
        outside = _add_symlinks_to_private_tree(tmp_path, source, link)

        snapshot = _SNAPSHOTTERS[kind](tmp_path, "cidx-meta", source)

        assert _mode(outside) == 0o644
        assert (snapshot / "memories" / "linked.md").is_symlink()
        assert _mode(source / "memories" / "mem-0001.md") & _OTHER_BITS == 0
        assert _mode(source / "memories") & _OTHER_BITS == 0
        assert _mode(chunks) & _OTHER_BITS == 0
        assert _mode(source / ".code-indexer") & _OTHER_BITS == 0


class TestSymlinkedCatalogPrivateRoot:
    @pytest.mark.parametrize("kind", ["cow", "local"])
    def test_symlinked_private_root_is_reported_not_followed(
        self, tmp_path: Path, kind: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o644, memories_dir_mode=0o755)
        chunks = _add_index_tree_at_default_umask(source)
        outside = tmp_path / "outside-memories"
        shutil.move(str(source / "memories"), str(outside))
        os.symlink(outside, source / "memories")

        with caplog.at_level(logging.ERROR):
            _SNAPSHOTTERS[kind](tmp_path, "cidx-meta", source)

        assert _mode(outside) == 0o755
        assert _mode(outside / "mem-0001.md") == 0o644
        assert _mode(chunks) & _OTHER_BITS == 0
        errors = [
            r.getMessage()
            for r in caplog.records
            if r.levelname == "ERROR" and "symbolic link" in r.getMessage()
        ]
        assert len(errors) == 1, errors
        assert "'memories'" in errors[0] and "not managed" in errors[0]


class TestNonCatalogRepositoriesKeepWidening:
    def test_snapshot_of_other_repo_widens_its_memories_dir(
        self, tmp_path: Path
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o600, memories_dir_mode=0o700)

        _snapshot(tmp_path, "example-repo", source)

        assert _mode(source / "memories" / "mem-0001.md") == 0o644
        assert _mode(source / "memories") == 0o755
        assert _mode(source / "example-repo.md") == 0o644

    def test_activation_of_other_repo_widens_its_memories_dir(
        self, tmp_path: Path
    ) -> None:
        source = _build_tree(tmp_path, memory_mode=0o600, memories_dir_mode=0o700)

        _activate(tmp_path, "example-repo", source)

        assert _mode(source / "memories" / "mem-0001.md") == 0o644
        assert _mode(source / "memories") == 0o755
