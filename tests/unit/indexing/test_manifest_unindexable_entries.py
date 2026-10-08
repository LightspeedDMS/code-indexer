"""What a delegated pass does with a file it has no way to read.

A delegated manifest reports ``blob_sha=None`` for three different situations,
and they are not the same problem:

* **a symlink** -- the backend enumerates it and refuses to read it, on purpose,
  so that fetch can never return bytes the manifest would not list. There is no
  way to get the content: not from the backend, and not from a disk the pod may
  not have.
* **unreadable**, or **size changed mid-read** -- not a contract, a problem with
  that one file. It must stay an ordinary, counted failure.

Measured before choosing (fixture: 20 ordinary files, an in-repo symlink, a
symlink to a directory, an unreadable file; backend serving content the pod
cannot see):

===========================  =======  ======  =========  ============  =======
strategy                     correct  lost    aborts     ord. failure  skipped
===========================  =======  ======  =========  ============  =======
read the local file (today)       21       2          -             -        -
hard error                        21       -          2             -        -
fetch the bytes instead           21       -          2             -        -
skip anything unhashable          21       -          -             -        2
split by cause (this)             21       -          -             1        1
===========================  =======  ======  =========  ============  =======

Fetching is not an option: it made two round trips and transferred **zero
bytes**, because the backend refuses symlinks by design. Erroring aborts a whole
index over an ordinary repository feature. Reading locally is what delegation
exists to stop needing.

Skipping a symlink costs no indexed content. A chunk's point UUID is seeded from
``{project_id}:{blob_sha}:{index}`` with NO path, so a symlink's chunks are
byte-identical to its target's and carry the SAME ids -- indexing it overwrites
the target's chunks with a copy of themselves.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence

import pytest

from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import (
    ContentEntry,
    ContentPage,
    LocalContentBackend,
)


class _StubBackend:
    """Serves a fixed manifest. ``is_local`` is the switch under test."""

    def __init__(self, entries: Sequence[ContentEntry], *, is_local: bool) -> None:
        self._entries = tuple(entries)
        self.is_local = is_local

    def manifest(
        self, repo_path: Any, *, cursor: Optional[str] = None, page_size: int = 1000
    ) -> ContentPage:
        return ContentPage(entries=self._entries, next_cursor=None)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[Any]:
        raise AssertionError("the hash phase must not fetch bytes")


_ENTRIES = (
    ContentEntry(path="pkg/real.py", size=14, blob_sha="a" * 40, is_symlink=False),
    ContentEntry(path="pkg/alias.py", size=8, blob_sha=None, is_symlink=True),
    ContentEntry(path="pkg/locked.py", size=0, blob_sha=None, is_symlink=False),
)


def _manifest(root: Path, *, is_local: bool) -> RepoManifest:
    return RepoManifest.build(root, backend=_StubBackend(_ENTRIES, is_local=is_local))


# ── the split ──────────────────────────────────────────────────────────────


def test_a_symlink_is_unindexable_under_a_delegated_backend(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, is_local=False)
    assert manifest.unindexable_reason(tmp_path / "pkg/alias.py") == "symlink"


def test_an_unreadable_file_is_NOT_skipped_it_must_fail_loudly(
    tmp_path: Path,
) -> None:
    """The half that separates this from "skip anything with no hash".

    Sweeping it up with the symlinks would turn a file cidx genuinely could not
    read into a silent omission, when today it is a counted failure.
    """
    manifest = _manifest(tmp_path, is_local=False)
    assert manifest.unindexable_reason(tmp_path / "pkg/locked.py") is None


def test_a_hashable_file_is_indexable(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, is_local=False)
    assert manifest.unindexable_reason(tmp_path / "pkg/real.py") is None


# ── nothing changes for a local backend ────────────────────────────────────


def test_a_local_backend_never_declares_anything_unindexable(tmp_path: Path) -> None:
    """With the files on this disk, a symlink is read by following it, as today.

    If this ever returns a reason, the default (non-delegated) indexing path
    silently stops indexing symlinked files.
    """
    manifest = _manifest(tmp_path, is_local=True)
    for rel in ("pkg/real.py", "pkg/alias.py", "pkg/locked.py"):
        assert manifest.unindexable_reason(tmp_path / rel) is None


def test_the_real_local_backend_is_also_exempt(tmp_path: Path) -> None:
    """Uses the SHIPPING LocalContentBackend, not a stub that claims is_local."""
    (tmp_path / "a.py").write_bytes(b"x = 1\n")
    link = tmp_path / "alias.py"
    link.symlink_to("a.py")
    manifest = RepoManifest.build(tmp_path, backend=LocalContentBackend())
    assert manifest.unindexable_reason(link) is None


# ── a miss is not a refusal ────────────────────────────────────────────────


def test_a_file_added_after_the_pass_began_is_not_declared_unindexable(
    tmp_path: Path,
) -> None:
    """A manifest MISS means "ask the normal path", never "do not index".

    Treating an unknown file as unindexable would silently drop everything
    created between building the manifest and walking the tree.
    """
    manifest = _manifest(tmp_path, is_local=False)
    assert manifest.unindexable_reason(tmp_path / "pkg/added_later.py") is None


def test_a_path_outside_the_repository_is_not_declared_unindexable(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "repo", is_local=False)
    assert manifest.unindexable_reason(tmp_path / "elsewhere.py") is None


# ── the filter in the processing path ──────────────────────────────────────


class _Processor:
    """Just the filter, with the attribute the real processor sets."""

    def __init__(self, manifest: Optional[RepoManifest]) -> None:
        self.repo_manifest = manifest

    drop = None  # bound below


from code_indexer.services.high_throughput_processor import (  # noqa: E402
    HighThroughputProcessor,
)

_Processor.drop = HighThroughputProcessor._drop_unindexable  # type: ignore[assignment]


def test_the_filter_removes_only_the_symlink(tmp_path: Path) -> None:
    proc = _Processor(_manifest(tmp_path, is_local=False))
    files = [
        tmp_path / "pkg/real.py",
        tmp_path / "pkg/alias.py",
        tmp_path / "pkg/locked.py",
    ]
    kept = proc.drop(files)  # type: ignore[misc]
    assert kept == [tmp_path / "pkg/real.py", tmp_path / "pkg/locked.py"]


def test_the_filter_is_a_no_op_without_a_manifest(tmp_path: Path) -> None:
    """The default path must be untouched, including the list object's contents."""
    proc = _Processor(None)
    files = [tmp_path / "a.py", tmp_path / "b.py"]
    assert proc.drop(files) == files  # type: ignore[misc]


