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
from typing import Iterator, List, Optional, Sequence, Union

import pytest

from code_indexer.server.storage.shared.content_backend import (
    ContentBackend,
    ContentBackendFactory,
    ContentPage,
    FetchedFile,
    LocalContentBackend,
)
from code_indexer.utils.blob_hash import git_blob_hash, git_blob_hash_bytes


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
    # A symlinked DIRECTORY, so "dirlink/escaped.txt" is lexically inside the repo
    # yet resolves outside it -- the case plain prefix checks miss.
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "escaped.txt").write_text("SECRET\n")
    os.symlink(outside_dir, root / "dirlink", target_is_directory=True)
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


# --- 8: the contract every backend must satisfy ---------------------------------
#
# An earlier version of this section compared the local backend's manifest to a
# stand-in that replayed those same entries, so it compared local output to
# itself and could not fail. What follows asserts the SHAPE instead, through a
# fixture parametrised over backends: phase 4 adds its delegated backend to
# ``_CONTRACT_BACKENDS`` and the whole suite runs against it unchanged, which is
# what makes "both backends agree" a tested claim rather than a stated one.

_CONTRACT_BACKENDS = {
    "local": LocalContentBackend,
}


@pytest.fixture(params=sorted(_CONTRACT_BACKENDS))
def contract_backend(request: pytest.FixtureRequest) -> ContentBackend:
    return _CONTRACT_BACKENDS[request.param]()


def test_contract_paths_are_repo_relative_posix(
    contract_backend: ContentBackend, repo: Path
) -> None:
    entries = _all_entries(contract_backend, repo)
    assert entries
    for rel in entries:
        assert not rel.startswith("/"), rel
        assert "\\" not in rel, rel
        assert ".." not in rel.split("/"), rel
    assert "sub/b.py" in entries


def test_contract_git_directory_is_never_enumerated(
    contract_backend: ContentBackend, repo: Path
) -> None:
    entries = _all_entries(contract_backend, repo)
    assert not [p for p in entries if p == ".git" or p.startswith(".git/")]


def test_contract_symlinks_are_reported_but_never_hashed(
    contract_backend: ContentBackend, repo: Path
) -> None:
    link = _all_entries(contract_backend, repo)["link.py"]
    assert link.is_symlink is True
    assert link.blob_sha is None


def test_contract_order_is_monotonic_in_the_path(
    contract_backend: ContentBackend, repo: Path
) -> None:
    paths = [e.path for e in contract_backend.manifest(repo, page_size=1000).entries]
    assert paths == sorted(paths)


def test_contract_paging_matches_a_single_pass(
    contract_backend: ContentBackend, repo: Path
) -> None:
    one_pass = [e.path for e in contract_backend.manifest(repo, page_size=1000).entries]
    paged: List[str] = []
    cursor: Optional[str] = None
    for _ in range(200):
        page = contract_backend.manifest(repo, cursor=cursor, page_size=2)
        paged.extend(e.path for e in page.entries)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert paged == one_pass


def test_contract_blob_sha_describes_the_bytes_returned(
    contract_backend: ContentBackend, repo: Path
) -> None:
    rels = ["a.py", "binary.dat", "big.bin", "empty.txt"]
    for item in contract_backend.fetch(repo, rels):
        assert item.error is None, item.path
        assert item.blob_sha == git_blob_hash_bytes(item.data), item.path


def test_contract_an_unfetchable_path_is_a_record_not_an_exception(
    contract_backend: ContentBackend, repo: Path
) -> None:
    results = {f.path: f for f in contract_backend.fetch(repo, ["missing.py", "a.py"])}
    assert results["missing.py"].error is not None
    assert results["missing.py"].data == b""
    assert results["a.py"].error is None


def test_contract_fetch_refuses_to_follow_a_symlink(
    contract_backend: ContentBackend, repo: Path
) -> None:
    """manifest says a symlink is never followed; fetch must say the same.

    Otherwise the two halves of the protocol disagree about the identity of the
    same path, and phase 4 feeds manifest paths straight into fetch.
    """
    (item,) = list(contract_backend.fetch(repo, ["link.py"]))
    assert item.error is not None
    assert item.data == b""
    assert item.blob_sha is None


@pytest.mark.parametrize(
    "escape,because",
    [
        ("<absolute>", "absolute"),
        ("../outside.txt", "parent-relative"),
        ("sub/../../outside.txt", "parent-relative"),
        ("dirlink/escaped.txt", "symlinked directory"),
    ],
)
def test_contract_fetch_refuses_paths_that_leave_the_repository(
    contract_backend: ContentBackend,
    repo: Path,
    tmp_path: Path,
    escape: str,
    because: str,
) -> None:
    """`Path("/repo") / "/etc/passwd"` is `/etc/passwd`: joining discards the root.

    The reason is asserted, not just the refusal. Each guard is defence in depth --
    remove any one and the final containment check still refuses these paths -- so
    without pinning WHICH guard fired, deleting one would pass unnoticed.
    """
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET\n")
    rel = str(outside) if escape == "<absolute>" else escape

    (item,) = list(contract_backend.fetch(repo, [rel]))
    assert item.error is not None, f"{rel} was not refused"
    assert because in item.error, f"{rel} refused for the wrong reason: {item.error}"
    assert item.data == b""
    assert item.blob_sha is None


