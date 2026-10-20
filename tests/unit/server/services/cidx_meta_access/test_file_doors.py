"""The per-request judgement every catalog file door hands the file service.

Only an admin, or a request whose alias is not the catalog and whose served
directory is confirmed not to be a catalog location, is unrestricted. A
restricted catalog request with no known served directory is not found, an
alias the gate cannot judge restricts, and the served directory decides even
when the alias names an ordinary repository. Real catalog fixture, real alias
pointers, real group store. Neutral names only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from code_indexer.server.cache.handle_binding import handle_binding, mint_handle
from code_indexer.server.services.cidx_meta_access.file_doors import (
    UNRESTRICTED,
    CatalogFileAccess,
    bound_handles,
    judge_file_door,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path / "data")


@pytest.fixture
def golden(tmp_path: Path, fx: CidxMetaFixture) -> Path:
    root = tmp_path / "data" / "golden-repos" / "aliases"
    root.mkdir(parents=True)
    (root / "cidx-meta-global.json").write_text(
        json.dumps({"target_path": str(fx.root)})
    )
    return root.parent


@pytest.fixture
def ordinary(tmp_path: Path) -> Path:
    repo = tmp_path / "ordinary-repo"
    repo.mkdir()
    return repo


def _judge(
    fx: CidxMetaFixture,
    alias: Any,
    served: Optional[Path],
    golden: Optional[Path],
    who: str = RESTRICTED_USER,
) -> CatalogFileAccess:
    return judge_file_door(
        alias,
        str(served) if served else None,
        who,
        fx.access_service,
        None,
        str(golden) if golden else None,
        fx.registry,
    )


def test_admin_is_unrestricted_on_the_catalog(
    fx: CidxMetaFixture, golden: Path
) -> None:
    assert _judge(fx, "cidx-meta-global", fx.root, golden, ADMIN_USER) is UNRESTRICTED


def test_catalog_request_with_unknown_served_path_is_not_found(
    fx: CidxMetaFixture, golden: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        _judge(fx, "cidx-meta-global", None, golden)


@pytest.mark.parametrize("alias", [None, "", [], "*", ("a", ("b",)), 7])
def test_unjudgeable_alias_is_restricted(
    fx: CidxMetaFixture, golden: Path, ordinary: Path, alias: Any
) -> None:
    assert _judge(fx, alias, ordinary, golden).view is not None


def test_ordinary_alias_serving_a_catalog_location_is_restricted(
    fx: CidxMetaFixture, golden: Path, tmp_path: Path
) -> None:
    pointed = tmp_path / "pointed-at-catalog"
    pointed.symlink_to(fx.root)

    assert _judge(fx, f"{ALPHA}-global", pointed, golden).view is not None


def test_ordinary_alias_on_an_ordinary_directory_is_unrestricted(
    fx: CidxMetaFixture, golden: Path, ordinary: Path
) -> None:
    access = _judge(fx, f"{ALPHA}-global", ordinary, golden)
    assert access.view is None and access.served == str(ordinary)


def test_missing_golden_repos_dir_restricts(
    fx: CidxMetaFixture, ordinary: Path
) -> None:
    assert _judge(fx, f"{ALPHA}-global", ordinary, None).view is not None


def test_composite_entry_outside_its_component_fails_closed() -> None:
    from datetime import datetime

    from code_indexer.server.models.composite_file_models import FileInfo
    from code_indexer.server.services.cidx_meta_access.file_doors import (
        visible_composite_entries,
    )

    stray = FileInfo(
        full_path="other/notes.md",
        name="notes.md",
        size=1,
        modified=datetime.now(),
        is_directory=False,
        component_repo="cidx-meta",
    )

    with pytest.raises(ValueError):
        visible_composite_entries([stray], "", lambda name: UNRESTRICTED)


def test_bound_handles_carry_the_callers_visibility(
    fx: CidxMetaFixture, golden: Path
) -> None:
    access = _judge(fx, "cidx-meta-global", fx.root, golden)
    assert access.view is not None

    with bound_handles(access):
        bound = mint_handle("h")
    with bound_handles(UNRESTRICTED):
        plain = mint_handle("h")

    assert handle_binding(bound) == access.view.vis.vis_hash
    assert handle_binding(plain) is None
    assert handle_binding(mint_handle("h")) is None
