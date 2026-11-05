"""Conformance suite for the index store seam (``cidx_sdk.seams.index_store``).

Subclass :class:`IndexStoreContract` and provide ``index_store`` (the store
under test). The same checks hold for a store that publishes nothing (local:
the built index is the readable one) and one that publishes immutable
versions (delegated: the core's ``DelegatedIndexStore`` over an artifact
client); where they differ the check branches on ``is_local``, which is part
of the contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cidx_sdk.seams.index_store import INDEX_DIRNAME, IndexStore, index_root_for


def _build(store: IndexStore, repo: Path, data: bytes) -> Path:
    root = store.index_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.bin").write_bytes(data)
    return root


class IndexStoreContract:
    """The index store's contract. Subclass and provide ``index_store``."""

    @pytest.fixture
    def index_store(self) -> IndexStore:
        raise NotImplementedError("provide the store under test")

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        path = tmp_path / "example-repo"
        path.mkdir()
        return path

    def test_is_local_is_a_bool(self, index_store: IndexStore) -> None:
        assert isinstance(index_store.is_local, bool)

    def test_the_index_is_built_inside_the_repository(
        self, index_store: IndexStore, repo: Path
    ) -> None:
        assert index_store.index_root(repo) == repo / INDEX_DIRNAME
        assert index_store.index_root(str(repo)) == index_root_for(repo)

    def test_a_published_build_reads_back(
        self, index_store: IndexStore, repo: Path, tmp_path: Path
    ) -> None:
        built = _build(index_store, repo, b"v1")
        version = index_store.publish(repo, built)
        if index_store.is_local:
            assert version is None
        else:
            assert isinstance(version, str) and version
        readable = index_store.checkout(repo, tmp_path / "reader", version)
        assert (readable / "index.bin").read_bytes() == b"v1"

    def test_a_version_is_immutable(
        self, index_store: IndexStore, repo: Path, tmp_path: Path
    ) -> None:
        if index_store.is_local:
            pytest.skip("a local store publishes no versions")
        first = index_store.publish(repo, _build(index_store, repo, b"v1"))
        second = index_store.publish(repo, _build(index_store, repo, b"v2"))
        assert first != second
        old = index_store.checkout(repo, tmp_path / "old", first)
        new = index_store.checkout(repo, tmp_path / "new")
        assert (old / "index.bin").read_bytes() == b"v1"
        assert (new / "index.bin").read_bytes() == b"v2"

    def test_delete_is_idempotent_and_honest(
        self, index_store: IndexStore, repo: Path, tmp_path: Path
    ) -> None:
        index_store.publish(repo, _build(index_store, repo, b"v1"))
        deleted = index_store.delete(repo)
        if index_store.is_local:
            assert deleted is None
            return
        assert deleted == 1
        assert index_store.delete(repo) == 0
        with pytest.raises(Exception):
            index_store.checkout(repo, tmp_path / "gone")
