"""The property the content seam actually has to hold, over generated repositories.

Four MAJOR findings on this seam were all the same shape: ``manifest`` and
``fetch`` disagreeing about what a path means. Lexical confinement, a
resolved-versus-lexical comparison, a traversing cursor, and a cursor resuming
THROUGH a symlink. Each fix was correct and each left a different door open,
which is the signature of testing doors instead of the invariant.

So this states the invariant once, and generates repository shapes to attack it:

  P1  paging equals a single pass, exactly
  P2  every non-symlink entry manifest lists, fetch returns, byte-identical
  P3  every symlink entry manifest lists, fetch refuses
  P4  nothing manifest lists resolves outside the repository
  P5  manifest(cursor=ANYTHING) is a subset of a fresh walk -- resuming can never
      reach what a walk cannot
  P6  a hostile path is refused by fetch AND never listed by manifest

Seeded rather than random: a failure prints its seed and reproduces exactly.
"""

from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import pytest

from code_indexer.server.storage.shared.content_backend import (
    ContentEntry,
    LocalContentBackend,
)
from code_indexer.utils.blob_hash import git_blob_hash_bytes

SEEDS = list(range(24))

#: What a caller may hand the backend as the repository path.
RepoArg = Union[str, Path]

#: HOW the repository is addressed, not just what is in it. A bug that compared a
#: resolved path against a lexical one refused every file in the repository -- but
#: only when repo_path was relative or had a symlinked ancestor, so a suite that
#: always passed a canonical absolute path could not see it. Golden repos are
#: reached through alias pointers and .versioned links, and an NFS mount point is
#: often a symlink, so none of these spellings is exotic.
ADDRESSING = ["absolute", "relative", "symlinked-ancestor"]

#: Planted outside every generated repo, so an escape is detectable by content
#: rather than only by path shape.
SECRET = b"OUTSIDE-THE-REPOSITORY\n"

_NAMES = [
    "a",
    "b",
    "mod",
    "sub",
    "sub.txt",
    "pkg",
    "zz",
    "x.py",
    "café",
    "with space",
    "dot.ted",
]


def _plant(root: Path, outside: Path, rng: random.Random, depth: int = 0) -> None:
    """Fill a directory with a random mix of the shapes that have broken this seam."""
    for _ in range(rng.randint(2, 6)):
        kind = rng.choice(
            [
                "file",
                "file",
                "file",
                "dir",
                "link_in_file",
                "link_out_file",
                "link_in_dir",
                "link_out_dir",
                "dangling",
                "gitdir",
                "empty_dir",
            ]
        )
        name = rng.choice(_NAMES) + rng.choice(["", ".py", ".md", ""])
        target = root / name
        if target.exists() or target.is_symlink():
            continue
        try:
            if kind == "file":
                target.write_bytes(
                    rng.choice(
                        [b"", b"x\n", b"a\r\nb\r\n", bytes([0, 1, 2, 255]), b"q" * 2048]
                    )
                )
            elif kind == "dir" and depth < 3:
                target.mkdir()
                _plant(target, outside, rng, depth + 1)
            elif kind == "empty_dir":
                target.mkdir()
            elif kind == "gitdir":
                (target / ".git").mkdir(parents=True, exist_ok=True)
                (target / ".git" / "config").write_bytes(b"[core]\n")
            elif kind == "link_in_file":
                sibling = root / (name + "-real")
                sibling.write_bytes(b"inside\n")
                os.symlink(sibling, target)
            elif kind == "link_out_file":
                os.symlink(outside / "secret.txt", target)
            elif kind == "link_in_dir" and depth < 3:
                sibling = root / (name + "-realdir")
                sibling.mkdir(exist_ok=True)
                (sibling / "inner.py").write_bytes(b"inner\n")
                os.symlink(sibling, target, target_is_directory=True)
            elif kind == "link_out_dir":
                os.symlink(outside, target, target_is_directory=True)
            elif kind == "dangling":
                os.symlink(root / "does-not-exist", target)
        except (OSError, FileExistsError):
            continue


