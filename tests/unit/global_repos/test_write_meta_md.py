"""
Unit tests for write_meta_md — Story #876.

Tests:
  1. already_locked=False acquires and releases the cidx-meta write lock exactly once
  2. Lock acquisition failure (returns False) raises LifecycleLockUnavailableError,
     no file written, release_write_lock NOT called
  3. already_locked=True skips lock acquisition entirely
  4. Atomic write: tempfile.mkstemp called with dir=cidx-meta/, os.rename called
     to move temp to cidx-meta/<alias>.md
  5. YAML frontmatter contains lifecycle + lifecycle_schema_version fields
  6. write_meta_md does NOT call cidx_meta_refresh_debouncer.signal_dirty
  7. Golden repo clone and .versioned/ directories are NOT touched
"""

import os
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
import yaml

from code_indexer.global_repos.lifecycle_batch_runner import (
    LifecycleLockUnavailableError,
    write_meta_md,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def golden_repos_dir(tmp_path: Path) -> Path:
    """Create a temp golden_repos_dir with cidx-meta subdirectory."""
    (tmp_path / "cidx-meta").mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def mock_scheduler_ok() -> MagicMock:
    """Scheduler whose acquire_write_lock returns True (lock available)."""
    scheduler = MagicMock()
    scheduler.acquire_write_lock.return_value = True
    return scheduler


@pytest.fixture()
def mock_scheduler_locked() -> MagicMock:
    """Scheduler whose acquire_write_lock returns False (lock held)."""
    scheduler = MagicMock()
    scheduler.acquire_write_lock.return_value = False
    return scheduler


LIFECYCLE_FM: Dict[str, Any] = {
    "lifecycle": {
        "ci_system": "github-actions",
        "deployment_target": "kubernetes",
        "language_ecosystem": "python/poetry",
        "build_system": "poetry",
        "testing_framework": "pytest",
        "confidence": "high",
    },
    "lifecycle_schema_version": 1,
}

DESCRIPTION = "A Python service for semantic code search."


# ---------------------------------------------------------------------------
# 1. already_locked=False acquires and releases lock exactly once
# ---------------------------------------------------------------------------


def test_write_meta_md_acquires_and_releases_lock_exactly_once(
    golden_repos_dir: Path, mock_scheduler_ok: MagicMock
) -> None:
    """already_locked=False: acquire_write_lock called once, release_write_lock called once."""
    write_meta_md(
        alias="foo-global",
        description_body=DESCRIPTION,
        lifecycle_frontmatter=LIFECYCLE_FM,
        already_locked=False,
        refresh_scheduler=mock_scheduler_ok,
        golden_repos_dir=golden_repos_dir,
    )

    mock_scheduler_ok.acquire_write_lock.assert_called_once_with(
        "cidx-meta", owner_name="lifecycle_writer"
    )
    mock_scheduler_ok.release_write_lock.assert_called_once_with(
        "cidx-meta", owner_name="lifecycle_writer"
    )


# ---------------------------------------------------------------------------
# 2. Lock acquisition failure raises LifecycleLockUnavailableError, no file, no release
# ---------------------------------------------------------------------------


def test_write_meta_md_lock_failure_raises_and_does_not_write(
    golden_repos_dir: Path, mock_scheduler_locked: MagicMock
) -> None:
    """acquire_write_lock returns False -> LifecycleLockUnavailableError, no file, no release."""
    meta_path = golden_repos_dir / "cidx-meta" / "foo-global.md"

    with pytest.raises(LifecycleLockUnavailableError):
        write_meta_md(
            alias="foo-global",
            description_body=DESCRIPTION,
            lifecycle_frontmatter=LIFECYCLE_FM,
            already_locked=False,
            refresh_scheduler=mock_scheduler_locked,
            golden_repos_dir=golden_repos_dir,
        )

    assert not meta_path.exists(), (
        "No file should be written when lock acquisition fails"
    )
    mock_scheduler_locked.release_write_lock.assert_not_called()


# ---------------------------------------------------------------------------
# 3. already_locked=True skips lock acquisition entirely
# ---------------------------------------------------------------------------


def test_write_meta_md_already_locked_skips_acquisition(
    golden_repos_dir: Path, mock_scheduler_ok: MagicMock
) -> None:
    """already_locked=True: neither acquire_write_lock nor release_write_lock is called."""
    write_meta_md(
        alias="foo-global",
        description_body=DESCRIPTION,
        lifecycle_frontmatter=LIFECYCLE_FM,
        already_locked=True,
        refresh_scheduler=mock_scheduler_ok,
        golden_repos_dir=golden_repos_dir,
    )

    mock_scheduler_ok.acquire_write_lock.assert_not_called()
    mock_scheduler_ok.release_write_lock.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Atomic write: mkstemp dir=cidx-meta/, os.rename to final <alias>.md
# ---------------------------------------------------------------------------


def test_write_meta_md_uses_atomic_rename(
    golden_repos_dir: Path, mock_scheduler_ok: MagicMock
) -> None:
    """
    Atomic write contract:
      - tempfile.mkstemp must be called with dir=<cidx-meta dir>
      - os.rename must be called with the final destination = cidx-meta/<alias>.md
    """
    cidx_meta_dir = golden_repos_dir / "cidx-meta"
    final_path = str(cidx_meta_dir / "foo-global.md")

    mkstemp_calls: list = []
    rename_calls: list = []

    real_mkstemp = tempfile.mkstemp
    real_rename = os.rename

    def capturing_mkstemp(**kwargs: Any) -> Any:
        mkstemp_calls.append(kwargs)
        return real_mkstemp(**kwargs)

    def capturing_rename(src: str, dst: str) -> None:
        rename_calls.append((src, dst))
        real_rename(src, dst)

    with (
        patch(
            "code_indexer.global_repos.lifecycle_batch_runner.tempfile.mkstemp",
            side_effect=capturing_mkstemp,
        ),
        patch(
            "code_indexer.global_repos.lifecycle_batch_runner.os.rename",
            side_effect=capturing_rename,
        ),
    ):
        write_meta_md(
            alias="foo-global",
            description_body=DESCRIPTION,
            lifecycle_frontmatter=LIFECYCLE_FM,
            already_locked=False,
            refresh_scheduler=mock_scheduler_ok,
            golden_repos_dir=golden_repos_dir,
        )

    assert len(mkstemp_calls) == 1, "Exactly one mkstemp call expected"
    mkstemp_dir = mkstemp_calls[0].get("dir")
    assert mkstemp_dir == str(cidx_meta_dir), (
        f"mkstemp must use dir=cidx-meta/, got: {mkstemp_dir}"
    )

    assert len(rename_calls) == 1, "Exactly one os.rename call expected"
    _, dst_path = rename_calls[0]
    assert dst_path == final_path, (
        f"os.rename destination should be {final_path}, got: {dst_path}"
    )


# ---------------------------------------------------------------------------
# 5. YAML frontmatter contains lifecycle + lifecycle_schema_version
# ---------------------------------------------------------------------------


def test_write_meta_md_frontmatter_contains_lifecycle_fields(
    golden_repos_dir: Path, mock_scheduler_ok: MagicMock
) -> None:
    """Written file has YAML frontmatter with lifecycle and lifecycle_schema_version."""
    write_meta_md(
        alias="repo-beta-global",
        description_body=DESCRIPTION,
        lifecycle_frontmatter=LIFECYCLE_FM,
        already_locked=False,
        refresh_scheduler=mock_scheduler_ok,
        golden_repos_dir=golden_repos_dir,
    )

    content = (golden_repos_dir / "cidx-meta" / "repo-beta-global.md").read_text()
    assert content.startswith("---\n"), (
        "File must start with YAML frontmatter delimiter"
    )
    parts = content.split("---\n", maxsplit=2)
    assert len(parts) >= 3, "File must have opening and closing frontmatter delimiters"
    fm = yaml.safe_load(parts[1])
    assert fm.get("lifecycle_schema_version") == 1
    assert fm.get("lifecycle", {}).get("confidence") == "high"
    assert fm.get("lifecycle", {}).get("ci_system") == "github-actions"


# ---------------------------------------------------------------------------
# 6. write_meta_md does NOT call signal_dirty
# ---------------------------------------------------------------------------


def test_write_meta_md_does_not_call_signal_dirty(
    golden_repos_dir: Path, mock_scheduler_ok: MagicMock
) -> None:
    """Debouncer signalling is the caller's responsibility; write_meta_md must not call it."""
    with patch(
        "code_indexer.global_repos.meta_description_hook.CidxMetaRefreshDebouncer.signal_dirty"
    ) as mock_signal:
        write_meta_md(
            alias="foo-global",
            description_body=DESCRIPTION,
            lifecycle_frontmatter=LIFECYCLE_FM,
            already_locked=False,
            refresh_scheduler=mock_scheduler_ok,
            golden_repos_dir=golden_repos_dir,
        )
    mock_signal.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Golden repo clone and .versioned/ are NOT touched
# ---------------------------------------------------------------------------


def test_write_meta_md_does_not_touch_golden_repo_clone(
    golden_repos_dir: Path, mock_scheduler_ok: MagicMock
) -> None:
    """Golden repo clone and .versioned/ directories must not be modified."""
    clone_dir = golden_repos_dir / "foo-global"
    clone_dir.mkdir()
    versioned_dir = golden_repos_dir / ".versioned" / "foo-global"
    versioned_dir.mkdir(parents=True)
    sentinel = clone_dir / "sentinel.txt"
    sentinel.write_text("untouched")
    versioned_sentinel = versioned_dir / "v_sentinel.txt"
    versioned_sentinel.write_text("untouched versioned")

    write_meta_md(
        alias="foo-global",
        description_body=DESCRIPTION,
        lifecycle_frontmatter=LIFECYCLE_FM,
        already_locked=False,
        refresh_scheduler=mock_scheduler_ok,
        golden_repos_dir=golden_repos_dir,
    )

    assert sentinel.read_text() == "untouched", "Clone directory must not be modified"
    assert versioned_sentinel.read_text() == "untouched versioned", (
        ".versioned/ must not be modified"
    )
    new_md_files = list(golden_repos_dir.glob("**/*.md"))
    assert all("cidx-meta" in str(f) for f in new_md_files), (
        "Only cidx-meta .md files should be written"
    )
