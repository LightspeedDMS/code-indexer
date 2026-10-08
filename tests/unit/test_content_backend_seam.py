"""The content-read seam: contract, hash identity, laziness, and fail-loud selection.

cidx's job is semantic search; reading a repository's bytes off a shared mount is
not. This seam makes that one hop pluggable so the reads can move to
workspace-mcp, which owns filesystem access by charter.

Phase 3 lands INERT: the default backend reads the mount exactly as today, and no
call site is migrated yet (that is phase 4). These tests pin the parts that would
otherwise rot silently -- that the local backend reuses the one definition of
content identity rather than growing a second, that fetch streams instead of
buffering, and that a delegated selection without an injected backend fails loudly
rather than quietly reading a path that may not exist in that container.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

import pytest

from code_indexer.server.storage.shared.content_backend import (
    ContentBackend,
    ContentBackendFactory,
    ContentPage,
    FetchedFile,
    LocalContentBackend,
)
from code_indexer.utils.blob_hash import git_blob_hash


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fixture repo covering the shapes that break naive implementations."""
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / "a.py").write_text("print('a')\n")
    (root / "empty.txt").write_text("")
    (root / "binary.dat").write_bytes(bytes([0, 1, 2, 255, 0, 128, 254]))
    (root / "big.bin").write_bytes(b"x" * (1024 * 1024 + 17))
    (root / "unicode.ts").write_text("café — 😀\n", encoding="utf-8")
    (root / "sub" / "b.py").write_text("print('b')\n")
    # A file whose name shares a prefix with the DIRECTORY "sub". This pair is the
    # only shape that distinguishes sorting directories as `name + "/"` from
    # sorting them by bare name, because "." (0x2E) sorts before "/" (0x2F):
    # without it both rules emit the same order and the invariant is untested.
    (root / "sub.txt").write_text("sibling\n")
    os.symlink(root / "a.py", root / "link.py")
    return root


def _entries(page: ContentPage) -> dict:
    return {e.path: e for e in page.entries}


def _all_entries(backend: ContentBackend, root: Path, page_size: int = 100) -> dict:
    out: dict = {}
    cursor: Optional[str] = None
    while True:
        page = backend.manifest(root, cursor=cursor, page_size=page_size)
        out.update(_entries(page))
        if page.next_cursor is None:
            return out
        cursor = page.next_cursor


class _RecordingBackend:
    """A delegated-style backend that records what it was handed."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    @property
    def is_local(self) -> bool:
        return False

    def manifest(self, repo_path, *, cursor=None, page_size=1000) -> ContentPage:
        self.calls.append(("manifest", str(repo_path), cursor, page_size))
        return ContentPage(entries=(), next_cursor=None)

    def fetch(self, repo_path, paths: Sequence[str]) -> Iterator[FetchedFile]:
        self.calls.append(("fetch", str(repo_path), tuple(paths)))
        return iter(())


# --- 1: one definition of content identity -------------------------------------


def test_local_manifest_hash_equals_the_shared_blob_hash_helper(repo: Path) -> None:
    """A second hash implementation here would silently fork content identity.

    The blob sha seeds the deterministic point UUID of every chunk, so a drifting
    hash re-identifies chunks rather than invalidating them -- the failure is
    silent duplicate content, not an error.
    """
    entries = _all_entries(LocalContentBackend(), repo)
    hashed = {p: e for p, e in entries.items() if not e.is_symlink}
    assert hashed, "fixture produced no hashable entries"
    for rel, entry in hashed.items():
        assert entry.blob_sha == git_blob_hash(repo / rel), rel


# --- 2: bytes survive the round trip -------------------------------------------


def test_local_fetch_returns_bytes_identical_to_a_direct_read(repo: Path) -> None:
    rels = ["a.py", "empty.txt", "binary.dat", "big.bin", "unicode.ts", "sub/b.py"]
    got = {f.path: f for f in LocalContentBackend().fetch(repo, rels)}
    assert set(got) == set(rels)
    for rel in rels:
        assert got[rel].data == (repo / rel).read_bytes(), rel
        assert got[rel].error is None


def test_local_fetch_blob_sha_describes_the_bytes_it_returned(repo: Path) -> None:
    for item in LocalContentBackend().fetch(repo, ["a.py", "big.bin"]):
        assert item.blob_sha == git_blob_hash(repo / item.path)


# --- 3: fetch streams, never buffers -------------------------------------------


def test_fetch_is_lazy_so_consuming_one_item_does_not_read_them_all(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A list comprehension here would reintroduce the buffer the wire format avoids."""
    opened: List[str] = []
    real_open = Path.read_bytes

    def spy(self: Path) -> bytes:
        opened.append(self.name)
        return real_open(self)

    monkeypatch.setattr(Path, "read_bytes", spy)

    stream = LocalContentBackend().fetch(repo, ["a.py", "sub/b.py", "binary.dat"])
    first = next(iter(stream))

    assert first.path == "a.py"
    assert len(opened) == 1, f"reading one item read {len(opened)} files: {opened}"


