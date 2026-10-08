"""Under a delegated backend, discovery comes from the manifest, not a walk.

The gate: the files a delegated pass discovers are EXACTLY the files the local
walk discovers -- same gitignore rules, same pruning, same size limit -- apart
from one documented difference, symlinks. A changed file set changes which
point ids exist, and the identity of the index rides on that set.

The fixture carries every case the plan names, and the ones that bit before:
an ignored file, an untracked file, a file excluded by exclude_dirs, a nested
.gitignore (read) and a second-level one (never read, by either mode), a CRLF
.gitignore, an oversized file, a wrong extension, a default-pruned directory, an
in-root symlink, an out-of-root symlink, a dangling symlink and a symlinked
directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, List, Sequence, Set

import pytest

from code_indexer.config import Config, IndexingConfig
from code_indexer.indexing.file_finder import FileFinder
from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import (
    FetchedFile,
    LocalContentBackend,
)

MAX_SIZE = 2000


class _Delegated:
    """Serves ``source``'s files whatever root it is asked about, and records fetches.

    Lets the finder run against a codebase path that does not exist here, so
    any local read, walk or stat in the delegated path fails by itself.
    """

    is_local = False

    def __init__(self, source: Path) -> None:
        self._source = source
        self._inner = LocalContentBackend()
        self.fetches: List[List[str]] = []

    def manifest(self, repo_path: Any, **k: Any) -> Any:
        return self._inner.manifest(self._source, **k)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        self.fetches.append(list(paths))
        return self._inner.fetch(self._source, paths)


def _write(root: Path, rel: str, data: bytes = b"x = 1\n") -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "main.py")
    _write(root, "untracked_new.py")  # discovery is a walk, not git ls-files
    _write(root, "pkg/mod.py")
    _write(root, "pkg/deep/leaf.py")
    _write(
        root,
        ".gitignore",
        b"ignored.py\r\ngen/\r\n!gen/keep.py\r\n# comment\r\n\r\n/anchored.py\r\n",
    )
    _write(root, "ignored.py")
    _write(root, "anchored.py")
    _write(root, "pkg/anchored.py")  # "/anchored.py" is root-anchored: kept
    _write(root, "gen/out.py")
    # Negated under an ignored directory: a pattern match alone would re-include
    # it, but the walk never descends into gen/ -- only pruning keeps it out.
    _write(root, "gen/keep.py")
    _write(root, "pkg/.gitignore", b"local.py\n")
    _write(root, "pkg/local.py")
    _write(root, "local.py")  # pkg's rule is scoped to pkg/: kept
    _write(root, "pkg/deep/.gitignore", b"leaf2.py\n")  # second level: never read
    _write(root, "pkg/deep/leaf2.py")
    _write(root, "vendor/lib.py")  # exclude_dirs
    _write(root, "node_modules/dep.py")  # default-pruned
    _write(root, "big.py", b"#" * (MAX_SIZE + 1))
    _write(root, "data.bin", b"\x00\x01")
    (root / "link_in.py").symlink_to(root / "main.py")
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"secret = 1\n")
    (root / "link_out.py").symlink_to(outside)
    (root / "dangling.py").symlink_to(root / "nope.py")
    (root / "linked_dir").symlink_to(root / "pkg", target_is_directory=True)
    return root


def _config(root: Path) -> Config:
    config = Config(
        codebase_dir=root,
        file_extensions=["py"],
        exclude_dirs=["vendor"],
        indexing=IndexingConfig(max_file_size=MAX_SIZE),
    )
    config.confine_to_codebase_root()  # server context, where delegation runs
    return config


def _relative(root: Path, found: Iterator[Path]) -> Set[str]:
    return {p.relative_to(root).as_posix() for p in found}


def test_manifest_discovery_matches_the_walk_except_symlinks(repo: Path) -> None:
    walked = _relative(repo, FileFinder(_config(repo)).find_files())

    backend = _Delegated(repo)
    finder = FileFinder(_config(repo))
    finder.set_manifest(RepoManifest.build(repo, backend=backend), backend)
    from_manifest = _relative(repo, finder.find_files())

    # The documented difference: an in-root symlink is not followed; its target
    # is discovered as its own entry, so the content is still indexed once.
    assert walked - from_manifest == {"link_in.py"}
    assert from_manifest == walked - {"link_in.py"}
    assert "main.py" in from_manifest
    # And the set itself, so a rule both modes got wrong cannot hide.
    assert from_manifest == {
        "main.py",
        "untracked_new.py",
        "pkg/mod.py",
        "pkg/deep/leaf.py",
        "pkg/deep/leaf2.py",
        "pkg/anchored.py",
        "local.py",
    }


def test_delegated_discovery_never_touches_the_disk(repo: Path, tmp_path: Path) -> None:
    """The codebase path does not exist here: any walk, stat or read would fail."""
    absent = tmp_path / "not-mounted"
    backend = _Delegated(repo)
    finder = FileFinder(_config(absent))
    finder.set_manifest(RepoManifest.build(absent, backend=backend), backend)

    found = _relative(absent, finder.find_files())

    assert "main.py" in found and "ignored.py" not in found and "big.py" not in found


def test_the_gitignores_are_fetched_in_one_call(repo: Path) -> None:
    backend = _Delegated(repo)
    finder = FileFinder(_config(repo))
    finder.set_manifest(RepoManifest.build(repo, backend=backend), backend)
    assert backend.fetches == [[".gitignore", "pkg/.gitignore"]]


def test_clearing_the_manifest_restores_the_walk_without_rereading(
    repo: Path,
) -> None:
    finder = FileFinder(_config(repo))
    local_spec = finder.exclude_spec
    backend = _Delegated(repo)
    finder.set_manifest(RepoManifest.build(repo, backend=backend), backend)
    finder.set_manifest(None)
    assert finder.exclude_spec is local_spec
    assert "link_in.py" in _relative(repo, finder.find_files())


def test_modified_files_use_the_manifests_mtime(repo: Path, tmp_path: Path) -> None:
    import os

    os.utime(repo / "main.py", ns=(2_000_000_000 * 10**9,) * 2)
    absent = tmp_path / "not-mounted"
    backend = _Delegated(repo)
    finder = FileFinder(_config(absent))
    finder.set_manifest(RepoManifest.build(absent, backend=backend), backend)

    modified = _relative(absent, finder.find_modified_files(1_900_000_000.0))

    assert modified == {"main.py"}


def test_eligibility_of_a_path_outside_the_manifest_is_no(repo: Path) -> None:
    """Added after the pass began: not eligible, and never a stat to find out."""
    backend = _Delegated(repo)
    finder = FileFinder(_config(repo))
    finder.set_manifest(RepoManifest.build(repo, backend=backend), backend)
    _write(repo, "late.py")
    assert finder.is_eligible(repo / "late.py") is False
    assert finder.is_eligible(repo / "main.py") is True


# ── the pass: one snapshot for discovery and processing ────────────────────


class _CountingDelegated(_Delegated):
    def __init__(self, source: Path) -> None:
        super().__init__(source)
        self.builds = 0

    def manifest(self, repo_path: Any, **k: Any) -> Any:
        if k.get("cursor") is None:
            self.builds += 1
        return super().manifest(repo_path, **k)


def _ht_processor(repo: Path, backend: Any) -> Any:
    from unittest.mock import MagicMock

    from code_indexer.services.high_throughput_processor import (
        HighThroughputProcessor,
    )

    return HighThroughputProcessor(_config(repo), MagicMock(), MagicMock(), backend)


def test_the_processing_pass_joins_an_open_pass(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """smart_index opened the pass for discovery; processing must read against
    THAT snapshot, and must not close it out from under the caller."""
    backend = _CountingDelegated(repo)
    proc = _ht_processor(repo, backend)
    seen: List[Any] = []
    monkeypatch.setattr(
        proc,
        "_process_files_high_throughput",
        lambda *a, **k: seen.append(proc.repo_manifest),
    )

    proc.begin_manifest_pass()  # what smart_index does
    outer = proc.repo_manifest
    assert proc.file_finder._manifest is outer, "discovery is not on the manifest"
    proc.process_files_high_throughput([], vector_thread_count=1)

    assert seen == [outer]
    assert backend.builds == 1
    assert proc.repo_manifest is outer, "the inner pass closed the caller's pass"
    proc.end_manifest_pass()
    assert proc.repo_manifest is None and proc.file_finder._manifest is None


def test_the_processing_pass_alone_opens_and_closes_its_own(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Watch mode and the branch-change path call it with no pass open."""
    backend = _CountingDelegated(repo)
    proc = _ht_processor(repo, backend)
    seen: List[Any] = []
    monkeypatch.setattr(
        proc,
        "_process_files_high_throughput",
        lambda *a, **k: seen.append(proc.repo_manifest),
    )

    proc.process_files_high_throughput([], vector_thread_count=1)

    assert seen[0] is not None
    assert backend.builds == 1
    assert proc.repo_manifest is None and not proc.manifest_pass_open