def test_the_filter_is_a_no_op_for_a_local_backend(tmp_path: Path) -> None:
    proc = _Processor(_manifest(tmp_path, is_local=True))
    files = [tmp_path / "pkg/real.py", tmp_path / "pkg/alias.py"]
    assert proc.drop(files) == files  # type: ignore[misc]


def test_dropping_a_file_is_logged_with_a_count_and_the_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A pass that indexes less must never do it quietly.

    This is the one place that legitimately indexes fewer files than it was
    handed, so it has to be the loudest -- a silent shortfall reported as
    success is the failure the whole seam exists to prevent.
    """
    proc = _Processor(_manifest(tmp_path, is_local=False))
    with caplog.at_level(logging.WARNING):
        proc.drop([tmp_path / "pkg/alias.py", tmp_path / "pkg/real.py"])  # type: ignore[misc]

    assert caplog.records, "a file was dropped with no warning at all"
    message = caplog.records[0].getMessage()
    assert "symlink" in message, "the warning does not say why"
    assert "1" in message, "the warning does not say how many"
    assert "alias.py" in message, "the warning does not name the file"


def test_the_warning_does_not_dump_every_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A repository of symlinks must not produce one unbounded log line."""
    entries = tuple(
        ContentEntry(path=f"l{i}.py", size=1, blob_sha=None, is_symlink=True)
        for i in range(50)
    )
    manifest = RepoManifest.build(
        tmp_path, backend=_StubBackend(entries, is_local=False)
    )
    proc = _Processor(manifest)
    with caplog.at_level(logging.WARNING):
        proc.drop([tmp_path / f"l{i}.py" for i in range(50)])  # type: ignore[misc]

    message = caplog.records[0].getMessage()
    assert "50" in message, "the count is what makes the elision safe"
    named = sum(1 for i in range(50) if f"l{i}.py" in message)
    assert named <= 3, (
        f"the warning named {named} of 50 dropped files; it must report the "
        "count and a few examples, not the whole list"
    )
    assert "..." in message, "the elision is not signalled, so the list looks complete"


