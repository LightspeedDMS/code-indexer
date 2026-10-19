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


def test_admin_listing_is_unchanged(fx: CidxMetaFixture) -> None:
    assert fx.access_service.filter_cidx_meta_files(LISTING, ADMIN_USER) == LISTING


def test_acting_scope_narrows_an_admin_listing(fx: CidxMetaFixture) -> None:
    with acting_scope({CANARY}):
        kept = fx.access_service.filter_cidx_meta_files(LISTING, ADMIN_USER)
    assert f"{CANARY}.md" in kept
    assert f"memories/{MEMORY_CANARY}.md" in kept
    assert f"{ALPHA}.md" not in kept
    assert f"memories/{MEMORY_ALPHA}.md" not in kept
