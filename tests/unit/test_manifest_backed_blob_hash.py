"""The blob hash may come from the manifest, but it must be the SAME hash.

The blob hash seeds every chunk's deterministic point UUID, so a manifest that
disagreed with the filesystem would not raise -- it would re-identify chunks and
leave duplicate content behind. These pin that the manifest-backed answer equals
the read-from-disk answer, that a manifest MISS falls back to reading rather than
reporting "no hash", and that the read actually disappears when it can be served.
"""

from __future__ import annotations

import builtins
import subprocess
from pathlib import Path
from typing import Any, List

import pytest

from code_indexer.config import Config
from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.services.file_identifier import FileIdentifier

SHAPES = {
    "a.py": b"def a():\n    return 1\n",
    "crlf.py": b"x\r\ny\r\n",
    "empty.py": b"",
    "binary.dat": bytes([0, 1, 2, 255, 0]),
    "big.bin": b"q" * (1024 * 1024 + 7),
    "sub/nested.py": b"nested\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    for name, raw in SHAPES.items():
        (root / name).write_bytes(raw)

    def run(*a: str) -> None:
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    run("add", "-A")
    run("commit", "-q", "-m", "fixture")
    return root


def test_manifest_blob_hash_equals_the_read_from_disk_hash(repo: Path) -> None:
    without = FileIdentifier(repo, Config(codebase_dir=repo))
    with_manifest = FileIdentifier(
        repo, Config(codebase_dir=repo), manifest=RepoManifest.build(repo)
    )

    for name in SHAPES:
        target = repo / name
        read = without.get_file_metadata(target)
        served = with_manifest.get_file_metadata(target)
        assert read["git_available"], (
            "fixture must be a git repo or this branch is dead"
        )
        assert read.get("git_hash"), f"{name}: no hash computed from disk"
        assert served.get("git_hash") == read.get("git_hash"), (
            f"{name}: manifest hash differs"
        )


def test_the_file_is_not_opened_to_hash_it_when_the_manifest_can_serve(
    repo: Path,
) -> None:
    """The whole point: one NFS round trip per file removed."""
    target = repo / "a.py"
    identifier = FileIdentifier(
        repo, Config(codebase_dir=repo), manifest=RepoManifest.build(repo)
    )

    opened: List[str] = []
    real_open = builtins.open

    def spy(file: Any, *a: Any, **k: Any) -> Any:
        if str(file) == str(target):
            opened.append("open")
        return real_open(file, *a, **k)

    builtins.open = spy  # type: ignore[assignment]
    try:
        opened.clear()
        identifier._get_git_metadata(target)
    finally:
        builtins.open = real_open  # type: ignore[assignment]

    assert opened == [], f"the file was still opened {len(opened)} time(s) to hash it"


def test_a_file_added_after_the_manifest_falls_back_to_reading(repo: Path) -> None:
    """A manifest MISS means 'ask the filesystem', never 'no hash'."""
    manifest = RepoManifest.build(repo)
    late = repo / "added_later.py"
    late.write_bytes(b"added after the pass began\n")

    identifier = FileIdentifier(repo, Config(codebase_dir=repo), manifest=manifest)
    plain = FileIdentifier(repo, Config(codebase_dir=repo))

    assert manifest.blob_sha(late) is None, (
        "fixture is wrong: the manifest should not know this file"
    )
    assert identifier.get_file_metadata(late).get(
        "git_hash"
    ) == plain.get_file_metadata(late).get("git_hash")
    assert identifier.get_file_metadata(late).get("git_hash"), (
        "fell back to no hash instead of reading"
    )


def test_a_symlink_falls_back_to_reading_rather_than_reporting_no_hash(
    repo: Path, tmp_path: Path
) -> None:
    """The manifest reports symlinks without following them, so it has no hash to give."""
    import os

    link = repo / "link.py"
    os.symlink(repo / "a.py", link)
    manifest = RepoManifest.build(repo)

    assert manifest.blob_sha(link) is None
    identifier = FileIdentifier(repo, Config(codebase_dir=repo), manifest=manifest)
    plain = FileIdentifier(repo, Config(codebase_dir=repo))
    assert identifier.get_file_metadata(link).get(
        "git_hash"
    ) == plain.get_file_metadata(link).get("git_hash")


def test_a_hash_reported_for_a_symlink_is_refused_even_if_a_backend_offers_one(
    repo: Path,
) -> None:
    """Defence against a backend that breaks the manifest contract.

    The local backend sets blob_sha=None for symlinks, so dropping the is_symlink
    guard changes nothing against it -- the guard exists for a DELEGATED backend
    that wrongly reported a hash. Constructing that entry directly is the only way
    to exercise it, and without this the guard could be deleted unnoticed.

    Following it would be a real error: the hash would describe the link's TARGET,
    which may be another repository entirely, and that hash seeds the point UUID.
    """
    from code_indexer.indexing.repo_manifest import RepoManifest as _RM
    from code_indexer.server.storage.shared.content_backend import ContentEntry

    liar = _RM(
        repo,
        {
            "link.py": ContentEntry(
                path="link.py", size=7, blob_sha="deadbeef" * 5, is_symlink=True
            )
        },
    )
    assert liar.blob_sha(repo / "link.py") is None, (
        "a symlink's reported hash must not be used"
    )


def test_a_path_outside_the_repository_is_never_served_from_the_manifest(
    repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"not mine\n")
    assert RepoManifest.build(repo).blob_sha(outside) is None


def test_bytes_in_hand_still_win_over_the_manifest(repo: Path) -> None:
    """Cheapest first: content, then manifest, then read."""
    target = repo / "a.py"
    identifier = FileIdentifier(
        repo, Config(codebase_dir=repo), manifest=RepoManifest.build(repo)
    )
    data = target.read_bytes()
    assert identifier.get_file_metadata(target, content=data).get(
        "git_hash"
    ) == identifier.get_file_metadata(target).get("git_hash")


def test_building_the_manifest_locally_costs_one_read_per_file(repo: Path) -> None:
    """Why this is not wired into the indexer yet.

    The local backend computes blob hashes by reading, so building the manifest
    opens every file in the repository. Wiring it against the local backend
    therefore MOVES the hash reads to the front of the pass rather than removing
    them -- the saving only appears when a delegated backend reports the hashes
    without the client reading anything.

    Pinned as a measurement because it is the kind of claim that quietly stops
    being true: if the local backend ever learns to read hashes out of the git
    index instead, this test fails and wiring it locally becomes a real win.
    """
    hashable = [repo / name for name in SHAPES if not (repo / name).is_symlink()]
    names = {str(p) for p in hashable}

    opened: List[str] = []
    real_open = builtins.open
    real_read_bytes = Path.read_bytes

    def spy(file: Any, *a: Any, **k: Any) -> Any:
        if str(file) in names:
            opened.append(str(file))
        return real_open(file, *a, **k)

    def spy_rb(self: Path) -> bytes:
        if str(self) in names:
            opened.append(str(self))
        return real_read_bytes(self)

    builtins.open = spy  # type: ignore[assignment]
    Path.read_bytes = spy_rb  # type: ignore[method-assign]
    try:
        manifest = RepoManifest.build(repo)
    finally:
        builtins.open = real_open  # type: ignore[assignment]
        Path.read_bytes = real_read_bytes  # type: ignore[method-assign]

    assert len(manifest) >= len(hashable), "fixture did not reach the whole repo"
    assert len(opened) >= len(hashable), (
        f"building the manifest over {len(hashable)} files opened only "
        f"{len(opened)} of them -- if the local backend no longer reads to hash, "
        "re-measure whether wiring the manifest into the indexer now pays off"
    )
