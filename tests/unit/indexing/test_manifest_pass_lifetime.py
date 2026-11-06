"""The manifest's lifetime, and what a missing hash does NOT mean.

Two things this pins:

* **The manifest is a snapshot, so it lives exactly one pass.** An indexer
  outlives a pass -- watch mode holds one for a whole session, the reprocess pass
  reuses the same instance -- and a manifest kept across passes would serve the
  blob hash a file had *before* it was edited. The point UUID is seeded from that
  hash, so the new content would be written over the old version's ids.

* **A symlink is NOT dropped from the pass.** An earlier version of this module
  dropped them, on the measured grounds that a symlink's chunks are
  byte-identical to its target's and carry the same ids. That holds only when the
  target is itself in the pass, and ``ContentEntry`` carries no target, so there
  is no way to tell the lossless case from the lossy one. Symlinks take the
  ordinary path and end as counted failures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence
from unittest.mock import MagicMock

import pytest

from code_indexer.config import Config
from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import (
    ContentEntry,
    ContentPage,
    LocalContentBackend,
)
from code_indexer.services import git_aware_processor as gap


class _StubBackend:
    """Serves a manifest. ``is_local`` is the switch; ``builds`` counts calls."""

    def __init__(
        self, entries: Sequence[ContentEntry], *, is_local: bool = False
    ) -> None:
        self._entries = tuple(entries)
        self.is_local = is_local
        self.builds = 0

    def set_entries(self, entries: Sequence[ContentEntry]) -> None:
        self._entries = tuple(entries)

    def manifest(
        self, repo_path: Any, *, cursor: Optional[str] = None, page_size: int = 1000
    ) -> ContentPage:
        self.builds += 1
        return ContentPage(entries=self._entries, next_cursor=None)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[Any]:
        raise AssertionError("the hash phase must not fetch bytes")


def _entry(path: str, sha: Optional[str], *, link: bool = False) -> ContentEntry:
    return ContentEntry(path=path, size=10, blob_sha=sha, is_symlink=link)


_ENTRIES = (
    _entry("pkg/real.py", "a" * 40),
    _entry("pkg/alias.py", None, link=True),
    _entry("pkg/locked.py", None),
)


def _processor(
    monkeypatch: pytest.MonkeyPatch, backend: Any, tmp_path: Path
) -> gap.GitAwareDocumentProcessor:
    monkeypatch.setattr(gap, "GitDetectionService", lambda *a, **k: MagicMock())
    monkeypatch.setattr(gap.DocumentProcessor, "__init__", lambda self, *a, **k: None)
    proc = gap.GitAwareDocumentProcessor(
        Config(codebase_dir=tmp_path), MagicMock(), MagicMock(), backend
    )
    # DocumentProcessor.__init__ is stubbed, so set what it would have set.
    proc.config = Config(codebase_dir=tmp_path)
    proc.file_finder = MagicMock()
    return proc


# ── the manifest's lifetime is a PASS, not an instance ─────────────────────


def test_no_manifest_exists_before_a_pass_begins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Building in __init__ is what made a reused indexer serve stale hashes."""
    backend = _StubBackend(_ENTRIES)
    proc = _processor(monkeypatch, backend, tmp_path)
    assert proc.repo_manifest is None
    assert backend.builds == 0, "the constructor paged the repository"


def test_each_pass_rebuilds_the_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = _StubBackend(_ENTRIES)
    proc = _processor(monkeypatch, backend, tmp_path)
    for expected in (1, 2, 3):
        proc.begin_manifest_pass()
        assert backend.builds == expected
        proc.end_manifest_pass()


def test_a_file_edited_between_passes_gets_its_NEW_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bug a per-instance manifest caused, stated as behaviour.

    Chunking the new bytes under the old hash would write them over the previous
    version's point ids, because the id is seeded from the hash.
    """
    backend = _StubBackend([_entry("a.py", "a" * 40)])
    proc = _processor(monkeypatch, backend, tmp_path)

    proc.begin_manifest_pass()
    first = proc.file_identifier._get_git_metadata(tmp_path / "a.py")["git_hash"]
    proc.end_manifest_pass()

    backend.set_entries([_entry("a.py", "b" * 40)])

    proc.begin_manifest_pass()
    second = proc.file_identifier._get_git_metadata(tmp_path / "a.py")["git_hash"]
    proc.end_manifest_pass()

    assert first == "a" * 40
    assert second == "b" * 40, (
        "the second pass served the hash from the first -- an edited file would "
        "be chunked under its old identity"
    )


def test_the_manifest_is_dropped_when_the_pass_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing outside a pass should be able to read a snapshot of one."""
    backend = _StubBackend(_ENTRIES)
    proc = _processor(monkeypatch, backend, tmp_path)
    proc.begin_manifest_pass()
    assert proc.repo_manifest is not None
    proc.end_manifest_pass()
    assert proc.repo_manifest is None
    assert proc.file_identifier._manifest is None


def test_the_pass_entry_point_brackets_the_manifest() -> None:
    """Driving the real entry point needs a provider, a store and a thread pool.

    Source-level instead: the build must happen inside
    ``process_files_high_throughput`` and the teardown must be in a ``finally``,
    or an exception mid-pass leaves a stale snapshot on a reused indexer.
    """
    import ast
    import inspect

    from code_indexer.services.high_throughput_processor import (
        HighThroughputProcessor,
    )

    tree = ast.parse(inspect.getsource(HighThroughputProcessor))
    entry = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "process_files_high_throughput"
    )

    def calls(name: str, scope: Any) -> List[Any]:
        return [
            c
            for c in ast.walk(scope)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == name
        ]

    assert calls("begin_manifest_pass", entry), "the pass never builds a manifest"
    tries = [n for n in ast.walk(entry) if isinstance(n, ast.Try)]
    assert any(calls("end_manifest_pass", t) and t.finalbody for t in tries), (
        "end_manifest_pass is not in a finally, so a failed pass leaves a stale "
        "manifest on a reused indexer"
    )


