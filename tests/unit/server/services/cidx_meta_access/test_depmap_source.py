"""The caller's dependency map as a DepMapSource (real fixture, no mocks)."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.services.cidx_meta_access import (
    MetaProjector,
    MetaVisibility,
)
from code_indexer.server.services.cidx_meta_access.depmap_source import (
    ProjectedDepMapSource,
    resolve_depmap_source,
)
from code_indexer.server.services.cidx_meta_access.universe_provider import (
    process_alias_universe,
)
from code_indexer.server.services.dep_map_source import FilesystemDepMapSource
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    AUDIT_DOMAIN,
    CANARY,
    GAMMA,
    RESTRICTED_USER,
    CidxMetaFixture,
    add_depmap_extension,
    build_cidx_meta_fixture,
)


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> CidxMetaFixture:
    fixture = build_cidx_meta_fixture(tmp_path_factory.mktemp("depmap-source"))
    add_depmap_extension(fixture)
    return fixture


def _restricted(fx: CidxMetaFixture) -> ProjectedDepMapSource:
    source, restricted = resolve_depmap_source(
        RESTRICTED_USER, fx.access_service, fx.dep_map_dir, fx.registry
    )
    assert restricted is True
    assert isinstance(source, ProjectedDepMapSource)
    return source


def test_admin_reads_the_filesystem_source(fx: CidxMetaFixture) -> None:
    source, restricted = resolve_depmap_source(
        ADMIN_USER, fx.access_service, fx.dep_map_dir, fx.registry
    )
    assert restricted is False
    assert isinstance(source, FilesystemDepMapSource)
    assert source.label("payments") == str(fx.dep_map_dir / "payments.md")


def test_domains_json_lists_visible_domains_with_visible_participants(
    fx: CidxMetaFixture,
) -> None:
    source = _restricted(fx)
    entries = source.load_domains_json()
    names = sorted(e["name"] for e in entries)
    # reports is hidden: its frontmatter does not parse.
    assert names == sorted(["payments", "ledger", AUDIT_DOMAIN])
    for entry in entries:
        assert set(entry) == {"name", "participating_repos"}
        assert entry["participating_repos"] == [ALPHA]
    audit = next(e for e in entries if e["name"] == AUDIT_DOMAIN)
    description = source.describe(audit)
    assert CANARY not in description
    assert "[restricted repository]" in description
    assert source.describe({"name": CANARY}) == ""


def test_domain_names_and_reads_hide_denied_documents(fx: CidxMetaFixture) -> None:
    source = _restricted(fx)
    assert CANARY not in source.domain_names()
    assert "inventory" not in source.domain_names()
    for hidden in (CANARY, "inventory", "legacy", "_journal", "_activity"):
        assert source.has_domain(hidden) is False
        with pytest.raises(FileNotFoundError):
            source.read_text(hidden)


def test_visible_documents_are_projected_and_support_documents_raw(
    fx: CidxMetaFixture,
) -> None:
    source = _restricted(fx)
    payments = source.read_text("payments")
    assert ALPHA in payments
    assert CANARY not in payments and GAMMA not in payments
    index = source.read_text("_index")
    assert CANARY not in index and "payments" in index
    assert source.read_text("_dep_types") == (
        fx.dep_map_dir / "_dep_types.md"
    ).read_text(encoding="utf-8")


def test_labels_are_catalog_relative(fx: CidxMetaFixture) -> None:
    source = _restricted(fx)
    assert source.label("payments") == "dependency-map/payments.md"
    assert source.resolved_label("payments") == "dependency-map/payments.md"
    assert source.within("payments") is True
    assert source.within(CANARY) is False


def test_projected_source_refuses_an_unrestricted_caller(
    fx: CidxMetaFixture,
) -> None:
    vis = MetaVisibility.everything()
    projector = MetaProjector(
        vis, dep_map_dir=fx.dep_map_dir, universe=process_alias_universe(fx.registry)
    )
    with pytest.raises(ValueError):
        ProjectedDepMapSource(fx.dep_map_dir, vis, projector)


def test_restricted_projection_without_registry_fails_loudly(
    fx: CidxMetaFixture, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError):
        resolve_depmap_source(RESTRICTED_USER, fx.access_service, tmp_path, None)


def test_one_universe_per_registry(fx: CidxMetaFixture) -> None:
    assert process_alias_universe(fx.registry) is process_alias_universe(fx.registry)
