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
        """The refresh_integrity_gate.py restore call sites clone a healthy
        chunks.db/metadata*.json ONTO an existing corrupt/stale regular
        file -- ordinary cp overwrite-in-place semantics, not a merge, and
        must remain allowed."""
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
