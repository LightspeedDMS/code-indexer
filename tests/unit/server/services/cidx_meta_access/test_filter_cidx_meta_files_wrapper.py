"""filter_cidx_meta_files(): the listing wrapper over classify().

It takes full catalog-relative paths. Until projected content is served,
TRANSFORM paths (dependency-map domain documents and ``_index.md``) are
excluded; ALLOW paths such as ``dependency-map/_dep_types.md`` are kept.
Neutral names only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.services.cidx_meta_access import acting_scope
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    MEMORY_ALPHA,
    MEMORY_CANARY,
    MEMORY_GLOBAL,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

LISTING = [
    f"{ALPHA}.md",
    f"{CANARY}.md",
    f"{GAMMA}.md",
    "README.md",
    ".gitignore",
    ".code-indexer/index/meta.json",
    "dependency-map/payments.md",
    "dependency-map/_index.md",
    "dependency-map/_dep_types.md",
    f"dependency-map/{ALPHA}.md",
    f"memories/{MEMORY_GLOBAL}.md",
    f"memories/{MEMORY_ALPHA}.md",
    f"memories/{MEMORY_CANARY}.md",
    "xray-patterns/__any__/p.yaml",
    f"xray-patterns/{CANARY}/p.yaml",
]


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path)


def test_restricted_listing_keeps_only_allowed_full_paths(fx: CidxMetaFixture) -> None:
    kept = fx.access_service.filter_cidx_meta_files(LISTING, RESTRICTED_USER)
    assert kept == [
        f"{ALPHA}.md",
        "dependency-map/_dep_types.md",
        f"memories/{MEMORY_GLOBAL}.md",
        f"memories/{MEMORY_ALPHA}.md",
        "xray-patterns/__any__/p.yaml",
    ]


def test_transform_documents_are_excluded_until_projection_is_served(
    fx: CidxMetaFixture,
) -> None:
    kept = fx.access_service.filter_cidx_meta_files(
        ["dependency-map/payments.md", "dependency-map/_index.md"], RESTRICTED_USER
    )
    assert kept == []


def test_alias_granted_to_no_group_and_readme_are_denied(fx: CidxMetaFixture) -> None:
    kept = fx.access_service.filter_cidx_meta_files(
        [f"{GAMMA}.md", "README.md"], RESTRICTED_USER
    )
    assert kept == []


def test_memory_scope_change_is_seen_at_once(fx: CidxMetaFixture) -> None:
    from code_indexer.server.services.access_filtering_service import (
        AccessFilteringService,
    )
    from code_indexer.server.services.memory_io import atomic_write_memory_file

    memories = fx.root / "memories"
    service = AccessFilteringService(fx.group_manager, memories_dir=memories)
    path = f"memories/{MEMORY_GLOBAL}.md"
    assert service.filter_cidx_meta_files([path], RESTRICTED_USER) == [path]

    atomic_write_memory_file(
        memories / f"{MEMORY_GLOBAL}.md",
        {
            "id": MEMORY_GLOBAL,
            "type": "gotcha",
            "scope": "repo",
            "referenced_repo": f"{CANARY}-global",
            "summary": "fixture memory",
            "tags": [],
            "created_by": "tester",
            "created_at": "2026-01-01T00:00:00+00:00",
            "edited_by": None,
            "edited_at": None,
        },
    )
    assert service.filter_cidx_meta_files([path], RESTRICTED_USER) == []


def test_catalog_scoped_memory_is_judged_alike_by_every_door(
    fx: CidxMetaFixture,
) -> None:
    from code_indexer.server.services.cidx_meta_access import (
        memory_visibility_fn,
        resolve_meta_visibility,
    )
    from code_indexer.server.services.memory_io import atomic_write_memory_file

    memory_id = "d4" * 16
    atomic_write_memory_file(
        fx.root / "memories" / f"{memory_id}.md",
        {
            "id": memory_id,
            "type": "gotcha",
            "scope": "repo",
            "referenced_repo": "cidx-meta-global",
            "summary": "fixture memory",
            "tags": [],
            "created_by": "tester",
            "created_at": "2026-01-01T00:00:00+00:00",
            "edited_by": None,
            "edited_at": None,
        },
    )
    retrieval = fx.access_service.filter_visible_memory_ids(
        [memory_id], RESTRICTED_USER
    )
    listing = fx.access_service.filter_cidx_meta_files(
        [f"memories/{memory_id}.md"], RESTRICTED_USER
    )
    vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    policy = memory_visibility_fn(fx.access_service, vis)(memory_id)
    assert retrieval == [memory_id]
    assert listing == [f"memories/{memory_id}.md"]
    assert policy is True


def test_admin_listing_is_unchanged(fx: CidxMetaFixture) -> None:
    assert fx.access_service.filter_cidx_meta_files(LISTING, ADMIN_USER) == LISTING


def test_acting_scope_narrows_an_admin_listing(fx: CidxMetaFixture) -> None:
    with acting_scope({CANARY}):
        kept = fx.access_service.filter_cidx_meta_files(LISTING, ADMIN_USER)
    assert f"{CANARY}.md" in kept
    assert f"memories/{MEMORY_CANARY}.md" in kept
    assert f"{ALPHA}.md" not in kept
    assert f"memories/{MEMORY_ALPHA}.md" not in kept
