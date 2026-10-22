"""Door-side catalog decisions read the caller's grants at most once.

Real catalog fixture and group store; the grant read is wrapped only to
count it. Neutral names only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from code_indexer.server.services.cidx_meta_access import acting_scope
from code_indexer.server.services.cidx_meta_access.catalog_requests import (
    catalog_restriction_applies,
    restrict_catalog_paths,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

CATALOG = "cidx-meta-global"
PATHS = [f"{ALPHA}.md", f"{CANARY}.md", "README.md"]


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path)


def _count_grant_reads(service: Any, monkeypatch: pytest.MonkeyPatch) -> List[str]:
    calls: List[str] = []
    real = service.get_accessible_repos

    def counting(user_id: str) -> Any:
        calls.append(user_id)
        return real(user_id)

    monkeypatch.setattr(service, "get_accessible_repos", counting)
    return calls


def test_restricted_listing_reads_grants_once(
    fx: CidxMetaFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_grant_reads(fx.access_service, monkeypatch)

    allowed = restrict_catalog_paths(
        PATHS, CATALOG, RESTRICTED_USER, fx.access_service, None
    )

    assert allowed == [f"{ALPHA}.md"]
    assert calls == [RESTRICTED_USER]


def test_admin_is_unrestricted_without_reading_grants(
    fx: CidxMetaFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_grant_reads(fx.access_service, monkeypatch)

    assert (
        restrict_catalog_paths(PATHS, CATALOG, ADMIN_USER, fx.access_service, None)
        is None
    )
    assert calls == []


def test_admin_acting_for_users_is_restricted(fx: CidxMetaFixture) -> None:
    with acting_scope([ALPHA]):
        assert catalog_restriction_applies(CATALOG, ADMIN_USER, fx.access_service, None)
        allowed = restrict_catalog_paths(
            PATHS, CATALOG, ADMIN_USER, fx.access_service, None
        )

    assert allowed == [f"{ALPHA}.md"]


def test_package_exports_every_name_it_lists() -> None:
    from code_indexer.server.services import cidx_meta_access

    missing = [n for n in cidx_meta_access.__all__ if not hasattr(cidx_meta_access, n)]

    assert missing == []


def test_missing_access_service_lists_nothing() -> None:
    assert catalog_restriction_applies(CATALOG, ADMIN_USER, None, None) is True
    assert restrict_catalog_paths(PATHS, CATALOG, ADMIN_USER, None, None) == []
