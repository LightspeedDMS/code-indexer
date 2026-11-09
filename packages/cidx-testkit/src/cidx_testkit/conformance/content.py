"""Conformance suite for the content seam (``cidx_sdk.seams.content``).

Every content backend -- the local one, each delegate, the testkit's fake --
must pass the same checks. To run them, subclass :class:`ContentBackendContract`
in a test module and implement the ``content_subject`` fixture: it returns a
:class:`ContentSubject` holding the backend under test and a way to lay files
out in the repository the backend serves::

    class TestLocalContentBackend(ContentBackendContract):
        @pytest.fixture
        def content_subject(self, tmp_path):
            return directory_subject(LocalContentBackend(), tmp_path / "repo")

The checks describe the contract, not one implementation: entry ORDER is only
required to be stable, the absent-root answer may be an empty listing (local)
or the 400 the delegated service sends, and the manifest options are checked
only when the backend advertises them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import pytest

from cidx_sdk.seams.content import (
    MANIFEST_OPTION_HASH,
    MANIFEST_OPTION_MAX_DEPTH,
    MANIFEST_OPTION_PREFIX,
    MANIFEST_OPTIONS,
    NOT_FOUND_FETCH_ERRORS,
    REFUSED_FETCH_ERRORS,
    UNREADABLE_FETCH_ERRORS,
    ContentBackend,
    ContentEntry,
    is_absent_root_error,
)
from cidx_sdk.seams.content_identity import git_blob_hash_bytes


@dataclass
class ContentSubject:
    """A backend under test and the repository it serves.

    Attributes:
        backend: the implementation under test.
        root: the repository path as the BACKEND is addressed with it.
        write: ``write(rel, data)`` creates a file (and its directories).
        symlink: ``symlink(rel, target)`` creates a symlink at *rel*.
        missing_root: a root the backend does not hold at all.
    """

    backend: ContentBackend
    root: str
    write: Callable[[str, bytes], None]
    symlink: Callable[[str, str], None]
    missing_root: str


def directory_subject(
    backend: ContentBackend, directory: Path, root: Optional[str] = None
) -> ContentSubject:
    """A subject whose repository is *directory* on this machine.

    *root* is what the backend is addressed with (default: the directory's
    path); a fake remote service that maps its own root passes it.
    """
    directory.mkdir(parents=True, exist_ok=True)

    def write(rel: str, data: bytes) -> None:
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def symlink(rel: str, target: str) -> None:
        link = directory / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link)

    served = root if root is not None else str(directory)
    return ContentSubject(
        backend=backend,
        root=served,
        write=write,
        symlink=symlink,
        missing_root=served.rstrip("/") + "-does-not-exist",
    )


def all_entries(
    backend: ContentBackend, root: str, page_size: int = 1000, **options: Any
) -> List[ContentEntry]:
    """Every entry of the manifest, following the cursor to the end."""
    entries: List[ContentEntry] = []
    cursor: Optional[str] = None
    for _ in range(10_000):
        page = backend.manifest(root, cursor=cursor, page_size=page_size, **options)
        entries.extend(page.entries)
        if page.next_cursor is None:
            return entries
        assert page.next_cursor != cursor, "the manifest cursor did not advance"
        cursor = page.next_cursor
    raise AssertionError("the manifest never reached its last page")


def _advertised(backend: ContentBackend) -> frozenset:
    declared = getattr(backend, "manifest_options", None)
    if isinstance(declared, (set, frozenset, tuple, list)):
        return frozenset(declared) & MANIFEST_OPTIONS
    return frozenset()


_FILES: Dict[str, bytes] = {
    "README.md": b"# example\n",
    "src/app.py": b"print('hello')\n",
    "src/pkg/mod.py": b"VALUE = 1\n",
    "src/pkg/deep/leaf.txt": b"leaf\n",
    "docs/guide.md": b"guide\n",
    "node_modules/dep/index.js": b"module.exports = 1\n",
    ".git/HEAD": b"ref: refs/heads/main\n",
}


class ContentBackendContract:
    """The content seam's contract. Subclass and provide ``content_subject``."""

    @pytest.fixture
    def content_subject(self, tmp_path: Path) -> ContentSubject:
        raise NotImplementedError("provide the backend under test")

    @pytest.fixture
    def populated(self, content_subject: ContentSubject) -> ContentSubject:
        for rel, data in _FILES.items():
            content_subject.write(rel, data)
        return content_subject

    # -- identity --------------------------------------------------------------

    def test_is_local_is_a_bool(self, content_subject: ContentSubject) -> None:
        assert isinstance(content_subject.backend.is_local, bool)

    def test_manifest_lists_every_file_relative_and_skips_git(
        self, populated: ContentSubject
    ) -> None:
        entries = all_entries(populated.backend, populated.root)
        paths = [e.path for e in entries]
        expected = {p for p in _FILES if not p.startswith(".git/")}
        assert set(paths) == expected
        assert len(paths) == len(set(paths)), "an entry was listed twice"
        assert all(not p.startswith("/") and "\\" not in p for p in paths)

    def test_blob_sha_is_the_git_blob_hash(self, populated: ContentSubject) -> None:
        for entry in all_entries(populated.backend, populated.root):
            assert entry.size == len(_FILES[entry.path])
            assert entry.blob_sha == git_blob_hash_bytes(_FILES[entry.path])
            assert entry.is_symlink is False
            assert entry.mtime_ns is None or isinstance(entry.mtime_ns, int)

    def test_paging_covers_every_entry_once_in_a_stable_order(
        self, populated: ContentSubject
    ) -> None:
        whole = [e.path for e in all_entries(populated.backend, populated.root)]
        paged = [
            e.path for e in all_entries(populated.backend, populated.root, page_size=2)
        ]
        assert paged == whole
        again = [e.path for e in all_entries(populated.backend, populated.root)]
        assert again == whole

    def test_prune_dirs_are_skipped_at_any_depth(
        self, populated: ContentSubject
    ) -> None:
        populated.write("src/node_modules/x.js", b"x\n")
        paths = {
            e.path
            for e in all_entries(
                populated.backend, populated.root, prune_dirs=("node_modules",)
            )
        }
        assert not any("node_modules" in p.split("/") for p in paths)
        assert "src/app.py" in paths

    def test_a_symlink_is_listed_never_followed(
        self, populated: ContentSubject
    ) -> None:
        populated.symlink("link.md", "README.md")
        entries = {e.path: e for e in all_entries(populated.backend, populated.root)}
        assert entries["link.md"].is_symlink is True
        assert entries["link.md"].blob_sha is None

    def test_an_unsafe_cursor_is_refused(self, populated: ContentSubject) -> None:
        for cursor in ("../other/a", "/etc/passwd", "./a"):
            with pytest.raises(Exception) as caught:
                populated.backend.manifest(populated.root, cursor=cursor)
            assert not isinstance(caught.value, AssertionError)

    def test_an_absent_root_is_empty_or_the_absent_root_answer(
        self, content_subject: ContentSubject
    ) -> None:
        try:
            page = content_subject.backend.manifest(content_subject.missing_root)
        except Exception as exc:
            assert is_absent_root_error(exc), f"an outage-shaped error: {exc!r}"
        else:
            assert page.entries == () and page.next_cursor is None

    # -- fetch -----------------------------------------------------------------

    def test_fetch_yields_bytes_in_request_order(
        self, populated: ContentSubject
    ) -> None:
        wanted = ["src/pkg/mod.py", "README.md", "docs/guide.md"]
        got = list(populated.backend.fetch(populated.root, wanted))
        assert [f.path for f in got] == wanted
        for fetched in got:
            assert fetched.error is None
            assert fetched.data == _FILES[fetched.path]
            assert fetched.blob_sha == git_blob_hash_bytes(fetched.data)

    def test_a_per_file_failure_is_a_record_not_an_exception(
        self, populated: ContentSubject
    ) -> None:
        wanted = ["missing.txt", "no/such/dir/file.txt", "README.md"]
        got = list(populated.backend.fetch(populated.root, wanted))
        assert [f.path for f in got] == wanted
        for fetched in got[:2]:
            assert fetched.data == b"" and fetched.blob_sha is None
            assert fetched.error in NOT_FOUND_FETCH_ERRORS, fetched.error
        assert got[2].error is None and got[2].data == _FILES["README.md"]

    def test_a_directory_is_unreadable(self, populated: ContentSubject) -> None:
        (fetched,) = populated.backend.fetch(populated.root, ["src"])
        assert fetched.error in UNREADABLE_FETCH_ERRORS, fetched.error

    @pytest.mark.parametrize("path", ["../escape.txt", "/etc/hostname", "a/./b", ""])
    def test_an_unsafe_path_is_refused_per_file(
        self, populated: ContentSubject, path: str
    ) -> None:
        (fetched,) = populated.backend.fetch(populated.root, [path])
        assert fetched.data == b""
        assert fetched.error in REFUSED_FETCH_ERRORS, fetched.error

    def test_a_symlink_is_not_fetched(self, populated: ContentSubject) -> None:
        populated.symlink("link.md", "README.md")
        (fetched,) = populated.backend.fetch(populated.root, ["link.md"])
        assert fetched.data == b""
        assert fetched.error in REFUSED_FETCH_ERRORS, fetched.error

    # -- options (only when advertised) ---------------------------------------

    def test_hash_false_omits_blob_sha(self, populated: ContentSubject) -> None:
        if MANIFEST_OPTION_HASH not in _advertised(populated.backend):
            pytest.skip("the backend does not advertise the hash option")
        entries = all_entries(populated.backend, populated.root, hash=False)
        assert entries and all(e.blob_sha is None for e in entries)

    def test_prefix_and_max_depth_scope_the_listing(
        self, populated: ContentSubject
    ) -> None:
        options = _advertised(populated.backend)
        if not {MANIFEST_OPTION_PREFIX, MANIFEST_OPTION_MAX_DEPTH} <= options:
            pytest.skip("the backend does not advertise prefix/max_depth")
        scoped = {
            e.path for e in all_entries(populated.backend, populated.root, prefix="src")
        }
        assert scoped == {"src/app.py", "src/pkg/mod.py", "src/pkg/deep/leaf.txt"}
        shallow = {
            e.path
            for e in all_entries(
                populated.backend, populated.root, prefix="src", max_depth=1
            )
        }
        assert shallow == {"src/app.py"}
        with pytest.raises(ValueError):
            populated.backend.manifest(populated.root, prefix="../src")


def content_conformance_checks() -> Sequence[str]:
    """The names of the checks, for reporting which a backend ran."""
    return sorted(
        name for name in vars(ContentBackendContract) if name.startswith("test_")
    )