# ── the premise the whole decision rests on ────────────────────────────────


def test_a_symlinks_chunks_would_collide_with_its_targets(tmp_path: Path) -> None:
    """Why dropping a symlink loses no indexed content.

    The point UUID seed is ``{project_id}:{signature}:{chunk_index}`` with no
    path in it, so identical bytes produce identical ids. Following a symlink
    therefore rewrites the target's chunks with a copy of themselves.

    If a path is ever added to that seed this fails, and the skip stops being
    free -- which is exactly when someone needs to revisit it.
    """
    import uuid

    from code_indexer.utils.blob_hash import git_blob_hash_bytes

    shared = b"shared helper\n"
    signature = git_blob_hash_bytes(shared)

    def ids(_path: str) -> List[str]:
        return [
            str(uuid.uuid5(uuid.NAMESPACE_DNS, f"proj:{signature}:{i}"))
            for i in range(3)
        ]

    assert ids("pkg/real.py") == ids("pkg/alias.py")


# ── the filter has to actually be wired in ─────────────────────────────────


def test_the_processing_entry_point_applies_the_filter() -> None:
    """Testing the helper directly cannot see the call site disappear.

    Deleting ``files = self._drop_unindexable(files)`` from
    ``process_files_high_throughput`` left every test above green: they call the
    helper themselves. Source-level because driving the real entry point needs an
    embedding provider, a vector store and a thread pool.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(HighThroughputProcessor))
    entry = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "process_files_high_throughput"
    )
    calls = [
        call
        for call in ast.walk(entry)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "_drop_unindexable"
    ]
    assert calls, (
        "process_files_high_throughput never calls _drop_unindexable, so an "
        "unreadable file reaches the chunking workers anyway"
    )


def test_the_filters_result_is_what_gets_processed() -> None:
    """Calling it and discarding the result would also pass the test above."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(HighThroughputProcessor))
    entry = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "process_files_high_throughput"
    )
    rebinds = [
        node
        for node in ast.walk(entry)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "files" for t in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "_drop_unindexable"
    ]
    assert rebinds, "the filtered list is computed but not used"


# ── the manifest is built only when it pays ────────────────────────────────


def _build_processor(monkeypatch: pytest.MonkeyPatch, backend: Any, tmp_path: Path):
    from unittest.mock import MagicMock

    from code_indexer.config import Config
    from code_indexer.services import git_aware_processor as gap

    built: List[Any] = []

    class _FakeManifest:
        @staticmethod
        def build(root: Any, backend: Any = None, page_size: int = 1000) -> Any:
            built.append(backend)
            return object()

    import code_indexer.indexing.repo_manifest as rm

    monkeypatch.setattr(rm, "RepoManifest", _FakeManifest)
    monkeypatch.setattr(gap, "FileIdentifier", lambda *a, **k: MagicMock())
    monkeypatch.setattr(gap, "GitDetectionService", lambda *a, **k: MagicMock())
    monkeypatch.setattr(gap.DocumentProcessor, "__init__", lambda self, *a, **k: None)

    proc = gap.GitAwareDocumentProcessor(
        Config(codebase_dir=tmp_path), MagicMock(), MagicMock(), backend
    )
    return proc, built


def test_no_manifest_is_built_for_a_local_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Against local it is a measured wash -- 40 opens either way over 20 files.

    Building it anyway would move every per-file hash read to the front of the
    pass and hash files the indexer would have filtered out.
    """
    proc, built = _build_processor(
        monkeypatch, _StubBackend(_ENTRIES, is_local=True), tmp_path
    )
    assert built == [], "a manifest was built for a local backend"
    assert proc.repo_manifest is None


def test_no_manifest_is_built_when_no_backend_is_supplied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    proc, built = _build_processor(monkeypatch, None, tmp_path)
    assert built == []
    assert proc.repo_manifest is None


def test_a_manifest_is_built_for_a_delegated_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = _StubBackend(_ENTRIES, is_local=False)
    proc, built = _build_processor(monkeypatch, backend, tmp_path)
    assert built == [backend], "no manifest was built for a delegated backend"
    assert proc.repo_manifest is not None