def test_contract_blob_sha_holds_even_if_the_file_changes_after_the_read(
    contract_backend: ContentBackend, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """blob_sha must describe the bytes IN THIS RECORD, not whatever is on disk now.

    Reading the file and then opening it again to hash it is not merely wasteful:
    the content can change in between, and the record would carry a hash of bytes
    the caller never saw. Since blob_sha seeds each chunk's point UUID, that is a
    silent mis-identification rather than an error.

    A static fixture cannot show this, which is why the file is changed underneath
    the read on purpose.
    """
    target = repo / "a.py"
    original = target.read_bytes()
    real_read = Path.read_bytes

    def read_then_change(self: Path) -> bytes:
        data = real_read(self)
        if self.name == "a.py":
            target.write_bytes(b"DIFFERENT CONTENT ENTIRELY\n")
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_change)

    (item,) = list(contract_backend.fetch(repo, ["a.py"]))
    assert item.data == original
    assert item.blob_sha == git_blob_hash_bytes(original)


@pytest.mark.parametrize("shape", ["symlinked-ancestor", "relative"])
def test_fetch_works_when_the_repo_path_is_not_already_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """manifest and fetch must agree about a repo reached through a symlink.

    `os.scandir` follows a symlinked root quite happily, so manifest lists the
    files. An earlier confinement check compared a fully resolved parent against a
    merely lexically normalised one, so it only held for an already-canonical
    absolute root -- and refused EVERY file otherwise, silently, as per-file error
    records.

    Both shapes here are ordinary for this fleet: golden repos are reached through
    alias pointers and .versioned snapshot links, and an NFS mount point is often
    a symlink itself.
    """
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (real / "a.py").write_text("x\n")
    (real / "sub" / "b.py").write_text("y\n")

    if shape == "symlinked-ancestor":
        repo_arg: Union[str, Path] = tmp_path / "alias"
        os.symlink(real, repo_arg, target_is_directory=True)
    else:
        monkeypatch.chdir(tmp_path)
        repo_arg = "real"

    backend = LocalContentBackend()
    listed = [e.path for e in backend.manifest(repo_arg, page_size=100).entries]
    assert listed == ["a.py", "sub/b.py"]

    fetched = {f.path: f for f in backend.fetch(repo_arg, listed)}
    for rel in listed:
        assert fetched[rel].error is None, (
            f"{shape}: {rel} refused with {fetched[rel].error}"
        )
        assert fetched[rel].data == (real / rel).read_bytes()


def test_a_symlinked_directory_is_still_refused_through_a_symlinked_root(
    tmp_path: Path,
) -> None:
    """Making the non-canonical cases work must not loosen the escape guard."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "keep.py").write_text("ok\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET\n")
    os.symlink(outside, real / "dirlink", target_is_directory=True)
    alias = tmp_path / "alias"
    os.symlink(real, alias, target_is_directory=True)

    results = {
        f.path: f
        for f in LocalContentBackend().fetch(alias, ["keep.py", "dirlink/secret.txt"])
    }
    assert results["keep.py"].error is None
    assert "symlinked directory" in (results["dirlink/secret.txt"].error or "")
    assert results["dirlink/secret.txt"].data == b""


def test_paging_cost_tracks_depth_not_tree_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resuming must not replay the walk from the root on every page.

    The first implementation restarted at the root each page and discarded
    everything up to the cursor, so exhausting a repository cost O(pages x tree).
    On this fixture that was 525 scandir calls where resuming needs 102, and on a
    real repo -- 60 pages over thousands of directories -- it is hundreds of
    thousands of NFS round trips.

    Counted, not timed: the call count is deterministic on any machine, where
    wall clock would be dominated by the page cache and would not fail on a fast
    disk even if the behaviour came back.
    """
    root = tmp_path / "repo"
    packages, modules, per_module = 8, 6, 8
    directories = 1
    for a in range(packages):
        directories += 1
        for b in range(modules):
            directory = root / f"pkg{a:02d}" / f"mod{b:02d}"
            directory.mkdir(parents=True)
            directories += 1
            for f in range(per_module):
                (directory / f"f{f}.py").write_text("x")
    files = packages * modules * per_module

    real_scandir = os.scandir
    calls = {"n": 0}

    def counting(
        path: Union[str, "os.PathLike[str]"] = ".",
    ) -> Iterator["os.DirEntry[str]"]:
        calls["n"] += 1
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting)

    backend = LocalContentBackend()
    page_size = 25
    cursor: Optional[str] = None
    pages, seen = 0, 0
    while True:
        page = backend.manifest(root, cursor=cursor, page_size=page_size)
        seen += len(page.entries)
        pages += 1
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert seen == files
    assert pages > 10, "fixture must span enough pages for the difference to show"

    # Resuming reads one directory per cursor component, plus each directory once
    # as the walk reaches it: pages * depth + directories. Re-walking would cost
    # pages * directories, which is an order of magnitude more here.
    depth = 3
    resume_budget = pages * depth + directories
    rewalk_cost = pages * directories
    assert calls["n"] <= resume_budget, (
        f"{calls['n']} scandir calls exceeds the resume budget {resume_budget}"
    )
    assert calls["n"] < rewalk_cost // 2, (
        f"{calls['n']} is close to the re-walk cost {rewalk_cost}"
    )


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
