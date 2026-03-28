"""
Unit tests for Story #554: Research Assistant Security Hardening - Cleanup Script.

Tests verify the cidx-meta-cleanup.sh script enforces path validation:
- Allows deletion inside cidx-meta directory
- Refuses deletion outside cidx-meta
- Refuses deletion of cidx-meta root itself
- Refuses nonexistent paths
- Blocks path traversal attacks
- Fails when CIDX_META_BASE not configured

Acceptance Criteria covered:
- AC4: cidx-meta-cleanup.sh path validation

Following TDD methodology: Tests written FIRST before implementing.
"""

import os
import subprocess
import pytest
from pathlib import Path

# Resolve script path relative to this test file.
# Path: tests/unit/server/services/ -> scripts/
_TESTS_DIR = Path(__file__).parent
_PROJECT_ROOT = _TESTS_DIR.parents[3]  # services -> server -> unit -> tests -> root
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "cidx-meta-cleanup.sh"


@pytest.fixture
def temp_cidx_meta(tmp_path):
    """
    Create a temporary cidx-meta directory structure for testing.

    Layout:
      tmp_path/
        golden-repos/
          cidx-meta/               <- cidx-meta base
            repo-description.md    <- file inside (safe to delete)
            dependency-map/
              some-dep.md          <- nested file inside (safe to delete)
        important.py               <- file OUTSIDE cidx-meta (must not be deleted)
    """
    cidx_meta = tmp_path / "golden-repos" / "cidx-meta"
    cidx_meta.mkdir(parents=True)
    (cidx_meta / "repo-description.md").write_text("# Test repo description")
    dep_map = cidx_meta / "dependency-map"
    dep_map.mkdir()
    (dep_map / "some-dep.md").write_text("# Dep map")

    outside_file = tmp_path / "important.py"
    outside_file.write_text("# Source code - must not be deleted")

    return tmp_path, cidx_meta


def _run_script(target_path, cidx_meta_base=None, env_override=None):
    """
    Run cidx-meta-cleanup.sh with given target_path.
    If cidx_meta_base is provided, sets CIDX_META_BASE in env.
    If env_override is provided, uses that env dict directly.
    Returns CompletedProcess.
    """
    if env_override is not None:
        env = env_override
    else:
        env = dict(os.environ)
        if cidx_meta_base is not None:
            env["CIDX_META_BASE"] = str(cidx_meta_base)
        else:
            env.pop("CIDX_META_BASE", None)

    return subprocess.run(
        [str(_SCRIPT_PATH), str(target_path)],
        capture_output=True,
        text=True,
        env=env,
    )


class TestCleanupScriptExists:
    """Pre-condition: the script must exist and be executable."""

    def test_script_exists(self):
        """AC4: cidx-meta-cleanup.sh must exist at scripts/cidx-meta-cleanup.sh."""
        assert _SCRIPT_PATH.exists(), (
            f"cidx-meta-cleanup.sh must exist at {_SCRIPT_PATH}. "
            "Create it as part of Story #554."
        )

    def test_script_is_executable(self):
        """AC4: cidx-meta-cleanup.sh must be executable (chmod +x)."""
        assert os.access(str(_SCRIPT_PATH), os.X_OK), (
            f"cidx-meta-cleanup.sh must be executable. Run: chmod +x {_SCRIPT_PATH}"
        )


class TestCleanupScriptAllowedDeletions:
    """AC4: Script deletes files that are inside cidx-meta."""

    def test_deletes_file_inside_cidx_meta(self, temp_cidx_meta):
        """AC4: File directly inside cidx-meta is deleted with exit code 0."""
        tmp_path, cidx_meta = temp_cidx_meta
        target = cidx_meta / "repo-description.md"
        assert target.exists(), "Test setup: file must exist before deletion"

        result = _run_script(target, cidx_meta_base=cidx_meta)

        assert result.returncode == 0, (
            f"Script must exit 0 when deleting inside cidx-meta. "
            f"stdout: {result.stdout!r}, stderr: {result.stderr!r}"
        )
        assert not target.exists(), (
            "Target file must be deleted after successful invocation"
        )

    def test_deletes_nested_file_inside_cidx_meta(self, temp_cidx_meta):
        """AC4: Nested file inside cidx-meta subdirectory is deleted with exit code 0."""
        tmp_path, cidx_meta = temp_cidx_meta
        target = cidx_meta / "dependency-map" / "some-dep.md"
        assert target.exists()

        result = _run_script(target, cidx_meta_base=cidx_meta)

        assert result.returncode == 0, (
            f"Must succeed for nested path. "
            f"stdout: {result.stdout!r}, stderr: {result.stderr!r}"
        )
        assert not target.exists(), "Nested file must be deleted"


