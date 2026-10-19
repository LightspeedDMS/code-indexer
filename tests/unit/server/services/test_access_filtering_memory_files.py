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
    group_manager: GroupAccessManager, cache: MemoryMetadataCache, memories_dir: Path
) -> AccessFilteringService:
    return AccessFilteringService(
        group_manager, memory_metadata_cache=cache, memories_dir=memories_dir
    )


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
# Test 1: Files that are neither a visible description nor a memory are denied
# ---------------------------------------------------------------------------


def test_non_memory_md_files_are_denied_for_poweruser(
    service: AccessFilteringService, poweruser: str
) -> None:
    """Root .md files that are not a granted repo's description are denied."""
    files = ["README.md", "notes.md", "my-repo.md"]
    result = service.filter_cidx_meta_files(files, poweruser)
    assert result == ["my-repo.md"]


def test_non_memory_md_files_are_denied_for_regular_user(
    service: AccessFilteringService, regular_user: str
) -> None:
    """Unprivileged users see none of them either."""
    files = ["README.md", "CHANGELOG.md"]
    result = service.filter_cidx_meta_files(files, regular_user)
    assert result == []


def test_non_md_files_are_denied(
    service: AccessFilteringService, regular_user: str
) -> None:
    """Non-.md files (e.g. .gitignore) are denied by default."""
    files = [".gitignore", "config.yaml"]
    result = service.filter_cidx_meta_files(files, regular_user)
    assert result == []


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
    filename = f"memories/{MEMORY_ID_GLOBAL}.md"

    result = service.filter_cidx_meta_files([filename], regular_user)
    assert filename in result


def test_global_memory_file_visible_to_poweruser(
    service: AccessFilteringService,
    memories_dir: Path,
    poweruser: str,
) -> None:
    _write_memory_file(memories_dir, MEMORY_ID_GLOBAL, scope="global")
    filename = f"memories/{MEMORY_ID_GLOBAL}.md"

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
    filename = f"memories/{MEMORY_ID_REPO}.md"

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
    filename = f"memories/{MEMORY_ID_REPO}.md"

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
    filename = f"memories/{MEMORY_ID_FILE}.md"

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
    filename = f"memories/{MEMORY_ID_FILE}.md"

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
    filename = f"memories/{MEMORY_ID_MISSING}.md"
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

    filename = f"memories/{corrupt_id}.md"
    result = service.filter_cidx_meta_files([filename], poweruser)
    assert filename not in result


# ---------------------------------------------------------------------------
# Test 8: Cache-miss path reads from filesystem and caches result
# ---------------------------------------------------------------------------


def test_listing_reads_memory_scope_fresh(
    memories_dir: Path,
    group_manager: GroupAccessManager,
    poweruser: str,
) -> None:
    """A warm metadata cache never decides a listing: the memory's
    frontmatter is read fresh, so a deleted memory disappears at once."""
    cache = MemoryMetadataCache(memories_dir, ttl_seconds=60)
    svc = AccessFilteringService(
        group_manager, memory_metadata_cache=cache, memories_dir=memories_dir
    )

    _write_memory_file(memories_dir, MEMORY_ID_GLOBAL, scope="global")
    filename = f"memories/{MEMORY_ID_GLOBAL}.md"

    result1 = svc.filter_cidx_meta_files([filename], poweruser)
    assert filename in result1

    (memories_dir / f"{MEMORY_ID_GLOBAL}.md").unlink()
    result2 = svc.filter_cidx_meta_files([filename], poweruser)
    assert filename not in result2


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

    files = [f"memories/{MEMORY_ID_REPO}.md", f"memories/{MEMORY_ID_GLOBAL}.md"]
    result = service.filter_cidx_meta_files(files, admin_user)

    assert f"memories/{MEMORY_ID_REPO}.md" in result
    assert f"memories/{MEMORY_ID_GLOBAL}.md" in result


# ---------------------------------------------------------------------------
# filter_visible_memory_ids: the same rule applied to bare memory ids
# ---------------------------------------------------------------------------


def _write_scoped_memories(memories_dir: Path) -> None:
    _write_memory_file(
        memories_dir, MEMORY_ID_REPO, scope="repo", referenced_repo="my-repo"
    )
    _write_memory_file(memories_dir, MEMORY_ID_GLOBAL, scope="global")
    _write_memory_file(
        memories_dir, MEMORY_ID_FILE, scope="file", referenced_repo="ungranted-repo"
    )


def test_visible_memory_ids_keep_input_order_and_apply_scope_rule(
    service: AccessFilteringService, memories_dir: Path, poweruser: str
) -> None:
    _write_scoped_memories(memories_dir)
    ids = [MEMORY_ID_REPO, MEMORY_ID_FILE, MEMORY_ID_MISSING, MEMORY_ID_GLOBAL]

    assert service.filter_visible_memory_ids(ids, poweruser) == [
        MEMORY_ID_REPO,
        MEMORY_ID_GLOBAL,
    ]