# ── nothing happens without a delegated backend ────────────────────────────


@pytest.mark.parametrize("supply_local_backend", [True, False])
def test_no_manifest_is_built_without_a_delegated_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, supply_local_backend: bool
) -> None:
    """Against local the manifest is a measured wash: 40 opens either way.

    Building it anyway would move every per-file hash read to the front of the
    pass and hash files the indexer would have filtered out.
    """
    backend = _StubBackend(_ENTRIES, is_local=True) if supply_local_backend else None
    proc = _processor(monkeypatch, backend, tmp_path)
    proc.begin_manifest_pass()
    assert proc.repo_manifest is None
    if backend is not None:
        assert backend.builds == 0
    proc.end_manifest_pass()


def test_ending_a_pass_that_never_began_is_harmless(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    proc = _processor(monkeypatch, None, tmp_path)
    proc.end_manifest_pass()
    assert proc.repo_manifest is None


# ── a missing hash is not a reason to skip the file ────────────────────────


def test_nothing_filters_the_file_list_on_a_missing_hash() -> None:
    """Dropping is lossless only when the target is also indexed.

    A link pointing outside the root, at an excluded or generated path, or at
    nothing, carries content reachable no other way -- and the manifest has no
    target field to tell those apart. So nothing is dropped; a symlink takes the
    ordinary path and ends as a counted failure.
    """
    from code_indexer.services.high_throughput_processor import (
        HighThroughputProcessor,
    )

    assert not hasattr(RepoManifest, "unindexable_reason"), (
        "a skip predicate is back; it cannot be made correct from a manifest "
        "entry, which carries no symlink target"
    )
    assert not hasattr(HighThroughputProcessor, "_drop_unindexable"), (
        "the pass filters its file list again"
    )


def test_a_symlink_entry_still_yields_no_hash_from_the_manifest(
    tmp_path: Path,
) -> None:
    """Unchanged: the manifest cannot vouch for a symlink, so the caller reads."""
    manifest = RepoManifest.build(tmp_path, backend=_StubBackend(_ENTRIES))
    assert manifest.blob_sha(tmp_path / "pkg/alias.py") is None
    assert manifest.blob_sha(tmp_path / "pkg/real.py") == "a" * 40


def test_the_real_local_backend_reports_a_symlink_without_a_hash(
    tmp_path: Path,
) -> None:
    """Uses the SHIPPING LocalContentBackend, not a stub that claims a shape."""
    (tmp_path / "a.py").write_bytes(b"x = 1\n")
    (tmp_path / "alias.py").symlink_to("a.py")
    manifest = RepoManifest.build(tmp_path, backend=LocalContentBackend())
    assert manifest.blob_sha(tmp_path / "alias.py") is None
    assert manifest.blob_sha(tmp_path / "a.py") is not None


# ── the premise behind "a symlink adds nothing", driven through production ──


def test_the_point_id_seed_does_not_include_the_file_path() -> None:
    """Calls the REAL id builder, with metadata that differs only by path.

    An earlier version of this test recomputed ``uuid5`` inline and compared the
    result to itself, so it held by construction and could not fail when the
    production seed changed. This drives
    ``GitAwareDocumentProcessor._create_point_id`` instead: if the path is ever
    folded into the seed, these two stop matching.

    The property is what made "a symlink's chunks are its target's chunks" true
    in the first place, and it is still worth knowing when it stops being true.
    """
    shared_hash = "c" * 40
    base = {
        "project_id": "proj",
        "git_available": True,
        "git_hash": shared_hash,
        "file_hash": "sha256:unused",
    }
    target = dict(base, file_path="pkg/real.py", relative_path="pkg/real.py")
    link = dict(base, file_path="pkg/alias.py", relative_path="pkg/alias.py")

    make = gap.GitAwareDocumentProcessor._create_point_id
    assert [make(None, target, i) for i in range(3)] == [
        make(None, link, i) for i in range(3)
    ], "the point id now depends on the path"


def test_the_point_id_does_change_with_the_hash_and_the_index() -> None:
    """Stops the test above passing because the builder ignores everything."""
    base = {
        "project_id": "proj",
        "git_available": True,
        "git_hash": "c" * 40,
        "file_hash": "sha256:unused",
    }
    make = gap.GitAwareDocumentProcessor._create_point_id
    assert make(None, base, 0) != make(None, base, 1), "chunk index is ignored"
    assert make(None, base, 0) != make(None, dict(base, git_hash="d" * 40), 0), (
        "the blob hash is ignored"
    )
    assert make(None, base, 0) != make(None, dict(base, project_id="other"), 0), (
        "the project is ignored"
    )


def test_beginning_a_pass_always_rebuilds_even_without_a_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``begin`` must not be a build-once guard, independently of ``end``.

    Written after a mutation survived: adding ``if self.repo_manifest is None``
    around the build kept every other test green, because ``end_manifest_pass``
    clears it between passes and the guard never fired. The two together are what
    caused the original bug, so each has to be pinned on its own.
    """
    backend = _StubBackend(_ENTRIES)
    proc = _processor(monkeypatch, backend, tmp_path)
    proc.begin_manifest_pass()
    proc.begin_manifest_pass()
    assert backend.builds == 2, (
        "the second begin reused the first pass's snapshot instead of rebuilding"
    )
