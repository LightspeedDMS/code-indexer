"""Conformance suite for the clone seam (``cidx_sdk.seams.clone``).

Every clone backend -- the local one, each delegate, the testkit's fake --
must pass the same checks. Subclass :class:`CloneBackendContract` and provide
the ``clone_subject`` fixture: a :class:`CloneSubject` holding the backend,
a source repository to clone and a directory the backend may clone into::

    class TestLocalClone(CloneBackendContract):
        @pytest.fixture
        def clone_subject(self, tmp_path):
            base = tmp_path / "golden"
            backend = make_local_clone_backend(
                CloneBackendSettings(services=plain_clone_services(),
                                     versioned_base=str(base)))
            return local_subject(backend, base)

The suite checks what callers rely on: the clone holds the source's files,
an existing destination is refused and left untouched, deletion is
idempotent, and the snapshot registry (``create_clone`` / ``clone_exists`` /
``list_clones``) agrees with itself. Cancellation of a running clone is
covered per implementation (it needs a clone slow enough to interrupt).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pytest

from cidx_sdk.seams.clone import (
    SHORT_POLL_SECONDS,
    CloneBackend,
    CloneServices,
)

_FILES: Dict[str, bytes] = {
    "README.md": b"# example\n",
    "src/app.py": b"print('hello')\n",
    "src/pkg/mod.py": b"VALUE = 1\n",
}


@dataclass
class CloneSubject:
    """A clone backend under test and where it works.

    Attributes:
        backend: the implementation under test.
        source: a repository directory (as the backend addresses it) holding
            the suite's files.
        dest_root: a directory (as the backend addresses it) under which the
            suite picks destinations that do not exist yet.
        read: ``read(path)`` -> the bytes of the file at *path* (an address
            the backend returned), wherever the backend put it.
        exists: ``exists(path)`` -> whether *path* exists there.
        make_dir: ``make_dir(path)`` creates a directory there.
        validates_identifiers: the backend refuses a namespace/name that is
            not a single safe path component (``ValueError``) rather than
            rewriting it (the CoW daemon sanitises instead).
    """

    backend: CloneBackend
    source: str
    dest_root: str
    read: Callable[[str], bytes]
    exists: Callable[[str], bool]
    make_dir: Callable[[str], None]
    validates_identifiers: bool = True


def local_subject(backend: CloneBackend, base: Path) -> CloneSubject:
    """A subject whose source and destinations are directories of this
    machine under *base* (the backend's versioned base)."""
    source = base / "source-repo"
    for rel, data in _FILES.items():
        target = source / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    dest_root = base / "clones"
    dest_root.mkdir(parents=True, exist_ok=True)
    return CloneSubject(
        backend=backend,
        source=str(source),
        dest_root=str(dest_root),
        read=lambda path: Path(path).read_bytes(),
        exists=lambda path: Path(path).exists(),
        make_dir=lambda path: Path(path).mkdir(parents=True),
    )


def _plain_run(
    args: List[str],
    *,
    cwd: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    poll_interval: float = SHORT_POLL_SECONDS,
    timeout: Optional[float] = None,
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _require_visible(path: str) -> None:
    if not Path(path).exists():
        raise RuntimeError(f"{path} is not visible on this node")


def plain_clone_services() -> CloneServices:
    """Test services for a backend run outside the core: processes run to
    completion (no cancellation), nothing is redacted, and the visibility
    barrier only checks that the path exists (no NFS here)."""
    return CloneServices(
        run_cancellable=_plain_run,
        redact=str,
        visibility_waiter=_require_visible,
    )


class CloneBackendContract:
    """The clone seam's contract. Subclass and provide ``clone_subject``."""

    @pytest.fixture
    def clone_subject(self, tmp_path: Path) -> CloneSubject:
        raise NotImplementedError("provide the backend under test")

    @staticmethod
    def _dest(subject: CloneSubject, name: str) -> str:
        return f"{subject.dest_root.rstrip('/')}/{name}"

    def test_satisfies_the_protocol(self, clone_subject: CloneSubject) -> None:
        assert isinstance(clone_subject.backend, CloneBackend)

    def test_a_clone_at_a_path_holds_the_source_files(
        self, clone_subject: CloneSubject
    ) -> None:
        dest = self._dest(clone_subject, "copy")
        returned = clone_subject.backend.create_clone_at_path(
            clone_subject.source, dest
        )
        assert returned == dest
        for rel, data in _FILES.items():
            assert clone_subject.read(f"{dest}/{rel}") == data

    def test_an_existing_directory_destination_is_refused_untouched(
        self, clone_subject: CloneSubject
    ) -> None:
        dest = self._dest(clone_subject, "taken")
        clone_subject.make_dir(dest)
        with pytest.raises(Exception):
            clone_subject.backend.create_clone_at_path(clone_subject.source, dest)
        assert clone_subject.exists(dest)
        assert not clone_subject.exists(f"{dest}/README.md")
        assert not clone_subject.exists(f"{dest}/source-repo")

    def test_delete_removes_a_clone_and_is_idempotent(
        self, clone_subject: CloneSubject
    ) -> None:
        dest = self._dest(clone_subject, "doomed")
        clone_subject.backend.create_clone_at_path(clone_subject.source, dest)
        assert clone_subject.backend.delete_clone(dest) is True
        assert not clone_subject.exists(dest)
        assert clone_subject.backend.delete_clone(dest) is True

    def test_the_snapshot_registry_agrees_with_itself(
        self, clone_subject: CloneSubject
    ) -> None:
        backend = clone_subject.backend
        assert backend.clone_exists("example-ns", "v_1") is False
        assert backend.list_clones("example-ns") == []
        path = backend.create_clone(clone_subject.source, "example-ns", "v_1")
        assert clone_subject.read(f"{path}/README.md") == _FILES["README.md"]
        assert backend.clone_exists("example-ns", "v_1") is True
        listed = backend.list_clones("example-ns")
        assert [entry["name"] for entry in listed] == ["v_1"]
        assert backend.delete_clone(path) is True
        assert backend.clone_exists("example-ns", "v_1") is False

    def test_an_unsafe_identifier_is_refused(self, clone_subject: CloneSubject) -> None:
        if not clone_subject.validates_identifiers:
            pytest.skip("this backend sanitises identifiers instead")
        with pytest.raises(ValueError):
            clone_subject.backend.create_clone(clone_subject.source, "ns/evil", "v_1")