class TestCleanupScriptBlockedDeletions:
    """AC4: Script refuses to delete paths outside cidx-meta."""

    def test_refuses_file_outside_cidx_meta(self, temp_cidx_meta):
        """AC4: File outside cidx-meta causes exit code 1, file not deleted."""
        tmp_path, cidx_meta = temp_cidx_meta
        outside_file = tmp_path / "important.py"
        assert outside_file.exists()

        result = _run_script(outside_file, cidx_meta_base=cidx_meta)

        assert result.returncode == 1, (
            f"Script must exit 1 for paths outside cidx-meta. "
            f"stdout: {result.stdout!r}, stderr: {result.stderr!r}"
        )
        assert outside_file.exists(), "File outside cidx-meta must NOT be deleted"

    def test_refuses_etc_passwd(self, temp_cidx_meta):
        """AC4: Script refuses /etc/passwd regardless of cidx-meta location."""
        tmp_path, cidx_meta = temp_cidx_meta

        result = _run_script("/etc/passwd", cidx_meta_base=cidx_meta)

        assert result.returncode == 1, (
            f"Script must refuse /etc/passwd. "
            f"stdout: {result.stdout!r}, stderr: {result.stderr!r}"
        )

    def test_refuses_cidx_meta_root_itself(self, temp_cidx_meta):
        """AC4: Script refuses to delete cidx-meta root directory itself."""
        tmp_path, cidx_meta = temp_cidx_meta

        result = _run_script(cidx_meta, cidx_meta_base=cidx_meta)

        assert result.returncode == 1, (
            f"Script must refuse to delete cidx-meta root. "
            f"stdout: {result.stdout!r}, stderr: {result.stderr!r}"
        )
        assert cidx_meta.exists(), "cidx-meta root must NOT be deleted"

    def test_refuses_nonexistent_path_inside_cidx_meta(self, temp_cidx_meta):
        """AC4: Script exits 1 when target path does not exist (even if inside cidx-meta)."""
        tmp_path, cidx_meta = temp_cidx_meta
        nonexistent = cidx_meta / "does-not-exist.md"
        assert not nonexistent.exists()

        result = _run_script(nonexistent, cidx_meta_base=cidx_meta)

        assert result.returncode == 1, (
            f"Script must exit 1 for nonexistent path. "
            f"stdout: {result.stdout!r}, stderr: {result.stderr!r}"
        )

    def test_refuses_missing_cidx_meta_base(self, tmp_path):
        """AC4: Script exits 1 when CIDX_META_BASE env var is not set."""
        test_file = tmp_path / "test.md"
        test_file.write_text("test")

        env = {k: v for k, v in os.environ.items() if k != "CIDX_META_BASE"}
        result = _run_script(test_file, env_override=env)

        assert result.returncode == 1, (
            f"Script must exit 1 when CIDX_META_BASE not configured. "
            f"stdout: {result.stdout!r}, stderr: {result.stderr!r}"
        )

    def test_blocks_path_traversal_attack(self, temp_cidx_meta):
        """
        AC4: Path traversal attacks (e.g., /cidx-meta/../../../etc/passwd)
        must be blocked by canonical path resolution (readlink -f).
        """
        tmp_path, cidx_meta = temp_cidx_meta
        traversal_path = str(cidx_meta) + "/../../../etc/passwd"

        result = _run_script(traversal_path, cidx_meta_base=cidx_meta)

        assert result.returncode == 1, (
            f"Path traversal attack must be blocked (exit 1). "
            f"stdout: {result.stdout!r}, stderr: {result.stderr!r}"
        )

    def test_refuses_directory_target_inside_cidx_meta(self, temp_cidx_meta):
        """
        MEDIUM-3: Script must refuse to delete a directory target, even if it is
        inside cidx-meta. Only regular files should be deleted to prevent accidental
        subtree deletion via rm -f on a directory (which no-ops) or future code changes.
        """
        tmp_path, cidx_meta = temp_cidx_meta
        # dependency-map is a directory inside cidx-meta
        directory_target = cidx_meta / "dependency-map"
        assert directory_target.is_dir(), (
            "Test setup: dependency-map must be a directory"
        )

        result = _run_script(directory_target, cidx_meta_base=cidx_meta)

        assert result.returncode == 1, (
            f"MEDIUM-3: Script must exit 1 when target is a directory, not a regular file. "
            f"stdout: {result.stdout!r}, stderr: {result.stderr!r}"
        )
        assert directory_target.exists(), (
            "Directory inside cidx-meta must NOT be deleted by the cleanup script"
        )