def test_visible_memory_ids_drop_non_memory_identifiers(
    service: AccessFilteringService, poweruser: str
) -> None:
    assert service.filter_visible_memory_ids(["my-repo", "README", ""], poweruser) == []


def test_visible_memory_ids_admin_keeps_every_id(
    service: AccessFilteringService, memories_dir: Path, admin_user: str
) -> None:
    _write_scoped_memories(memories_dir)
    ids = [MEMORY_ID_FILE, MEMORY_ID_MISSING, MEMORY_ID_GLOBAL]

    assert service.filter_visible_memory_ids(ids, admin_user) == ids


def test_visible_memory_ids_empty_input(
    service: AccessFilteringService, poweruser: str
) -> None:
    assert service.filter_visible_memory_ids([], poweruser) == []


def test_visible_memory_ids_resolve_grants_once_per_call(
    service: AccessFilteringService,
    group_manager: GroupAccessManager,
    memories_dir: Path,
    poweruser: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_scoped_memories(memories_dir)
    calls = []
    real_get_group_repos = group_manager.get_group_repos

    def _counting_get_group_repos(group_id: int) -> Any:
        calls.append(group_id)
        return real_get_group_repos(group_id)

    monkeypatch.setattr(group_manager, "get_group_repos", _counting_get_group_repos)
    ids = [MEMORY_ID_REPO, MEMORY_ID_FILE, MEMORY_ID_GLOBAL]

    service.filter_visible_memory_ids(ids, poweruser)

    assert len(calls) == 1


def test_visible_memory_ids_see_scope_change_written_by_another_instance(
    group_manager: GroupAccessManager, memories_dir: Path, poweruser: str
) -> None:
    """Each node authorizes from the file as it is now, never a cached scope.

    The reader's cache stays warm with the old scope on purpose: on another
    cluster node nothing invalidates it when the writer node edits the file.
    """
    writer_cache = MemoryMetadataCache(memories_dir, ttl_seconds=60)
    reader_cache = MemoryMetadataCache(memories_dir, ttl_seconds=60)
    reader = AccessFilteringService(
        group_manager, memory_metadata_cache=reader_cache, memories_dir=memories_dir
    )
    _write_memory_file(memories_dir, MEMORY_ID_GLOBAL, scope="global")
    reader_cache.get(MEMORY_ID_GLOBAL)
    assert reader.filter_visible_memory_ids([MEMORY_ID_GLOBAL], poweruser) == [
        MEMORY_ID_GLOBAL
    ]

    _write_memory_file(
        memories_dir, MEMORY_ID_GLOBAL, scope="repo", referenced_repo="ungranted-repo"
    )
    writer_cache.invalidate(MEMORY_ID_GLOBAL)

    assert reader.filter_visible_memory_ids([MEMORY_ID_GLOBAL], poweruser) == []


def test_visible_memory_ids_admin_gets_only_memory_identifiers(
    service: AccessFilteringService, memories_dir: Path, admin_user: str
) -> None:
    _write_memory_file(memories_dir, MEMORY_ID_GLOBAL, scope="global")
    ids = ["my-repo", MEMORY_ID_GLOBAL, "../escape", ""]

    assert service.filter_visible_memory_ids(ids, admin_user) == [MEMORY_ID_GLOBAL]


def test_visible_memory_ids_hide_non_string_referenced_repo(
    service: AccessFilteringService, memories_dir: Path, poweruser: str
) -> None:
    memories_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_memory_file(
        memories_dir / f"{MEMORY_ID_REPO}.md",
        {"id": MEMORY_ID_REPO, "scope": "repo", "referenced_repo": 12345},
    )

    assert service.filter_visible_memory_ids([MEMORY_ID_REPO], poweruser) == []


def test_visible_memory_ids_hide_everything_without_memories_dir(
    group_manager: GroupAccessManager,
    cache: MemoryMetadataCache,
    memories_dir: Path,
    poweruser: str,
) -> None:
    _write_memory_file(memories_dir, MEMORY_ID_GLOBAL, scope="global")
    cache_only = AccessFilteringService(group_manager, memory_metadata_cache=cache)

    assert cache_only.filter_visible_memory_ids([MEMORY_ID_GLOBAL], poweruser) == []


def test_visible_memory_ids_hide_unreadable_memory_path(
    service: AccessFilteringService, memories_dir: Path, poweruser: str
) -> None:
    (memories_dir / f"{MEMORY_ID_GLOBAL}.md").mkdir(parents=True)

    assert service.filter_visible_memory_ids([MEMORY_ID_GLOBAL], poweruser) == []
