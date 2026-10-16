"""
Tests for create_clone_at_path: it must
refuse an already-existing DIRECTORY destination instead of letting
`cp --reflink=auto -a src existing_dir` silently merge src INTO existing_dir
as a subdirectory and exit 0.

Scope note (from checking every real caller before writing this fix):
- ActivatedRepoManager._clone_with_copy_on_write (the activation
  path) and RefreshScheduler._restore_master_from_versioned both call
  create_clone_at_path with a destination verified ABSENT beforehand -- an
  existing destination there is always the traversal bug, never legitimate.
<<<<<<< HEAD
- refresh_integrity_gate.py's restore_chunks_db_via_reflink /
  restore_metadata_files_via_reflink intentionally clone ONTO an existing
  REGULAR FILE (overwrite-in-place self-heal) -- `cp -a src existing_file`
  is ordinary, safe cp semantics (overwrite), not a merge, so this refusal
  is scoped to an existing DIRECTORY destination only, never an existing file.

Foundation #1 compliant: real filesystem, real `cp` subprocess via
LocalCloneBackend -- no mocking of the refusal logic itself.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
=======
- refresh_integrity_gate.py restores single files (chunks.db,
  metadata*.json) with its own atomic copy, never through a clone backend
  (Bug #2157). LocalCloneBackend keeps ordinary `cp -a` file-onto-file
  overwrite semantics; the refusal there is scoped to an existing DIRECTORY.
- CowDaemonBackend (Bug #2157): the daemon removes a failed job's
  destination, so the backend never starts a job for a file source or for
  ANY pre-existing destination -- the daemon can only ever remove a path
  its own job created.

Foundation #1 compliant: real filesystem, real `cp` subprocess via
LocalCloneBackend -- no mocking of the refusal logic itself. The daemon's
HTTP API is a recording double (no daemon exists locally).
"""

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.storage.shared.clone_backend import (
    CloneDestinationExistsError,
    CowDaemonBackend,
    LocalCloneBackend,
)
from code_indexer.server.utils.config_manager import CowDaemonConfig
>>>>>>> upstream/master


@pytest.fixture
def backend():
    return LocalCloneBackend()


class TestCreateCloneAtPathRefusesExistingDirectory:
    def test_refuses_existing_directory_destination_and_copies_nothing(self, backend):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "payload.txt").write_text("attacker-controlled content\n")

            # An already-existing directory at the destination -- exactly
            # the shape a traversed `activated_repos_dir/../golden-repos`
            # destination has (the pre-existing shared golden-repos tree).
            dest = Path(tmp) / "dest"
            dest.mkdir()
            (dest / "legitimate_existing_file.txt").write_text("do not touch\n")

            with pytest.raises(FileExistsError):
                backend.create_clone_at_path(str(source), str(dest))

            # Nothing from source was merged into dest.
            assert not (dest / "payload.txt").exists()
            assert not (dest / "source").exists()
            # The pre-existing legitimate content is untouched.
            assert (dest / "legitimate_existing_file.txt").read_text() == (
                "do not touch\n"
            )

    def test_succeeds_when_destination_absent(self, backend):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "file.txt").write_text("hello\n")

            dest = Path(tmp) / "dest"

            result = backend.create_clone_at_path(str(source), str(dest))

            assert result == str(dest)
            assert (dest / "file.txt").read_text() == "hello\n"

    def test_allows_overwrite_of_existing_regular_file_destination(self, backend):
<<<<<<< HEAD
        """The refresh_integrity_gate.py restore call sites clone a healthy
        chunks.db/metadata*.json ONTO an existing corrupt/stale regular
        file -- ordinary cp overwrite-in-place semantics, not a merge, and
        must remain allowed."""
=======
        """The refusal is scoped to an existing DIRECTORY: cloning a file
        ONTO an existing regular file keeps ordinary cp overwrite-in-place
        semantics, not a merge."""
>>>>>>> upstream/master
        with tempfile.TemporaryDirectory() as tmp:
            source_file = Path(tmp) / "healthy.db"
            source_file.write_text("healthy content\n")

            dest_file = Path(tmp) / "corrupt.db"
            dest_file.write_text("corrupt content\n")

            result = backend.create_clone_at_path(str(source_file), str(dest_file))

            assert result == str(dest_file)
            assert dest_file.read_text() == "healthy content\n"

    def test_real_cp_would_have_merged_without_the_fix(self):
        """Sanity check of the underlying cp behaviour itself: plain
        `cp --reflink=auto -a` on an existing directory destination
        silently merges and exits 0 -- this is NOT a test of production
        code, it documents why the refusal in create_clone_at_path is
        necessary at all."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "payload.txt").write_text("x\n")
            dest = Path(tmp) / "dest"
            dest.mkdir()

            result = subprocess.run(
                ["cp", "--reflink=auto", "-a", str(source), str(dest)],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
            assert (dest / "source" / "payload.txt").exists()
<<<<<<< HEAD
=======


class _RecordingDaemonApi:
    """Stands in for the ``requests`` module the daemon backend calls.
    Records every request; any job it is asked to start would complete."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(("POST", url))
        raise AssertionError(f"no daemon job may be started: POST {url}")

    def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(("GET", url))
        raise AssertionError(f"unexpected daemon poll: GET {url}")


def _daemon_backend(mount: Path) -> CowDaemonBackend:
    return CowDaemonBackend(
        config=CowDaemonConfig(
            daemon_url="http://daemon.example.com:8081",
            api_key="test-api-key",
            mount_point=str(mount),
            daemon_storage_path=str(mount),
        ),
        visibility_waiter=lambda _path: None,
    )


class TestCowDaemonBackendTouchesOnlyWhatItCreates:
    def test_file_source_refused_before_any_daemon_job(self, tmp_path: Path):
        source_file = tmp_path / "v_1" / "chunks.db"
        source_file.parent.mkdir()
        source_file.write_bytes(b"snapshot store")
        dest = tmp_path / "repo" / "chunks.db"
        dest.parent.mkdir()
        api = _RecordingDaemonApi()

        with patch.dict(sys.modules, {"requests": api}):
            with pytest.raises(NotADirectoryError):
                _daemon_backend(tmp_path).create_clone_at_path(
                    str(source_file), str(dest)
                )

        assert api.calls == []
        assert not dest.exists()

    def test_existing_file_destination_refused_before_any_daemon_job(
        self, tmp_path: Path
    ):
        source_dir = tmp_path / "v_1"
        source_dir.mkdir()
        (source_dir / "file.txt").write_text("snapshot\n")
        dest = tmp_path / "repo"
        dest.write_bytes(b"pre-existing regular file")
        api = _RecordingDaemonApi()

        with patch.dict(sys.modules, {"requests": api}):
            with pytest.raises(CloneDestinationExistsError):
                _daemon_backend(tmp_path).create_clone_at_path(
                    str(source_dir), str(dest)
                )

        assert api.calls == []
        assert dest.read_bytes() == b"pre-existing regular file"
>>>>>>> upstream/master
