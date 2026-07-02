"""
Tests for Bug #940 — lifecycle writer must preserve pre-existing frontmatter
keys (last_analyzed, name, url, technologies, purpose) when writing lifecycle
data to cidx-meta/<alias>.md.

Tests:
  1. test_existing_frontmatter_keys_preserved_after_lifecycle_write
     - Pre-create .md with last_analyzed, name, url, technologies, purpose + body.
     - Run _process_one_repo. Assert all original keys + new lifecycle/version
       are present in the written file.  Assert body is replaced by new description.
  2. test_new_file_writes_only_lifecycle_keys
     - .md does not exist before call. Assert file is created with ONLY lifecycle
       and lifecycle_schema_version keys (today's behavior preserved).
  3. test_lifecycle_keys_overwrite_when_pre_existing
     - Pre-create .md with stale lifecycle block + last_analyzed.
     - Run _process_one_repo. Assert lifecycle is the NEW one, not the stale one.
     - Assert last_analyzed is still present.
  4. test_merge_frontmatter_is_superset_and_new_wins
     - Directly call _merge_frontmatter with overlapping keys; verify the merged
       result is a superset of both input dicts with new values winning on
       collision (the core merge contract).
     - Also verify that merging an existing dict with a new dict that drops no
       keys produces the correct superset (invariant: no key silently dropped).
  5. test_corrupt_frontmatter_raises_not_silently_overwrites
     - Pre-create .md with invalid/corrupt YAML frontmatter.
     - Assert that _process_one_repo raises (fail-closed per Messi Rule #13)
       rather than silently overwriting the corrupt file with lifecycle-only data.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import pytest
import yaml

from code_indexer.global_repos.lifecycle_batch_runner import (
    LifecycleBatchRunner,
    _merge_frontmatter,
)
from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body
from code_indexer.global_repos.unified_response_parser import (
    CURRENT_LIFECYCLE_SCHEMA_VERSION,
    UnifiedResult,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LIFECYCLE_NEW: Dict[str, Any] = {
    "ci_system": "github-actions",
    "deployment_target": "kubernetes",
    "language_ecosystem": "python/poetry",
    "build_system": "poetry",
    "testing_framework": "pytest",
    "confidence": "high",
}

_LIFECYCLE_STALE: Dict[str, Any] = {
    "ci_system": "jenkins",
    "deployment_target": "bare-metal",
    "language_ecosystem": "java/maven",
    "build_system": "maven",
    "testing_framework": "junit",
    "confidence": "low",
}

_NEW_DESCRIPTION = "Updated description after lifecycle backfill run."


def _make_valid_result() -> UnifiedResult:
    """Return a fresh UnifiedResult — avoids sharing mutable dicts across tests."""
    return UnifiedResult(
        description=_NEW_DESCRIPTION,
        lifecycle=dict(_LIFECYCLE_NEW),
    )


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubScheduler:
    """Always grants the lock; records acquire/release calls."""

    def __init__(self) -> None:
        self.acquire_calls: List[tuple] = []
        self.release_calls: List[tuple] = []

    def acquire_write_lock(self, key: str, owner_name: str) -> bool:
        self.acquire_calls.append((key, owner_name))
        return True

    def release_write_lock(self, key: str, owner_name: str) -> None:
        self.release_calls.append((key, owner_name))


class _StubJobTracker:
    def update_status(self, job_id: str, **kwargs: Any) -> None:
        pass

    def complete_job(self, job_id: str, result: Optional[Dict] = None) -> None:
        pass

    def fail_job(self, job_id: str, error: str) -> None:
        pass


class _StubDebouncer:
    def signal_dirty(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_runner(
    golden_repos_dir: Path,
    invoker_result: Optional[UnifiedResult] = None,
) -> LifecycleBatchRunner:
    """Build a LifecycleBatchRunner with stubs and the given invoker result."""
    result = invoker_result if invoker_result is not None else _make_valid_result()

    def _invoker(alias: str, repo_path: Path) -> UnifiedResult:
        return result

    return LifecycleBatchRunner(
        golden_repos_dir=golden_repos_dir,
        job_tracker=_StubJobTracker(),
        refresh_scheduler=_StubScheduler(),
        debouncer=_StubDebouncer(),
        claude_cli_invoker=_invoker,
        concurrency=1,
        sub_batch_size_override=10,
    )


def _write_md(path: Path, frontmatter: Dict[str, Any], body: str) -> None:
    """Write a YAML-frontmatter markdown file to path."""
    fm_yaml = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True)
    path.write_text(f"---\n{fm_yaml}---\n\n{body}\n", encoding="utf-8")


def _read_frontmatter(path: Path) -> Dict[str, Any]:
    """Read frontmatter dict from a written .md file using the real parser."""
    content = path.read_text(encoding="utf-8")
    frontmatter, _ = split_frontmatter_and_body(content)
    # split_frontmatter_and_body returns Tuple[Dict[str, Any], str] but mypy loses
    # precision through tuple unpacking; explicit cast restores the narrow type.
    return cast(Dict[str, Any], frontmatter)


def _read_body(path: Path) -> str:
    """Read the markdown body (after frontmatter) from a .md file."""
    content = path.read_text(encoding="utf-8")
    _, body = split_frontmatter_and_body(content)
    # split_frontmatter_and_body returns Tuple[Dict[str, Any], str] but mypy loses
    # precision through tuple unpacking; explicit cast restores the narrow type.
    return cast(str, body).strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def golden_repos_dir(tmp_path: Path) -> Path:
    """Create golden_repos_dir with cidx-meta subdirectory."""
    (tmp_path / "cidx-meta").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1: existing frontmatter keys preserved after lifecycle write
# ---------------------------------------------------------------------------


def test_existing_frontmatter_keys_preserved_after_lifecycle_write(
    golden_repos_dir: Path,
) -> None:
    """
    When cidx-meta/<alias>.md already has last_analyzed, name, url, technologies,
    purpose, plus an old description body, _process_one_repo must:
    - Preserve all original frontmatter keys in the written file.
    - Add the new 'lifecycle' and 'lifecycle_schema_version' keys.
    - Replace the body with the new description from UnifiedResult.
    """
    alias = "humanize"
    (golden_repos_dir / alias).mkdir()
    meta_path = golden_repos_dir / "cidx-meta" / f"{alias}.md"

    original_frontmatter: Dict[str, Any] = {
        "last_analyzed": "2026-04-22T18:12:00Z",
        "name": "humanize",
        "url": "https://github.com/python-humanize/humanize",
        "technologies": ["python", "locale"],
        "purpose": "Human-friendly string formatting library",
    }
    original_body = "Original description of the humanize library."
    _write_md(meta_path, original_frontmatter, original_body)

    runner = _make_runner(golden_repos_dir)
    runner._process_one_repo(alias, "job-940")

    written_fm = _read_frontmatter(meta_path)
    written_body = _read_body(meta_path)

    # All original keys must survive the lifecycle write.
    assert written_fm.get("last_analyzed") == "2026-04-22T18:12:00Z", (
        "last_analyzed must be preserved after lifecycle write"
    )
    assert written_fm.get("name") == "humanize", (
        "name must be preserved after lifecycle write"
    )
    assert written_fm.get("url") == "https://github.com/python-humanize/humanize", (
        "url must be preserved after lifecycle write"
    )
    assert written_fm.get("technologies") == ["python", "locale"], (
        "technologies must be preserved after lifecycle write"
    )
    assert written_fm.get("purpose") == "Human-friendly string formatting library", (
        "purpose must be preserved after lifecycle write"
    )

    # New lifecycle keys must be present.
    assert "lifecycle" in written_fm, "lifecycle key must be written"
    assert (
        written_fm.get("lifecycle_schema_version") == CURRENT_LIFECYCLE_SCHEMA_VERSION
    ), "lifecycle_schema_version must be set to current version"

    # Body must be replaced with the new description.
    assert written_body == _NEW_DESCRIPTION, (
        "Body must be replaced with the new description from UnifiedResult"
    )


# ---------------------------------------------------------------------------
# Test 2: new file writes only lifecycle keys (first-write behavior preserved)
# ---------------------------------------------------------------------------


def test_new_file_writes_only_lifecycle_keys(golden_repos_dir: Path) -> None:
    """
    When cidx-meta/<alias>.md does NOT exist before _process_one_repo runs,
    the file must be created with ONLY 'lifecycle' and 'lifecycle_schema_version'
    in the frontmatter — no extra keys invented.
    """
    alias = "new-repo"
    (golden_repos_dir / alias).mkdir()
    meta_path = golden_repos_dir / "cidx-meta" / f"{alias}.md"

    assert not meta_path.exists(), "Test pre-condition: file must not exist yet"

    runner = _make_runner(golden_repos_dir)
    runner._process_one_repo(alias, "job-940")

    assert meta_path.exists(), "File must be created after _process_one_repo"
    written_fm = _read_frontmatter(meta_path)

    # Only lifecycle keys should be present (today's behavior for new files).
    assert set(written_fm.keys()) == {"lifecycle", "lifecycle_schema_version"}, (
        f"New file must have only lifecycle and lifecycle_schema_version keys, "
        f"got: {set(written_fm.keys())}"
    )
    assert written_fm["lifecycle_schema_version"] == CURRENT_LIFECYCLE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Test 3: lifecycle keys overwrite stale lifecycle when pre-existing
# ---------------------------------------------------------------------------


def test_lifecycle_keys_overwrite_when_pre_existing(golden_repos_dir: Path) -> None:
    """
    When the pre-existing .md already has a stale 'lifecycle' block, the new
    lifecycle dict from UnifiedResult must WIN (lifecycle is source of truth
    for its own keys). All other keys (last_analyzed) must still be preserved.
    """
    alias = "shortuuid"
    (golden_repos_dir / alias).mkdir()
    meta_path = golden_repos_dir / "cidx-meta" / f"{alias}.md"

    stale_frontmatter: Dict[str, Any] = {
        "last_analyzed": "2026-01-01T00:00:00Z",
        "lifecycle": dict(_LIFECYCLE_STALE),
        "lifecycle_schema_version": 1,
    }
    _write_md(meta_path, stale_frontmatter, "Old description.")

    runner = _make_runner(golden_repos_dir, invoker_result=_make_valid_result())
    runner._process_one_repo(alias, "job-940")

    written_fm = _read_frontmatter(meta_path)

    # last_analyzed must survive.
    assert written_fm.get("last_analyzed") == "2026-01-01T00:00:00Z", (
        "last_analyzed must be preserved even when existing lifecycle is overwritten"
    )

    # New lifecycle must win over stale.
    written_lifecycle = written_fm.get("lifecycle")
    assert isinstance(written_lifecycle, dict), "lifecycle must be a dict"
    assert written_lifecycle.get("ci_system") == "github-actions", (
        "New lifecycle ci_system must overwrite stale jenkins value"
    )
    assert written_lifecycle.get("deployment_target") == "kubernetes", (
        "New lifecycle deployment_target must overwrite stale bare-metal value"
    )
    assert written_lifecycle.get("confidence") == "high", (
        "New lifecycle confidence must overwrite stale low value"
    )

    # Schema version must be updated to current.
    assert (
        written_fm.get("lifecycle_schema_version") == CURRENT_LIFECYCLE_SCHEMA_VERSION
    )


# ---------------------------------------------------------------------------
# Test 4: _merge_frontmatter produces superset with new values winning
# ---------------------------------------------------------------------------


def test_merge_frontmatter_is_superset_and_new_wins() -> None:
    """
    Direct unit test of _merge_frontmatter:
    - Result is always a superset of both input dicts (no key silently dropped).
    - New values win on collision (lifecycle keys are source of truth for themselves).
    - Keys present only in existing_fm are preserved unchanged.
    - Merging with an empty existing dict returns exactly the new dict.

    This test documents the merge contract independently of _process_one_repo,
    and also verifies the key-preservation invariant: for any existing key K,
    _merge_frontmatter must not silently drop it.  If the implementation ever
    drops a key, the superset assertion below will fail — providing the same
    safety as a post-write invariant check but at the unit level of the helper.
    """
    existing_fm: Dict[str, Any] = {
        "last_analyzed": "2026-04-22T18:12:00Z",
        "name": "humanize",
        "lifecycle": {"ci_system": "jenkins", "confidence": "low"},
        "lifecycle_schema_version": 1,
    }
    new_fm: Dict[str, Any] = {
        "lifecycle": {"ci_system": "github-actions", "confidence": "high"},
        "lifecycle_schema_version": CURRENT_LIFECYCLE_SCHEMA_VERSION,
    }

    merged = _merge_frontmatter(existing_fm, new_fm)

    # Keys from existing_fm that are not in new_fm must be preserved.
    assert merged.get("last_analyzed") == "2026-04-22T18:12:00Z", (
        "last_analyzed from existing must survive the merge"
    )
    assert merged.get("name") == "humanize", "name from existing must survive the merge"

    # New values must win on collision.
    assert merged.get("lifecycle_schema_version") == CURRENT_LIFECYCLE_SCHEMA_VERSION, (
        "new lifecycle_schema_version must overwrite old version"
    )
    merged_lifecycle = merged.get("lifecycle")
    assert isinstance(merged_lifecycle, dict), "lifecycle must be a dict after merge"
    assert merged_lifecycle.get("ci_system") == "github-actions", (
        "new ci_system must overwrite old jenkins value"
    )

    # Result must be a superset: no key from either side may be missing.
    expected_keys = existing_fm.keys() | new_fm.keys()
    assert set(merged.keys()) >= expected_keys, (
        f"Merged dict must contain all keys from both dicts. "
        f"Missing: {expected_keys - set(merged.keys())}"
    )

    # Edge case: merging with an empty existing dict returns exactly the new dict.
    merged_empty = _merge_frontmatter({}, new_fm)
    assert merged_empty == new_fm, (
        "Merging empty existing dict with new_fm must return new_fm unchanged"
    )


# ---------------------------------------------------------------------------
# Test 5: corrupt frontmatter raises, does not silently overwrite
# ---------------------------------------------------------------------------


def test_corrupt_frontmatter_raises_not_silently_overwrites(
    golden_repos_dir: Path,
) -> None:
    """
    When cidx-meta/<alias>.md has corrupt/unparseable YAML frontmatter,
    _process_one_repo must raise (fail-closed per Messi Rule #13) rather
    than silently overwriting the corrupt file with lifecycle-only data.

    This prevents a bug where a transient corruption event would cause all
    prior metadata (last_analyzed, name, url, etc.) to be irreversibly lost.
    """
    alias = "python-slugify"
    (golden_repos_dir / alias).mkdir()
    meta_path = golden_repos_dir / "cidx-meta" / f"{alias}.md"

    # Write a file with structurally broken YAML frontmatter.
    corrupt_content = (
        "---\n"
        "last_analyzed: 2026-04-28T10:00:00Z\n"
        "name: python-slugify\n"
        "  broken_indent: this is invalid yaml\n"
        "---\n\n"
        "Some original description body.\n"
    )
    meta_path.write_text(corrupt_content, encoding="utf-8")

    runner = _make_runner(golden_repos_dir)
    with pytest.raises(Exception):
        runner._process_one_repo(alias, "job-940")
