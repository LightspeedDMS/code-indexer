"""Projection fails closed: only recognised structures reach restricted callers.

Real fixture files and registry; synthetic lines use neutral names only.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest

from code_indexer.server.services.cidx_meta_access import (
    AliasUniverse,
    MetaVisibility,
    domain_participants,
    load_depmap_model,
    project_chunk,
    project_lines,
    resolve_meta_visibility,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
    read_lines,
)


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path_factory.mktemp("fail-closed"))


def _project(fx: CidxMetaFixture, lines: List[str]) -> List[Optional[str]]:
    return project_lines(
        lines,
        "dependency-map/payments.md",
        resolve_meta_visibility(RESTRICTED_USER, fx.access_service),
        load_depmap_model(fx.dep_map_dir),
        AliasUniverse(fx.registry).snapshot(),
    )


def test_unrecognised_table_is_removed(fx: CidxMetaFixture) -> None:
    lines = ["| Something | Misc |", "|---|---|", f"| {ALPHA} | unknown-private-name |"]
    assert _project(fx, lines) == [None, None, None]


def test_table_row_without_a_recognised_header_is_removed(fx: CidxMetaFixture) -> None:
    assert _project(fx, [f"| {ALPHA} | allowed | extra |"]) == [None]


def test_recognised_table_with_wrong_cell_count_row_is_removed(
    fx: CidxMetaFixture,
) -> None:
    lines = [
        "| Repository | Language | Role |",
        "|---|---|---|",
        f"| {ALPHA} | Python | API |",
        f"| {ALPHA} | Python |",
    ]
    assert _project(fx, lines) == lines[:3] + [None]


def test_unclosed_frontmatter_removes_the_document(fx: CidxMetaFixture) -> None:
    lines = ["---", f"participating_repos: [{ALPHA}, {CANARY}", "", "body text"]
    assert _project(fx, lines) == [None, None, None, None]


def test_yaml_fragment_before_a_delimiter_is_removed(fx: CidxMetaFixture) -> None:
    lines = [f"participating_repos: [{ALPHA}, {CANARY}", "---", "plain prose"]
    assert _project(fx, lines) == [None, None, "plain prose"]


DOCUMENTS = [
    "dependency-map/payments.md",
    "dependency-map/ledger.md",
    "dependency-map/reports.md",
    "dependency-map/_index.md",
    f"{ALPHA}.md",
]


@pytest.mark.parametrize("rel", DOCUMENTS)
def test_chunk_never_exposes_more_than_the_document(
    fx: CidxMetaFixture, rel: str
) -> None:
    document = read_lines(fx.root / rel)
    vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    model = load_depmap_model(fx.dep_map_dir)
    universe = AliasUniverse(fx.registry).snapshot()
    full = project_lines(document, rel, vis, model, universe)
    for start in range(len(document)):
        for end in range(start + 1, min(len(document), start + 12) + 1):
            chunk = project_chunk(
                document[start:end], start + 1, document, rel, vis, model, universe
            )
            assert len(chunk) == end - start
            for offset, line in enumerate(chunk):
                assert line is None or line == full[start + offset], (rel, start, end)


def test_stale_chunk_line_is_removed(fx: CidxMetaFixture) -> None:
    document = read_lines(fx.root / f"{ALPHA}.md")
    stale = [document[0], f"{document[2]} (edited)"]
    projected = project_chunk(
        stale,
        1,
        document,
        f"{ALPHA}.md",
        resolve_meta_visibility(RESTRICTED_USER, fx.access_service),
        load_depmap_model(fx.dep_map_dir),
        AliasUniverse(fx.registry).snapshot(),
    )
    assert projected[1] is None


def test_admin_chunk_is_unchanged(fx: CidxMetaFixture) -> None:
    document = read_lines(fx.dep_map_dir / "payments.md")
    chunk = document[20:30]
    assert (
        project_chunk(
            chunk,
            21,
            document,
            "dependency-map/payments.md",
            resolve_meta_visibility(ADMIN_USER, fx.access_service),
            load_depmap_model(fx.dep_map_dir),
            AliasUniverse(fx.registry).snapshot(),
        )
        == chunk
    )


def test_chunk_position_must_be_positive(fx: CidxMetaFixture) -> None:
    with pytest.raises(ValueError):
        project_chunk(
            ["x"],
            0,
            ["x"],
            f"{ALPHA}.md",
            MetaVisibility.restricted([ALPHA]),
            load_depmap_model(fx.dep_map_dir),
            AliasUniverse(fx.registry).snapshot(),
        )


def test_flow_list_membership_is_recognised(tmp_path: Path) -> None:
    dep_map = tmp_path / "dependency-map"
    dep_map.mkdir()
    (dep_map / "flow-domain.md").write_text(
        f"---\ndomain: flow-domain\nparticipating_repos: [{ALPHA}, {CANARY}]\n---\n"
        "# Domain\n",
        encoding="utf-8",
    )
    assert domain_participants(dep_map, "flow-domain") == {ALPHA, CANARY}
    model = load_depmap_model(dep_map)
    assert model.participants["flow-domain"] == frozenset({ALPHA, CANARY})
    assert model.visible_domains(frozenset({ALPHA})) == frozenset({"flow-domain"})
