"""
Tests for Issue #1511: CoW-daemon snapshot creation must defensively ensure
the source tree is group+other readable/traversable BEFORE handing it off to
a remote-executing clone backend (the CoW daemon), since the daemon may run
as a different OS user than the process that wrote the collection files.
"""

import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Tuple

from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class CowDaemonBackend:
    """Test double whose class name matches the production gating check
    (``type(self._clone_backend).__name__ == "CowDaemonBackend"``).

    Deliberately does NOT touch the real filesystem beyond recording the
    call and returning a fake path. Also snapshots the mode bits of every
    entry under ``source_path`` AT CALL TIME, so tests can prove the
    preflight ran BEFORE create_clone was invoked -- not merely that modes
    happen to be correct after create_snapshot returns.
    """

    def __init__(self, versioned_base: str) -> None:
        # Presence of _versioned_base engages the collision-retry-loop path
        # in _create_clone_backend_snapshot, matching production shape.
        self._versioned_base = versioned_base
        self.calls: List[Tuple[str, str, str]] = []
        self.modes_at_call_time: List[Dict[Path, int]] = []

    def create_clone(self, source_path: str, alias: str, version_name: str) -> str:
        self.calls.append((source_path, alias, version_name))
        snapshot: Dict[Path, int] = {}
        src = Path(source_path)
        if src.exists():
            snapshot[src] = _mode(src)
            for entry in src.rglob("*"):
                snapshot[entry] = _mode(entry)
        self.modes_at_call_time.append(snapshot)
        return f"{self._versioned_base}/.versioned/{alias}/{version_name}"


class LocalCloneBackend:
    """Test double for the non-CowDaemon path -- must NOT trigger chmod."""

    def __init__(self, versioned_base: str) -> None:
        self._versioned_base = versioned_base
        self.calls: List[Tuple[str, str, str]] = []

    def create_clone(self, source_path: str, alias: str, version_name: str) -> str:
        self.calls.append((source_path, alias, version_name))
        return f"{self._versioned_base}/.versioned/{alias}/{version_name}"


def _build_source_tree(tmp_path: Path) -> Path:
    """Build a small file/dir tree with restrictive permissions, simulating
    files written by the code-indexer OS user with a restrictive umask."""
    source_dir = tmp_path / "source_repo"
    sub_dir = source_dir / "index"
    sub_dir.mkdir(parents=True)

    file1 = source_dir / "collection_meta.json"
    file1.write_text("{}")
    file2 = sub_dir / "hnsw_index.bin"
    file2.write_text("binary-data")

    # Restrictive modes mirroring the bug report: files 600, dirs 700.
    os.chmod(file1, 0o600)
    os.chmod(file2, 0o600)
    os.chmod(sub_dir, 0o700)
    os.chmod(source_dir, 0o700)

    return source_dir


class TestCowDaemonPermissionPreflight:
    def test_chmod_applied_before_create_clone_for_cow_daemon_backend(
        self, tmp_path: Path
    ) -> None:
        """RED: production code does not yet call the preflight helper, so
        the modes captured AT create_clone call time remain restrictive."""
        source_dir = _build_source_tree(tmp_path)
        versioned_base = str(tmp_path / "versioned_base")
        backend = CowDaemonBackend(versioned_base)
        manager = VersionedSnapshotManager(clone_backend=backend)

        result = manager.create_snapshot("myrepo", str(source_dir))

        assert backend.calls, "create_clone should have been invoked"
        assert result

        assert len(backend.modes_at_call_time) == 1
        modes_at_call = backend.modes_at_call_time[0]
        assert modes_at_call, "expected non-empty snapshot of source tree"

        for path, mode in modes_at_call.items():
            if path.is_dir():
                assert mode & 0o055 == 0o055, f"dir {path} mode {oct(mode)}"
            else:
                assert mode & 0o044 == 0o044, f"file {path} mode {oct(mode)}"

    def test_no_chmod_for_non_cow_daemon_backend(self, tmp_path: Path) -> None:
        """Gating must be name-based: LocalCloneBackend must NOT be touched."""
        source_dir = _build_source_tree(tmp_path)
        versioned_base = str(tmp_path / "versioned_base")
        backend = LocalCloneBackend(versioned_base)
        manager = VersionedSnapshotManager(clone_backend=backend)

        original_modes = {p: _mode(p) for p in source_dir.rglob("*")}
        original_modes[source_dir] = _mode(source_dir)

        result = manager.create_snapshot("myrepo", str(source_dir))

        assert backend.calls, "create_clone should have been invoked"
        assert result, "create_snapshot should return the backend's clone path"
        for path, orig_mode in original_modes.items():
            assert _mode(path) == orig_mode, f"{path} mode changed unexpectedly"

    def test_chmod_failure_on_one_entry_does_not_abort_snapshot(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """A failing `find`/`chmod` invocation during the preflight must be
        logged and swallowed -- create_snapshot must still complete and
        still call create_clone."""
        source_dir = _build_source_tree(tmp_path)
        versioned_base = str(tmp_path / "versioned_base")
        backend = CowDaemonBackend(versioned_base)
        manager = VersionedSnapshotManager(clone_backend=backend)

        import subprocess as subprocess_module
        from code_indexer.server.storage.shared import snapshot_manager

        real_run = subprocess_module.run

        def flaky_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
            result = real_run(cmd, *args, **kwargs)
            # Simulate one failing entry within the batch: chmod ran but
            # reported a non-zero exit for at least one path.
            return subprocess_module.CompletedProcess(
                cmd,
                returncode=1,
                stdout=result.stdout,
                stderr="simulated permission failure",
            )

        monkeypatch.setattr(snapshot_manager.subprocess, "run", flaky_run)

        # Must not raise despite the injected failure.
        result = manager.create_snapshot("myrepo", str(source_dir))

        assert backend.calls, "create_clone should still have been invoked"
        assert result
