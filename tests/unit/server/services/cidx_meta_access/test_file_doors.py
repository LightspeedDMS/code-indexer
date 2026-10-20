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


def _configured(repo: Path, config: Any) -> Path:
    (repo / ".code-indexer").mkdir(exist_ok=True)
    text = config if isinstance(config, str) else json.dumps(config)
    (repo / ".code-indexer" / "config.json").write_text(text)
    return repo


def _components(repo: Path, names: Any) -> dict:
    for name in names:
        (repo / name).mkdir()
    return {"proxy_mode": True, "discovered_repos": names}


@pytest.mark.parametrize(
    "config", [{"proxy_mode": False}, {"embedding_provider": "voyage-ai"}]
)
def test_non_composite_configuration_leaves_an_ordinary_directory_unrestricted(
    fx: CidxMetaFixture, golden: Path, ordinary: Path, config: Any
) -> None:
    served = _configured(ordinary, config)
    assert _judge(fx, f"{ALPHA}-global", served, golden).view is None


def test_composite_of_ordinary_components_is_unrestricted(
    fx: CidxMetaFixture, golden: Path, ordinary: Path
) -> None:
    served = _configured(ordinary, _components(ordinary, [ALPHA, "bravo"]))
    assert _judge(fx, f"{ALPHA}-global", served, golden).view is None


def test_composite_with_a_catalog_component_is_restricted(
    fx: CidxMetaFixture, golden: Path, ordinary: Path
) -> None:
    (ordinary / "notes").symlink_to(fx.root)
    served = _configured(ordinary, {"proxy_mode": True, "discovered_repos": ["notes"]})

    assert _judge(fx, f"{ALPHA}-global", served, golden).view is not None
    assert _judge(fx, f"{ALPHA}-global", served, golden, ADMIN_USER) is UNRESTRICTED


@pytest.mark.parametrize(
    "config",
    [
        "{not json",
        "[1, 2]",
        {"proxy_mode": True, "discovered_repos": "alpha"},
        {"proxy_mode": True, "discovered_repos": ["../elsewhere"]},
        {"proxy_mode": True, "discovered_repos": [".."]},
        {"proxy_mode": True, "discovered_repos": [7]},
        " " * (1048576 + 1),
    ],
)
def test_unjudgeable_composite_configuration_is_restricted(
    fx: CidxMetaFixture, golden: Path, ordinary: Path, config: Any
) -> None:
    served = _configured(ordinary, config)
    assert _judge(fx, f"{ALPHA}-global", served, golden).view is not None


def test_unreadable_file_raises_instead_of_reading_as_binary(tmp_path: Path) -> None:
    import os

    from code_indexer.server.services.cidx_meta_access.file_doors import (
        served_file_payload,
    )

    unreadable = tmp_path / "locked.txt"
    unreadable.write_text("visible content\n")
    unreadable.chmod(0)
    if os.access(unreadable, os.R_OK):
        pytest.skip("running with privileges that bypass file modes")
    try:
        with pytest.raises(PermissionError):
            served_file_payload("locked.txt", unreadable, UNRESTRICTED)
    finally:
        unreadable.chmod(0o644)


@pytest.mark.parametrize("raw", [b"a\x00b", b"caf\xe9 latin-1\n"])
def test_undecodable_file_is_served_as_binary(tmp_path: Path, raw: bytes) -> None:
    from code_indexer.server.services.cidx_meta_access.file_doors import (
        served_file_payload,
    )

    blob = tmp_path / "blob.bin"
    blob.write_bytes(raw)

    assert served_file_payload("blob.bin", blob, UNRESTRICTED) == {
        "path": "blob.bin",
        "is_binary": True,
        "size": len(raw),
        "content": None,
    }


def test_text_file_is_served_with_its_content(tmp_path: Path) -> None:
    from code_indexer.server.services.cidx_meta_access.file_doors import (
        served_file_payload,
    )

    text = tmp_path / "notes.txt"
    text.write_text("hello\n")

    payload = served_file_payload("notes.txt", text, UNRESTRICTED)
    assert payload == {
        "path": "notes.txt",
        "is_binary": False,
        "size": 6,
        "content": "hello\n",
    }


def test_composite_entry_outside_its_component_fails_closed() -> None:
    from datetime import datetime

    from code_indexer.server.managers.composite_file_listing import ComponentListing
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
        visible_composite_entries(
            [ComponentListing("cidx-meta", "", [stray])], lambda name: UNRESTRICTED
        )


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
