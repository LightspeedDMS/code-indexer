"""Line projection of dependency-map and description documents (per user).

Real files from the shared fixture, real _index.md from IndexRegenerator,
real SQLite registry for the alias universe. Neutral names only.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import pytest

from code_indexer.server.services.cidx_meta_access import (
    AliasUniverse,
    MetaVisibility,
    load_depmap_model,
    project_lines,
    project_meta_document,
    render_for_display,
    render_line_aligned,
    resolve_meta_visibility,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    PAYMENTS_NOTES_HEADER_LINE,
    RESTRICTED_USER,
    SHORT_ALIAS,
    UNIVERSE_ONLY_ALIAS,
    CidxMetaFixture,
    build_cidx_meta_fixture,
    read_lines,
)

PLACEHOLDER = "[restricted repository]"


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path_factory.mktemp("projection"))


def _project(
    fx: CidxMetaFixture,
    rel: str,
    vis: MetaVisibility,
    lines: Optional[List[str]] = None,
) -> List[Optional[str]]:
    source = lines if lines is not None else read_lines(fx.root / rel)
    return project_lines(
        source,
        rel,
        vis,
        load_depmap_model(fx.dep_map_dir),
        AliasUniverse(fx.registry).snapshot(),
    )


def _restricted(fx: CidxMetaFixture) -> MetaVisibility:
    return resolve_meta_visibility(RESTRICTED_USER, fx.access_service)


def _text(projected: List[Optional[str]]) -> str:
    return render_line_aligned(projected)


DOCUMENTS = [
    "dependency-map/payments.md",
    "dependency-map/ledger.md",
    "dependency-map/reports.md",
    "dependency-map/_index.md",
    f"{ALPHA}.md",
]


@pytest.mark.parametrize("rel", DOCUMENTS)
def test_projection_is_line_aligned(fx: CidxMetaFixture, rel: str) -> None:
    lines = read_lines(fx.root / rel)
    projected = _project(fx, rel, _restricted(fx))
    assert len(projected) == len(lines)
    assert len(render_line_aligned(projected).split("\n")) == len(lines)


def test_restricted_view_of_domain_file_contains_only_visible_repos(
    fx: CidxMetaFixture,
) -> None:
    lines = read_lines(fx.dep_map_dir / "payments.md")
    projected = _project(fx, "dependency-map/payments.md", _restricted(fx))
    by_line = dict(zip(lines, projected))

    assert by_line[f"| {CANARY} | Go | Settlement worker |"] is None
    outgoing = next(
        line for line in lines if line.startswith(f"| {ALPHA} | {CANARY} |")
    )
    assert by_line[outgoing] is None
    assert by_line[f"| {ALPHA} | Python | Payment API |"] == (
        f"| {ALPHA} | Python | Payment API |"
    )
    assert by_line[f"  - {CANARY}"] is None
    assert by_line[f"  - {ALPHA}"] == f"  - {ALPHA}"

    text = _text(projected)
    assert CANARY not in text
    assert GAMMA not in text
    assert f"| {ALPHA} | {ALPHA} | ledger |" in text


def test_index_counts_are_recomputed_per_user(fx: CidxMetaFixture) -> None:
    lines = read_lines(fx.dep_map_dir / "_index.md")
    projected = _project(fx, "dependency-map/_index.md", _restricted(fx))
    text = _text(projected)

    assert "repos_analyzed_count: 1" in text
    assert "domains_count: 3" in text
    assert "| payments | Payments | 1 |" in text
    assert "| ledger | Ledger | 1 |" in text
    assert "| payments | ledger | alpha | Code-level | posts ledger entries |" in text
    assert (
        "**Summary**: 1 cross-domain edges across 3 domains. "
        "1 standalone domains: reports."
    ) in text
    assert f"| {ALPHA} | payments |" in text
    for hidden in (CANARY, GAMMA, "inventory"):
        assert hidden not in text
    assert "repos_analyzed_count: 3" in lines[3]  # source untouched


def test_admin_index_view_keeps_full_counts(fx: CidxMetaFixture) -> None:
    lines = read_lines(fx.dep_map_dir / "_index.md")
    admin = resolve_meta_visibility(ADMIN_USER, fx.access_service)
    assert _project(fx, "dependency-map/_index.md", admin) == lines


def test_prose_aliases_are_replaced_on_word_boundaries(fx: CidxMetaFixture) -> None:
    text = _text(_project(fx, f"{ALPHA}.md", _restricted(fx)))
    assert f"{ALPHA} talks to {PLACEHOLDER} and {PLACEHOLDER} over HTTP." in text
    assert f"It mirrors {PLACEHOLDER} nightly." in text
    assert UNIVERSE_ONLY_ALIAS not in text
    assert f"The {SHORT_ALIAS} adapter is optional." in text

    canary_only = MetaVisibility.restricted([CANARY])
    other = _text(_project(fx, f"{CANARY}.md", canary_only))
    assert f"Unlike {PLACEHOLDER}, it keeps an alphabet of flags." in other


def test_global_suffix_and_links_are_redacted(fx: CidxMetaFixture) -> None:
    lines = [
        f"See `{CANARY}-global` and [{CANARY}]({CANARY}.md).",
        f"{CANARY}-client is a different repository.",
    ]
    projected = _project(fx, f"{ALPHA}.md", _restricted(fx), lines)
    assert projected[0] == (
        f"See `{PLACEHOLDER}` and [{PLACEHOLDER}]({PLACEHOLDER}.md)."
    )
    assert projected[1] == lines[1]


def test_chunk_projects_like_the_same_lines_of_the_document(
    fx: CidxMetaFixture,
) -> None:
    from code_indexer.server.services.cidx_meta_access import project_chunk

    rel = "dependency-map/payments.md"
    lines = read_lines(fx.dep_map_dir / "payments.md")
    assert PAYMENTS_NOTES_HEADER_LINE < 40
    first_row = lines.index("| Repository | Language | Role |") + 2
    chunk = lines[first_row : first_row + 3]
    whole = _project(fx, rel, _restricted(fx))
    projected = project_chunk(
        chunk,
        first_row + 1,
        lines,
        rel,
        _restricted(fx),
        load_depmap_model(fx.dep_map_dir),
        AliasUniverse(fx.registry).snapshot(),
    )
    assert projected == whole[first_row : first_row + 3]
    assert projected[0] == f"| {ALPHA} | Python | Payment API |"
    assert projected[1] is None
    assert _project(fx, rel, _restricted(fx), chunk)[:2] == [None, None]


class _RefusingRegistry:
    def list_repos(self) -> dict:
        raise AssertionError("an admin projection must not read the registry")


def test_unrestricted_view_is_identity_without_parsing(
    fx: CidxMetaFixture, tmp_path: Path
) -> None:
    not_a_directory = tmp_path / "file-not-dir"
    not_a_directory.write_text("x", encoding="utf-8")
    lines = read_lines(fx.dep_map_dir / "payments.md")
    admin = resolve_meta_visibility(ADMIN_USER, fx.access_service)
    projected = project_meta_document(
        lines,
        "dependency-map/payments.md",
        admin,
        dep_map_dir=not_a_directory,
        universe=AliasUniverse(_RefusingRegistry()),
    )
    assert projected == lines


def test_restricted_document_projection_uses_model_and_universe(
    fx: CidxMetaFixture,
) -> None:
    lines = read_lines(fx.root / f"{ALPHA}.md")
    projected = project_meta_document(
        lines,
        f"{ALPHA}.md",
        _restricted(fx),
        dep_map_dir=fx.dep_map_dir,
        universe=AliasUniverse(fx.registry),
    )
    assert CANARY not in _text(projected)
    assert UNIVERSE_ONLY_ALIAS not in _text(projected)


def test_request_projector_loads_the_model_once(tmp_path: Path) -> None:
    from code_indexer.server.services.cidx_meta_access import MetaProjector

    fx = build_cidx_meta_fixture(tmp_path)
    vis = _restricted(fx)
    rel = "dependency-map/_index.md"
    projector = MetaProjector(
        vis, dep_map_dir=fx.dep_map_dir, universe=AliasUniverse(fx.registry)
    )
    index = read_lines(fx.dep_map_dir / "_index.md")
    before = projector.project(index, rel)
    assert "domains_count: 3" in _text(before)

    ledger = fx.dep_map_dir / "ledger.md"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace(
            f"  - {ALPHA}\n", f"  - {CANARY}\n", 1
        ),
        encoding="utf-8",
    )
    assert projector.project(index, rel) == before
    assert projector.project_chunk(index[:8], 1, index, rel) == before[:8]
    fresh = project_meta_document(
        index, rel, vis, dep_map_dir=fx.dep_map_dir, universe=AliasUniverse(fx.registry)
    )
    assert "domains_count: 2" in _text(fresh)


def test_unrestricted_request_projector_does_no_io(
    fx: CidxMetaFixture, tmp_path: Path
) -> None:
    from code_indexer.server.services.cidx_meta_access import MetaProjector

    not_a_directory = tmp_path / "file-not-dir"
    not_a_directory.write_text("x", encoding="utf-8")
    admin = resolve_meta_visibility(ADMIN_USER, fx.access_service)
    projector = MetaProjector(
        admin, dep_map_dir=not_a_directory, universe=AliasUniverse(_RefusingRegistry())
    )
    lines = read_lines(fx.dep_map_dir / "payments.md")
    assert projector.project(lines, "dependency-map/payments.md") == lines
    assert projector.project_chunk(lines[:3], 1, lines, "x.md") == lines[:3]
    with pytest.raises(ValueError):
        projector.project_chunk(lines[:3], 0, lines, "x.md")


def test_malformed_structures_are_removed_for_restricted_view(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fx = build_cidx_meta_fixture(tmp_path)
    for name in ("ledger.md", "reports.md"):
        path = fx.dep_map_dir / name
        unique = f"Notes for {tmp_path.name}.\n"
        path.write_text(path.read_text(encoding="utf-8") + unique, encoding="utf-8")
    ledger = read_lines(fx.dep_map_dir / "ledger.md")
    reports = read_lines(fx.dep_map_dir / "reports.md")
    with caplog.at_level(logging.WARNING):
        projected_ledger = _project(fx, "dependency-map/ledger.md", _restricted(fx))
        projected_again = _project(fx, "dependency-map/ledger.md", _restricted(fx))
        projected_reports = _project(fx, "dependency-map/reports.md", _restricted(fx))

    assert projected_ledger == projected_again
    by_line = dict(zip(ledger, projected_ledger))
    assert by_line[f"| {ALPHA} | Python | Ledger writer |"] is not None
    assert by_line[f"| {ALPHA} | Python | Ledger | extra |"] is None
    assert by_line[f"| {CANARY} | Go |"] is None

    closing = reports.index("---", 1)
    assert projected_reports[: closing + 1] == [None] * (closing + 1)
    assert f"{ALPHA} renders reports." in _text(projected_reports)

    ledger_warnings = [r for r in caplog.records if "ledger.md" in r.getMessage()]
    reports_warnings = [r for r in caplog.records if "reports.md" in r.getMessage()]
    assert len(ledger_warnings) == 1
    assert len(reports_warnings) == 1
    diagnostics = [r for r in caplog.records if r.name.endswith("depmap_projection")]
    assert diagnostics and all(CANARY not in r.getMessage() for r in diagnostics)


def test_display_rendering_keeps_tables_contiguous(fx: CidxMetaFixture) -> None:
    projected = _project(fx, "dependency-map/payments.md", _restricted(fx))
    rendered = render_for_display(projected).split("\n")
    roles = rendered.index("| Repository | Language | Role |")
    assert rendered[roles : roles + 4] == [
        "| Repository | Language | Role |",
        "|---|---|---|",
        f"| {ALPHA} | Python | Payment API |",
        "",
    ]
    assert rendered.count("") == sum(1 for line in projected if line == "")
    assert len(render_line_aligned(projected).split("\n")) == len(projected)