def _make_repo(tmp_path: Path, seed: int) -> Tuple[Path, Path]:
    rng = random.Random(seed)
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_bytes(SECRET)
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    _plant(root, outside, rng)
    return root, outside


def _full(backend: LocalContentBackend, root: RepoArg) -> List[ContentEntry]:
    out: List[ContentEntry] = []
    cursor: Optional[str] = None
    for _ in range(500):
        page = backend.manifest(root, cursor=cursor, page_size=1000)
        out.extend(page.entries)
        if page.next_cursor is None:
            return out
        cursor = page.next_cursor
    raise AssertionError("manifest did not terminate")


def _paged(
    backend: LocalContentBackend, root: RepoArg, size: int
) -> List[ContentEntry]:
    out: List[ContentEntry] = []
    cursor: Optional[str] = None
    for _ in range(2000):
        page = backend.manifest(root, cursor=cursor, page_size=size)
        out.extend(page.entries)
        if page.next_cursor is None:
            return out
        cursor = page.next_cursor
    raise AssertionError("paging did not terminate")


@pytest.mark.parametrize("addressing", ADDRESSING)
@pytest.mark.parametrize("seed", SEEDS)
def test_seam_invariants_hold_for_generated_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int, addressing: str
) -> None:
    root, outside = _make_repo(tmp_path, seed)
    backend = LocalContentBackend()
    root_real = os.path.realpath(root)

    # `repo` is what the caller hands the backend; `root` stays the real path used
    # to read files off disk for comparison.
    if addressing == "absolute":
        repo: RepoArg = root
    elif addressing == "relative":
        monkeypatch.chdir(tmp_path)
        repo = "repo"
    else:
        alias = tmp_path / "alias"
        os.symlink(root, alias, target_is_directory=True)
        repo = alias

    listed = _full(backend, repo)
    paths = [e.path for e in listed]
    by_path: Dict[str, ContentEntry] = {e.path: e for e in listed}

    # P1 -- paging equals a single pass, for several page sizes.
    for size in (1, 2, 3, 7):
        assert [e.path for e in _paged(backend, repo, size)] == paths, (
            f"seed={seed}/{addressing} page_size={size}"
        )
    assert paths == sorted(paths), f"seed={seed}/{addressing}: order is not monotonic"
    assert len(paths) == len(set(paths)), f"seed={seed}/{addressing}: duplicate entry"
    assert not any(p == ".git" or p.startswith(".git/") for p in paths), (
        f"seed={seed}/{addressing}: .git listed"
    )

    # P4 -- nothing listed resolves outside the repository.
    for entry in listed:
        if entry.is_symlink:
            continue  # reported, never followed; its target may be anywhere
        real = os.path.realpath(root / entry.path)
        assert real == root_real or real.startswith(root_real + os.sep), (
            f"seed={seed}/{addressing}: {entry.path} escaped"
        )

    # P2/P3 -- the two halves agree about every single listed path.
    fetched = {f.path: f for f in backend.fetch(repo, paths)}
    assert set(fetched) == set(paths), (
        f"seed={seed}/{addressing}: fetch dropped or invented paths"
    )
    for entry in listed:
        got = fetched[entry.path]
        if entry.is_symlink:
            assert got.error is not None, (
                f"seed={seed}/{addressing}: symlink {entry.path} was fetched"
            )
            assert got.data == b"", (
                f"seed={seed}/{addressing}: symlink {entry.path} returned bytes"
            )
        else:
            assert got.error is None, (
                f"seed={seed}/{addressing}: listed {entry.path} refused as {got.error!r}"
            )
            assert got.data == (root / entry.path).read_bytes(), (
                f"seed={seed}/{addressing}: {entry.path} bytes differ"
            )
            assert got.blob_sha == git_blob_hash_bytes(got.data), (
                f"seed={seed}/{addressing}: {entry.path} hash"
            )
            assert entry.blob_sha == got.blob_sha, (
                f"seed={seed}/{addressing}: {entry.path} manifest/fetch hash differ"
            )

    # P5 -- resuming can never reach what a fresh walk cannot.
    hostile_cursors: List[str] = [
        "../",
        "../outside/secret.txt",
        "./x",
        "/etc/passwd",
        "",
        "does/not/exist",
        "zzzz",
    ]
    hostile_cursors += paths
    hostile_cursors += [p + "/a" for p in paths]  # resume THROUGH an entry
    hostile_cursors += [p + "/../../outside/secret.txt" for p in paths[:4]]
    for cursor in hostile_cursors:
        try:
            page = backend.manifest(repo, cursor=cursor, page_size=1000)
        except ValueError:
            continue  # refusing is always a valid answer
        for entry in page.entries:
            assert entry.path in by_path, (
                f"seed={seed}/{addressing}: cursor {cursor!r} produced {entry.path!r}, which a fresh walk never lists"
            )

    # P6 -- a hostile path is refused by fetch, and was never listed.
    # Paths that go THROUGH a listed directory symlink. Without these, every
    # generated fetch input is either something manifest listed (which by
    # construction never goes beneath a link) or something refused LEXICALLY, so
    # _confine's resolved-parent comparison is never reached. Removing only that
    # branch left this suite entirely green -- the example suite caught it, which
    # is the "testing doors" approach this file is meant to replace.
    through_links: List[str] = []
    for entry in listed:
        if not entry.is_symlink:
            continue
        target = root / entry.path
        if not target.is_dir():  # follows the link on purpose: is it a DIRECTORY link?
            continue
        try:
            names = sorted(
                child.name for child in os.scandir(os.path.realpath(target))
            )[:2]
        except OSError:
            names = []
        through_links.extend(f"{entry.path}/{name}" for name in (names or ["a"]))

    for bad in through_links:
        assert bad not in by_path, (
            f"seed={seed}/{addressing}: manifest listed through a link: {bad!r}"
        )
        (got,) = list(backend.fetch(repo, [bad]))
        assert got.error is not None, (
            f"seed={seed}/{addressing}: fetch followed a directory symlink for {bad!r}"
        )
        assert got.data == b"", (
            f"seed={seed}/{addressing}: bytes returned through a link for {bad!r}"
        )

    hostile_paths = [
        str(outside / "secret.txt"),
        "../outside/secret.txt",
        "./x",
        "/etc/passwd",
        "a/../../outside/secret.txt",
    ] + [p + "/../../outside/secret.txt" for p in paths[:4]]
    for bad in hostile_paths:
        assert bad not in by_path, (
            f"seed={seed}/{addressing}: manifest listed hostile path {bad!r}"
        )
        (got,) = list(backend.fetch(repo, [bad]))
        assert got.error is not None, (
            f"seed={seed}/{addressing}: fetch accepted hostile path {bad!r}"
        )
        assert got.data == b"", (
            f"seed={seed}/{addressing}: fetch returned bytes for {bad!r}"
        )


def test_the_generator_actually_produces_the_shapes_that_broke_this_seam(
    tmp_path: Path,
) -> None:
    """A generator that happened to emit only plain files would make the suite vacuous."""
    kinds = {"symlink": 0, "nested": 0, "outward_link": 0, "dir_link": 0, "entries": 0}
    for seed in SEEDS:
        root, _ = _make_repo(tmp_path / f"s{seed}", seed)
        for entry in _full(LocalContentBackend(), root):
            kinds["entries"] += 1
            if entry.is_symlink:
                kinds["symlink"] += 1
                if (root / entry.path).is_dir():
                    kinds["dir_link"] += 1
                if not os.path.realpath(root / entry.path).startswith(
                    os.path.realpath(root) + os.sep
                ):
                    kinds["outward_link"] += 1
            if "/" in entry.path:
                kinds["nested"] += 1
    assert kinds["entries"] > 100, kinds
    assert kinds["symlink"] > 10, kinds
    assert kinds["outward_link"] > 0, kinds
    assert kinds["nested"] > 10, kinds
    # Without directory links, P6's through-a-link paths would all be vacuous.
    assert kinds["dir_link"] > 0, kinds
