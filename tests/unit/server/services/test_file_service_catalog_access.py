"""The file service never serves the catalog without the request's judgement.

Listing or reading a catalog directory requires the access the door judged
for this request (the caller's catalog view, or the explicit unrestricted
answer). A request that carries none is answered exactly like a missing
path, so a caller that forgets the argument gets nothing. With a restricted
view, hidden files never appear on any page and every count covers visible
files only. Real catalog fixture on disk. Neutral names only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.models.api_models import FileListQueryParams
from code_indexer.server.services.cidx_meta_access.file_doors import (
    CatalogFileAccess,
    judge_file_door,
)
from code_indexer.server.services.file_service import FileListingService
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    MEMORY_ALPHA,
    MEMORY_GLOBAL,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

CATALOG_ALIAS = "cidx-meta-global"


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path)


def _params(page: int = 1, limit: int = 500) -> FileListQueryParams:
    return FileListQueryParams(page=page, limit=limit, path_pattern=None, language=None)


def test_unjudged_listing_of_the_catalog_is_not_found(fx: CidxMetaFixture) -> None:
    with pytest.raises(FileNotFoundError):
        FileListingService().list_files_by_path(str(fx.root), _params())


def test_unjudged_read_of_the_catalog_is_not_found(fx: CidxMetaFixture) -> None:
    with pytest.raises(FileNotFoundError) as raised:
        FileListingService().get_file_content_by_path(str(fx.root), f"{CANARY}.md")
    assert CANARY not in str(raised.value).replace(f"{CANARY}.md", "<path>")


def _restricted(fx: CidxMetaFixture) -> CatalogFileAccess:
    access = judge_file_door(
        CATALOG_ALIAS,
        str(fx.root),
        RESTRICTED_USER,
        fx.access_service,
        None,
        None,
        fx.registry,
    )
    assert access.view is not None
    return access


VISIBLE = {
    f"{ALPHA}.md",
    "dependency-map/_index.md",
    "dependency-map/payments.md",
    "dependency-map/ledger.md",
    "dependency-map/reports.md",
    "dependency-map/_dep_types.md",
    "dependency-map/_analysis_guidelines.md",
    "xray-patterns/__any__/p.yaml",
    f"memories/{MEMORY_GLOBAL}.md",
    f"memories/{MEMORY_ALPHA}.md",
}


def test_every_page_and_count_covers_visible_files_only(fx: CidxMetaFixture) -> None:
    service, access = FileListingService(), _restricted(fx)
    seen, page = [], 1
    while page <= 20:
        listed = service.list_files_by_path(str(fx.root), _params(page, 3), access)
        seen += [f.path for f in listed.files]
        assert listed.pagination.total == len(VISIBLE)
        if not listed.pagination.has_next:
            break
        page += 1

    assert sorted(seen) == sorted(VISIBLE)


MISSING = "xray-patterns/__any__/missing.yaml"
LINK = "xray-patterns/__any__/leak.yaml"


@pytest.mark.parametrize(
    "spelling",
    [
        f"./{CANARY}.md",
        f"xray-patterns/../{CANARY}.md",
        f"//{CANARY}.md",
        LINK,
        f"dependency-map/../dependency-map/{CANARY}.md",
    ],
)
def test_hidden_file_spellings_read_like_a_missing_file(
    fx: CidxMetaFixture, spelling: str
) -> None:
    # The link's own name is allowed; the file it serves is hidden.
    (fx.root / LINK).symlink_to(fx.root / f"{CANARY}.md")
    service, access = FileListingService(), _restricted(fx)

    with pytest.raises(FileNotFoundError) as hidden:
        service.get_file_content_by_path(str(fx.root), spelling, access=access)
    with pytest.raises(FileNotFoundError) as missing:
        service.get_file_content_by_path(str(fx.root), MISSING, access=access)

    assert str(hidden.value).replace(spelling, "<p>") == str(missing.value).replace(
        MISSING, "<p>"
    )
    listed = service.list_files_by_path(str(fx.root), _params(), access)
    assert LINK not in [f.path for f in listed.files]


def test_judgement_for_another_directory_is_not_found(
    fx: CidxMetaFixture, tmp_path: Path
) -> None:
    ordinary = tmp_path / "ordinary-judged"
    ordinary.mkdir()
    golden = tmp_path / "golden-repos"
    golden.mkdir()
    access = judge_file_door(
        "ordinary-global",
        str(ordinary),
        RESTRICTED_USER,
        fx.access_service,
        None,
        str(golden),
        fx.registry,
    )
    assert access.view is None

    with pytest.raises(FileNotFoundError):
        FileListingService().list_files_by_path(str(fx.root), _params(), access)
    with pytest.raises(FileNotFoundError):
        FileListingService().get_file_content_by_path(
            str(fx.root), f"{ALPHA}.md", access=access
        )


def test_unjudged_ordinary_directory_is_served_unchanged(tmp_path: Path) -> None:
    repo = tmp_path / "ordinary"
    repo.mkdir()
    (repo / "a.txt").write_text("one\n")

    listed = FileListingService().list_files_by_path(str(repo), _params())
    read = FileListingService().get_file_content_by_path(str(repo), "a.txt")

    assert [f.path for f in listed.files] == ["a.txt"]
    assert read["content"] == "one\n"