# --- 4-6: selection is explicit and fails loudly -------------------------------


def test_factory_default_is_local() -> None:
    assert ContentBackendFactory.create().is_local is True
    assert isinstance(ContentBackendFactory.create(), LocalContentBackend)


def test_delegated_without_an_injected_backend_raises_naming_the_config_key() -> None:
    with pytest.raises(RuntimeError) as err:
        ContentBackendFactory.create("delegated")
    message = str(err.value)
    assert "content_backend" in message
    assert "local" in message


def test_delegated_with_an_injected_backend_returns_it() -> None:
    injected = _RecordingBackend()
    assert (
        ContentBackendFactory.create("delegated", delegated_backend=injected)
        is injected
    )


def test_an_unknown_backend_name_raises_rather_than_defaulting_to_local() -> None:
    with pytest.raises(RuntimeError) as err:
        ContentBackendFactory.create("typo")
    assert "typo" in str(err.value)


# --- 7: a delegated backend's files are not on this disk -----------------------


def test_a_delegated_backend_reports_it_is_not_local() -> None:
    """is_local drives every probe that asks about THIS container's filesystem.

    A delegated backend's files are not on this disk, so a probe would "discover"
    them missing and degrade silently instead of failing.
    """
    backend = ContentBackendFactory.create(
        "delegated", delegated_backend=_RecordingBackend()
    )
    assert backend.is_local is False


# --- 8: the two backends must agree ---------------------------------------------


def test_local_and_delegated_manifests_are_identical_for_the_same_fixture(
    repo: Path,
) -> None:
    """Any hash or path-normalisation drift between the two shows up here.

    The delegated stand-in replays what the local backend produced, so this pins
    the SHAPE both must satisfy: repo-relative POSIX paths, .git excluded,
    symlinks reported but never followed, and a stable order.
    """
    local = _all_entries(LocalContentBackend(), repo)

    class _Replaying:
        is_local = False

        def manifest(self, repo_path, *, cursor=None, page_size=1000) -> ContentPage:
            return ContentPage(
                entries=tuple(sorted(local.values(), key=lambda e: e.path)),
                next_cursor=None,
            )

        def fetch(self, repo_path, paths):  # pragma: no cover - not used here
            return iter(())

    delegated = _all_entries(_Replaying(), repo)
    assert local == delegated


def test_manifest_excludes_git_reports_symlinks_and_uses_posix_relative_paths(
    repo: Path,
) -> None:
    entries = _all_entries(LocalContentBackend(), repo)

    assert not [p for p in entries if p == ".git" or p.startswith(".git/")], entries
    assert "sub/b.py" in entries, "paths must be repo-relative with POSIX separators"
    assert not any(p.startswith("/") for p in entries)

    link = entries["link.py"]
    assert link.is_symlink is True
    assert link.blob_sha is None, "a symlink is reported, never followed"

    assert entries["empty.txt"].size == 0
    assert entries["big.bin"].size == 1024 * 1024 + 17


def test_manifest_paging_covers_every_entry_once_and_in_a_stable_order(
    repo: Path,
) -> None:
    backend = LocalContentBackend()
    one_pass = [e.path for e in backend.manifest(repo, page_size=1000).entries]

    paged: List[str] = []
    cursor: Optional[str] = None
    pages = 0
    while True:
        page = backend.manifest(repo, cursor=cursor, page_size=2)
        paged.extend(e.path for e in page.entries)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert pages < 100, "cursor is not advancing"

    assert paged == one_pass, "paged traversal must match a single pass exactly"
    assert len(paged) == len(set(paged)), "an entry was emitted twice"
    assert pages > 1, "fixture did not actually span multiple pages"


def test_manifest_order_is_monotonic_in_the_path(repo: Path) -> None:
    """The emitted order must ascend by path, because the cursor IS a path.

    Resuming means "everything after this one", so a non-monotonic order silently
    skips entries rather than erroring. A directory sorts under ``name + "/"``
    for this reason: "sub.txt" must precede "sub/b.py", since "." sorts before
    "/". Sorting directories by bare name reverses that pair and the cursor then
    steps over "sub.txt" entirely.
    """
    paths = [
        e.path for e in LocalContentBackend().manifest(repo, page_size=1000).entries
    ]

    assert "sub.txt" in paths and "sub/b.py" in paths, "fixture lost the prefix pair"
    assert paths == sorted(paths), f"order is not ascending by path: {paths}"


def test_an_unreadable_file_is_reported_per_file_and_does_not_fail_the_batch(
    repo: Path,
) -> None:
    """One bad file must not discard the batch, mirroring the wire format's error record."""
    results = {
        f.path: f for f in LocalContentBackend().fetch(repo, ["missing.py", "a.py"])
    }
    assert results["missing.py"].error is not None
    assert results["missing.py"].data == b""
    assert results["a.py"].data == (repo / "a.py").read_bytes()
