"""Recognised dependency-map tables in the formats the generator emits.

Real domain files, a real ``_index.md`` from IndexRegenerator, a static
registry rows source standing in for the cluster registry. Neutral names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.services.cidx_meta_access import (
    AliasUniverse,
    MetaVisibility,
    load_depmap_model,
    project_lines,
)
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator

VISIBLE_A = "alpha-svc"
VISIBLE_B = "delta-svc"
HIDDEN_A = "beta-svc"
HIDDEN_B = "gamma-svc"
ROLES_HEADER = "| Repo | Language | Role within domain |"
REPO = "[restricted repository]"
DOMAIN = "[restricted domain]"

ROLES_HEADER_VARIANTS = [
    "| Repo | Language | Role within domain |",
    "| repo | language | role within domain |",
    "| Repository | Language | Role within domain |",
    "| Repo | Language | Role |",
    "| Repository | Role | Verified Stack |",
]


class StaticRegistry:
    """The registry's ``list_repos()`` rows (the cluster registry is an
    external store)."""

    def __init__(self, repos: List[str]) -> None:
        self._rows = {f"{r}-global": {"repo_name": r} for r in repos}

    def list_repos(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._rows)


def _domain(name: str, repos: List[str], body: List[str]) -> str:
    head = ["---", f"domain: {name}", f"description: {name} domain"]
    head += ["participating_repos:"] + [f"- {r}" for r in repos] + ["---", ""]
    return "\n".join(head + [f"# Domain Analysis: {name}", ""] + body) + "\n"


def _payments_body(roles_header: str) -> List[str]:
    width = roles_header.count("|") - 1
    pad = " | x" * (width - 3)
    return [
        "## Overview",
        f"The payments domain holds {VISIBLE_A} and {HIDDEN_A}. Each repo is small.",
        "Lookups go through the identity domain first.",
        "",
        "## Repository Roles",
        roles_header,
        "|" + "---|" * width,
        f"| {VISIBLE_A} | Python | API{pad} |",
        f"| {HIDDEN_A} | Go | ledger{pad} |",
        "",
        "## Cross-Domain Connections",
        "",
        "### Outgoing Dependencies",
        "",
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |",
        "|---|---|---|---|---|---|",
        f"| {VISIBLE_A} | {HIDDEN_B} | identity | Service integration | tokens | a.py |",
        f"| {VISIBLE_A} | {VISIBLE_B} | reporting | Code-level "
        f"| {HIDDEN_A} events are aggregated by {VISIBLE_B} | src/x.py |",
        f"| {VISIBLE_A} | {VISIBLE_B} | archive | Data contracts | nightly | a.sql |",
        f"| {HIDDEN_A} (worker) | {VISIBLE_B} | reporting | Code-level | feeds | b.py |",
        "",
        "### Incoming Dependencies",
        "",
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |",
        "|---|---|---|---|---|---|",
        f"| {VISIBLE_B} | {VISIBLE_A} | reporting | Code-level "
        f"| {HIDDEN_B} relays the calls | r.py |",
    ]


def build_real_format_map(root: Path, roles_header: str = ROLES_HEADER) -> Path:
    """payments (visible + hidden repo), identity (hidden), reporting and
    archive (visible); a hidden repository is also named ``archive``."""
    dep_map = root / "dependency-map"
    dep_map.mkdir(parents=True)
    files = {
        "payments": ([VISIBLE_A, HIDDEN_A], _payments_body(roles_header)),
        "identity": ([HIDDEN_B], [f"{HIDDEN_B} issues tokens."]),
        "reporting": ([VISIBLE_B, VISIBLE_A], [f"{VISIBLE_B} builds reports."]),
        "archive": ([VISIBLE_B], ["Cold storage."]),
    }
    domains = []
    for name, (repos, body) in files.items():
        (dep_map / f"{name}.md").write_text(_domain(name, repos, body), "utf-8")
        description = f"{name} domain"
        if name == "reporting":
            description = f"{HIDDEN_A} feeds the reports"
        domains.append(
            {"name": name, "description": description, "participating_repos": repos}
        )
    (dep_map / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    IndexRegenerator().regenerate(dep_map)
    return dep_map


def registry() -> StaticRegistry:
    return StaticRegistry([VISIBLE_A, VISIBLE_B, HIDDEN_A, HIDDEN_B, "archive"])


def restricted() -> MetaVisibility:
    return MetaVisibility.restricted([VISIBLE_A, VISIBLE_B])


def project(
    dep_map: Path, name: str, lines: Optional[List[str]] = None
) -> List[Optional[str]]:
    source = lines or (dep_map / name).read_text(encoding="utf-8").splitlines()
    return project_lines(
        source,
        f"dependency-map/{name}",
        restricted(),
        load_depmap_model(dep_map),
        AliasUniverse(registry()).snapshot(),
    )


def kept(projected: List[Optional[str]]) -> List[str]:
    return [line for line in projected if line is not None]


@pytest.mark.parametrize("header", ROLES_HEADER_VARIANTS)
def test_roles_header_variants_survive_and_prose_repo_is_untouched(
    tmp_path: Path, header: str
) -> None:
    dep_map = build_real_format_map(tmp_path, header)
    model = load_depmap_model(dep_map)
    assert "repo" not in model.structured_names
    assert "repository" not in model.structured_names

    out = kept(project(dep_map, "payments.md"))
    assert header in out
    assert f"The payments domain holds {VISIBLE_A} and {REPO}. Each repo is small." in (
        out
    )
    assert any(line.startswith(f"| {VISIBLE_A} | Python | API") for line in out)
    assert not any(HIDDEN_A in line for line in out)
    assert "| This Repo | Depends On | Target Domain | Type | Why | Evidence |" in out
    assert "| External Repo | Depends On | Source Domain | Type | Why | Evidence |" in (
        out
    )


def _rows(lines: List[str], header_prefix: str) -> List[List[str]]:
    start = next(i for i, line in enumerate(lines) if line.startswith(header_prefix))
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip()[1:-1].split("|")])
    return rows


def test_free_text_cells_never_drop_a_visible_row(tmp_path: Path) -> None:
    dep_map = build_real_format_map(tmp_path)
    payments = kept(project(dep_map, "payments.md"))
    assert (
        f"| {VISIBLE_A} | {VISIBLE_B} | reporting | Code-level "
        f"| {REPO} events are aggregated by {VISIBLE_B} | src/x.py |"
    ) in payments
    assert (
        f"| {VISIBLE_B} | {VISIBLE_A} | reporting | Code-level "
        f"| {REPO} relays the calls | r.py |"
    ) in payments
    assert not any("(worker)" in line for line in payments)
    assert not any("| identity |" in line for line in payments)

    index = kept(project(dep_map, "_index.md"))
    assert f"| reporting | {REPO} feeds the reports | 2 |" in index


def test_hidden_domain_names_are_marked_as_domains(tmp_path: Path) -> None:
    dep_map = build_real_format_map(tmp_path)
    lines = [
        "Lookups go through the identity domain first.",
        "Cold data moves to archive nightly.",
        f"{HIDDEN_B} issues tokens.",
    ]
    assert project(dep_map, "payments.md", lines) == [
        f"Lookups go through the {DOMAIN} domain first.",
        "Cold data moves to archive nightly.",
        f"{REPO} issues tokens.",
    ]
    payments = kept(project(dep_map, "payments.md"))
    assert (
        f"| {VISIBLE_A} | {VISIBLE_B} | archive | Data contracts | nightly | a.sql |"
    ) in payments
    assert f"Lookups go through the {DOMAIN} domain first." in payments


PLACEHOLDER_WORDS = ["none", "External", "n/a", "-", "empty"]


def test_placeholder_words_are_never_harvested(tmp_path: Path) -> None:
    dep_map = build_real_format_map(tmp_path)
    payments = dep_map / "payments.md"
    rows = [
        f"| {VISIBLE_A} | {word} | reporting | Code-level | none needed | x.py |"
        for word in PLACEHOLDER_WORDS
    ]
    marker = "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
    marker += "|---|---|---|---|---|---|\n"
    text = payments.read_text(encoding="utf-8")
    payments.write_text(text.replace(marker, marker + "\n".join(rows) + "\n"), "utf-8")

    model = load_depmap_model(dep_map)
    normalised = {word.lower() for word in PLACEHOLDER_WORDS}
    assert not normalised & model.structured_names
    out = kept(project(dep_map, "payments.md"))
    for row in rows:
        assert row in out
    prose = ["None of the external callers keep empty state."]
    assert project(dep_map, "payments.md", prose) == prose


def test_index_counts_equal_the_rows_kept(tmp_path: Path) -> None:
    dep_map = build_real_format_map(tmp_path)
    index = kept(project(dep_map, "_index.md"))
    catalog = _rows(index, "| Domain | Description | Repo Count |")
    matrix = _rows(index, "| Repository | Domain |")
    graph = _rows(index, "| Source Domain | Target Domain | Via Repos |")

    assert sorted(row[0] for row in catalog) == ["archive", "payments", "reporting"]
    assert f"domains_count: {len(catalog)}" in index
    for domain, _description, count in catalog:
        assert int(count) == sum(1 for row in matrix if row[1] == domain), domain
    assert f"repos_analyzed_count: {len({row[0] for row in matrix})}" in index
    summary = next(line for line in index if line.startswith("**Summary**"))
    assert summary.startswith(f"**Summary**: {len(graph)} cross-domain edges")
    assert {(row[0], row[1]) for row in graph} == {
        ("payments", "reporting"),
        ("payments", "archive"),
    }


@pytest.mark.parametrize("header", ROLES_HEADER_VARIANTS)
@pytest.mark.parametrize("name", ["payments.md", "_index.md"])
def test_chunk_never_exposes_more_than_the_document_real_formats(
    tmp_path: Path, header: str, name: str
) -> None:
    from code_indexer.server.services.cidx_meta_access import project_chunk

    dep_map = build_real_format_map(tmp_path, header)
    document = (dep_map / name).read_text(encoding="utf-8").splitlines()
    rel = f"dependency-map/{name}"
    model = load_depmap_model(dep_map)
    universe = AliasUniverse(registry()).snapshot()
    full = project_lines(document, rel, restricted(), model, universe)
    for start in range(len(document)):
        for end in range(start + 1, len(document) + 1):
            chunk = project_chunk(
                document[start:end],
                start + 1,
                document,
                rel,
                restricted(),
                model,
                universe,
            )
            assert len(chunk) == end - start
            for offset, line in enumerate(chunk):
                assert line is None or line == full[start + offset], (start, end)


def test_graph_row_with_only_hidden_via_repos_is_removed_and_not_counted(
    tmp_path: Path,
) -> None:
    dep_map = build_real_format_map(tmp_path)
    index_file = dep_map / "_index.md"
    header = "| Source Domain | Target Domain | Via Repos | Type | Why |\n|---|---|---|---|---|\n"
    extra = f"| reporting | archive | {HIDDEN_A} | Code-level | archives |\n"
    text = index_file.read_text(encoding="utf-8")
    assert header in text
    index_file.write_text(text.replace(header, header + extra), encoding="utf-8")

    index = kept(project(dep_map, "_index.md"))
    graph = _rows(index, "| Source Domain | Target Domain | Via Repos |")
    assert ("reporting", "archive") not in {(row[0], row[1]) for row in graph}
    summary = next(line for line in index if line.startswith("**Summary**"))
    assert summary.startswith(f"**Summary**: {len(graph)} cross-domain edges")


def test_inline_yaml_list_drops_hidden_entries(tmp_path: Path) -> None:
    dep_map = build_real_format_map(tmp_path)
    line = f"repos_analyzed: [{VISIBLE_A}, {HIDDEN_A}, {VISIBLE_B}]"
    assert project(dep_map, "_index.md", [line]) == [
        f"repos_analyzed: [{VISIBLE_A}, {VISIBLE_B}]"
    ]


def test_absent_dependency_map_yields_the_empty_model(tmp_path: Path) -> None:
    model = load_depmap_model(tmp_path / "no-such-dependency-map")
    assert model.fingerprint == "absent"
    assert not model.domains and not model.structured_names
