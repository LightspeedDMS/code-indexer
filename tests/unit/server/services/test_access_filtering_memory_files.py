"""
Tests for the memory-aware branch of AccessFilteringService.filter_cidx_meta_files.

Story #877 Phase 3-A — Item 1.

Covers:
1. Non-memory files (non-UUID stem) pass through — ALL listed files verified.
2. Memory file with scope=global is always visible to any user.
3. Memory file with scope=repo and referenced_repo in user's accessible set → visible.
4. Memory file with scope=repo and referenced_repo NOT in accessible set → filtered out.
5. Memory file with scope=file follows the same repo-access rule as scope=repo.
6. Missing memory file → filtered out (fail-closed).
7. Corrupt YAML frontmatter memory file → filtered out (fail-closed).
8. Cache-miss path reads from filesystem and caches result.
9. Admin user sees all memory files including those for repos the admins group
   has not been explicitly granted (admin sees everything).

Real filesystem (tmp_path), real GroupAccessManager, real MemoryMetadataCache,
real memory_io writes. No mocks for the service under test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.server.services.access_filtering_service import AccessFilteringService
from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.services.memory_io import atomic_write_memory_file
from code_indexer.server.services.memory_metadata_cache import MemoryMetadataCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_memory_file(
    memories_dir: Path,
    memory_id: str,
    scope: str,
    referenced_repo: str | None = None,
    referenced_file: str | None = None,
) -> Path:
    """Write a minimal memory file with the given scope/referenced_repo."""
    memories_dir.mkdir(parents=True, exist_ok=True)
    fm: Dict[str, Any] = {
        "id": memory_id,
        "type": "gotcha",
        "scope": scope,
        "summary": "test",
        "tags": [],
        "created_by": "tester",
        "created_at": "2024-01-01T00:00:00+00:00",
        "edited_by": None,
        "edited_at": None,
    }
    if referenced_repo is not None:
        fm["referenced_repo"] = referenced_repo
    if referenced_file is not None:
        fm["referenced_file"] = referenced_file
    path = memories_dir / f"{memory_id}.md"
    atomic_write_memory_file(path, fm)
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path):
    return tmp_path / "test.db"


@pytest.fixture
def memories_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memories"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def group_manager(temp_db: Path) -> GroupAccessManager:
    manager = GroupAccessManager(temp_db)
    powerusers = manager.get_group_by_name("powerusers")
    manager.grant_repo_access("my-repo", powerusers.id, "system:test")
    manager.grant_repo_access("other-repo", powerusers.id, "system:test")
    return manager


@pytest.fixture
def cache(memories_dir: Path) -> MemoryMetadataCache:
    return MemoryMetadataCache(memories_dir, ttl_seconds=60)


@pytest.fixture
def service(
    group_manager: GroupAccessManager, cache: MemoryMetadataCache
) -> AccessFilteringService:
    return AccessFilteringService(group_manager, memory_metadata_cache=cache)


@pytest.fixture
def poweruser(group_manager: GroupAccessManager) -> str:
    powerusers = group_manager.get_group_by_name("powerusers")
    group_manager.assign_user_to_group("puser", powerusers.id, "admin")
    return "puser"


@pytest.fixture
def regular_user(group_manager: GroupAccessManager) -> str:
    users = group_manager.get_group_by_name("users")
    group_manager.assign_user_to_group("ruser", users.id, "admin")
    return "ruser"


@pytest.fixture
def admin_user(group_manager: GroupAccessManager) -> str:
    """Admin user — admins group has NO explicit repo grants here.

    The test verifies that admin sees all memory files regardless of whether
    the admins group was granted the referenced repo.
    """
    admins = group_manager.get_group_by_name("admins")
    group_manager.assign_user_to_group("admin", admins.id, "admin")
    return "admin"


# Stable UUID-shaped memory IDs (32 hex chars)
MEMORY_ID_GLOBAL = "aaaaaaaa" * 4
MEMORY_ID_REPO = "bbbbbbbb" * 4
MEMORY_ID_FILE = "cccccccc" * 4
MEMORY_ID_MISSING = "dddddddd" * 4


# ---------------------------------------------------------------------------
# Test 1: Non-memory files pass through unchanged (all listed files verified)
# ---------------------------------------------------------------------------


def test_non_memory_md_files_all_pass_through_for_poweruser(
    service: AccessFilteringService, poweruser: str
) -> None:
    """Non-UUID .md stems that are not known repo aliases always pass through.

    All listed files must appear in the result unchanged.
    """
    files = ["README.md", "notes.md"]
    result = service.filter_cidx_meta_files(files, poweruser)
    assert set(result) == {"README.md", "notes.md"}


def test_non_memory_md_files_all_pass_through_for_regular_user(
    service: AccessFilteringService, regular_user: str
) -> None:
    """Non-UUID .md files pass through for unprivileged users too."""
    files = ["README.md", "CHANGELOG.md"]
    result = service.filter_cidx_meta_files(files, regular_user)
    assert set(result) == {"README.md", "CHANGELOG.md"}


def test_non_md_files_all_pass_through(
    service: AccessFilteringService, regular_user: str
) -> None:
    """Non-.md files (e.g. .gitignore) always pass through."""
    files = [".gitignore", "config.yaml"]
    result = service.filter_cidx_meta_files(files, regular_user)
    assert set(result) == {".gitignore", "config.yaml"}


# ---------------------------------------------------------------------------
# Test 2: Memory file with scope=global is always visible
# ---------------------------------------------------------------------------


def test_global_memory_file_visible_to_regular_user(
    service: AccessFilteringService,
    memories_dir: Path,
    regular_user: str,
) -> None:
    """scope=global memory files are visible to all users regardless of repo access."""
    _write_memory_file(memories_dir, MEMORY_ID_GLOBAL, scope="global")
    filename = f"{MEMORY_ID_GLOBAL}.md"

    result = service.filter_cidx_meta_files([filename], regular_user)
    assert filename in result


def test_global_memory_file_visible_to_poweruser(
    service: AccessFilteringService,
    memories_dir: Path,
    poweruser: str,
) -> None:
    _write_memory_file(memories_dir, MEMORY_ID_GLOBAL, scope="global")
    filename = f"{MEMORY_ID_GLOBAL}.md"

    result = service.filter_cidx_meta_files([filename], poweruser)
    assert filename in result


# ---------------------------------------------------------------------------
# Test 3: scope=repo with referenced_repo in accessible set → visible
# ---------------------------------------------------------------------------


def test_repo_scoped_memory_visible_when_repo_accessible(
    service: AccessFilteringService,
    memories_dir: Path,
    poweruser: str,
) -> None:
    """scope=repo memory files are visible when referenced_repo is accessible to user."""
    _write_memory_file(
        memories_dir, MEMORY_ID_REPO, scope="repo", referenced_repo="my-repo"
    )
    filename = f"{MEMORY_ID_REPO}.md"

    result = service.filter_cidx_meta_files([filename], poweruser)
    assert filename in result


# ---------------------------------------------------------------------------
# Test 4: scope=repo with referenced_repo NOT in accessible set → filtered out
# ---------------------------------------------------------------------------


def test_repo_scoped_memory_hidden_when_repo_inaccessible(
    service: AccessFilteringService,
    memories_dir: Path,
    regular_user: str,
) -> None:
    """scope=repo memory files are hidden when referenced_repo is not accessible."""
    _write_memory_file(
        memories_dir, MEMORY_ID_REPO, scope="repo", referenced_repo="my-repo"
    )
    filename = f"{MEMORY_ID_REPO}.md"

    result = service.filter_cidx_meta_files([filename], regular_user)
    assert filename not in result


# ---------------------------------------------------------------------------
# Test 5: scope=file follows the same repo-access rule
# ---------------------------------------------------------------------------


def test_file_scoped_memory_visible_when_referenced_repo_accessible(
    service: AccessFilteringService,
    memories_dir: Path,
    poweruser: str,
) -> None:
    """scope=file memory files follow the same repo-access rule as scope=repo."""
    _write_memory_file(
        memories_dir,
        MEMORY_ID_FILE,
        scope="file",
        referenced_repo="my-repo",
        referenced_file="src/foo.py",
    )
    filename = f"{MEMORY_ID_FILE}.md"

    result = service.filter_cidx_meta_files([filename], poweruser)
    assert filename in result


def test_file_scoped_memory_hidden_when_referenced_repo_inaccessible(
    service: AccessFilteringService,
    memories_dir: Path,
    regular_user: str,
) -> None:
    _write_memory_file(
        memories_dir,
        MEMORY_ID_FILE,
        scope="file",
        referenced_repo="my-repo",
        referenced_file="src/foo.py",
    )
    filename = f"{MEMORY_ID_FILE}.md"

    result = service.filter_cidx_meta_files([filename], regular_user)
    assert filename not in result


# ---------------------------------------------------------------------------
# Test 6: Missing memory file → filtered out (fail-closed)
# ---------------------------------------------------------------------------


def test_missing_memory_file_filtered_out(
    service: AccessFilteringService,
    poweruser: str,
) -> None:
    """A memory file that does not exist on disk is excluded (fail-closed)."""
    filename = f"{MEMORY_ID_MISSING}.md"
    # File is intentionally NOT written to disk

    result = service.filter_cidx_meta_files([filename], poweruser)
    assert filename not in result


# ---------------------------------------------------------------------------
# Test 7: Corrupt YAML frontmatter memory file → filtered out (fail-closed)
# ---------------------------------------------------------------------------


def test_corrupt_yaml_memory_file_filtered_out(
    service: AccessFilteringService,
    memories_dir: Path,
    poweruser: str,
) -> None:
    """A memory file with malformed YAML frontmatter is excluded (fail-closed)."""
    corrupt_id = "eeeeeeee" * 4
    path = memories_dir / f"{corrupt_id}.md"
    # Valid delimiters, invalid YAML content (unbalanced brace triggers parse error)
    path.write_text("---\nkey: {unclosed brace\n---\nbody\n")

    filename = f"{corrupt_id}.md"
    result = service.filter_cidx_meta_files([filename], poweruser)
    assert filename not in result


# ---------------------------------------------------------------------------
# Test 8: Cache-miss path reads from filesystem and caches result
# ---------------------------------------------------------------------------


def test_cache_miss_reads_from_filesystem_then_caches(
    memories_dir: Path,
    group_manager: GroupAccessManager,
    poweruser: str,
) -> None:
    """On cache miss, service reads from disk; subsequent call uses cache."""
    cache = MemoryMetadataCache(memories_dir, ttl_seconds=60)
    svc = AccessFilteringService(group_manager, memory_metadata_cache=cache)

    _write_memory_file(memories_dir, MEMORY_ID_GLOBAL, scope="global")
    filename = f"{MEMORY_ID_GLOBAL}.md"

    # First call: cache miss → reads from disk
    result1 = svc.filter_cidx_meta_files([filename], poweruser)
    assert filename in result1

    # Remove file from disk; second call should use cached result
    (memories_dir / filename).unlink()
    result2 = svc.filter_cidx_meta_files([filename], poweruser)
    assert filename in result2, (
        "Second call should return cached result after file deletion"
    )


# ---------------------------------------------------------------------------
# Test 9: Admin user sees all memory files including truly inaccessible-repo ones
# ---------------------------------------------------------------------------


def test_admin_sees_memory_files_for_repos_not_granted_to_admins(
    service: AccessFilteringService,
    memories_dir: Path,
    admin_user: str,
) -> None:
    """Admin user sees all memory files even for repos the admins group has no grant for.

    The admins group fixture intentionally has NO explicit repo grants.
    The memory file references 'my-repo' which is only granted to powerusers.
    Admin must still see the file.
    """
    _write_memory_file(
        memories_dir, MEMORY_ID_REPO, scope="repo", referenced_repo="my-repo"
    )
    _write_memory_file(memories_dir, MEMORY_ID_GLOBAL, scope="global")

    files = [f"{MEMORY_ID_REPO}.md", f"{MEMORY_ID_GLOBAL}.md"]
    result = service.filter_cidx_meta_files(files, admin_user)

    assert f"{MEMORY_ID_REPO}.md" in result
    assert f"{MEMORY_ID_GLOBAL}.md" in result