def test_smart_index_opens_the_pass_around_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_indexer.services.smart_indexer import SmartIndexer

    seen = {}
    indexer = SmartIndexer.__new__(SmartIndexer)
    events: List[str] = []
    monkeypatch.setattr(indexer, "begin_manifest_pass", lambda: events.append("begin"))
    monkeypatch.setattr(indexer, "end_manifest_pass", lambda: events.append("end"))

    def body(**kwargs: Any) -> str:
        events.append("discover+process")
        seen.update(kwargs)
        return "stats"

    monkeypatch.setattr(indexer, "_smart_index", body)

    assert indexer.smart_index(force_full=True, enable_fts=True) == "stats"
    assert events == ["begin", "discover+process", "end"]
    assert seen["force_full"] is True and seen["enable_fts"] is True


def test_a_lone_cr_gitignore_splits_like_a_text_mode_read(tmp_path: Path) -> None:
    """The walk read .gitignore in text mode (universal newlines); fetched bytes
    must split the same way, or two rules fuse into one that matches nothing."""
    from code_indexer.indexing.file_finder import _gitignore_lines

    raw = b"a.py\rb.py\r\nc.py\n"
    (tmp_path / ".gitignore").write_bytes(raw)
    with open(tmp_path / ".gitignore", encoding="utf-8", errors="ignore") as f:
        text_mode = [line.strip() for line in f if line.strip()]

    assert _gitignore_lines(raw.decode("utf-8"), None) == text_mode
    assert _gitignore_lines(raw.decode("utf-8"), "pkg") == [
        "pkg/a.py",
        "pkg/b.py",
        "pkg/c.py",
    ]
