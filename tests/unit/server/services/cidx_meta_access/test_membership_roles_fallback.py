"""Domain membership falls back to the roles table when it is empty or missing.

Real fixture tree, real group store and registry; no mocks. Every door
(path policy, model, projected depmap source, wiki decider, Web domain
list) derives membership the same way, and visibility stays "at least one
visible participant".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from code_indexer.server.services.cidx_meta_access import (
    DecisionKind,
    MetaVisibility,
    classify,
    domain_participants,
    load_depmap_model,
    participants_reader,
)
from code_indexer.server.services.cidx_meta_access.catalog_articles import (
    catalog_decider,
)
from code_indexer.server.services.cidx_meta_access.depmap_source import (
    source_for_visibility,
)
from code_indexer.server.services.dependency_map_domain_service import (
    DependencyMapDomainService,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    GAMMA,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

DERIVED = "billing"
HIDDEN_ONLY = "vault"
NO_ROLES = "orphan"


def _roles_domain(name: str, repos: List[str]) -> str:
    rows = [f"| {repo} | Python | {name} worker |" for repo in repos]
    return "\n".join(
        [
            "---",
            f"domain: {name}",
            f"description: {name} domain",
            "participating_repos: []",
            "---",
            "",
            f"# Domain Analysis: {name}",
            "",
            "## Repository Roles",
            "",
            "| Repository | Language | Role |",
            "|---|---|---|",
            *rows,
            "| none | - | placeholder row |",
            "",
        ]
    )


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    fixture = build_cidx_meta_fixture(tmp_path)
    dep_map = fixture.dep_map_dir
    (dep_map / f"{DERIVED}.md").write_text(
        _roles_domain(DERIVED, [ALPHA, CANARY]), encoding="utf-8"
    )
    (dep_map / f"{HIDDEN_ONLY}.md").write_text(
        _roles_domain(HIDDEN_ONLY, [CANARY, GAMMA]), encoding="utf-8"
    )
    (dep_map / f"{NO_ROLES}.md").write_text(
        f"---\ndomain: {NO_ROLES}\nparticipating_repos: []\n---\n\n"
        f"# Domain Analysis: {NO_ROLES}\n\n{ALPHA} is mentioned in prose only.\n",
        encoding="utf-8",
    )
    domains_file = dep_map / "_domains.json"
    domains = json.loads(domains_file.read_text(encoding="utf-8"))
    domains += [
        {"name": DERIVED, "description": "Billing", "participating_repos": []},
        {"name": HIDDEN_ONLY, "description": "Vault"},
        {"name": NO_ROLES, "description": "Orphan", "participating_repos": []},
    ]
    domains_file.write_text(json.dumps(domains), encoding="utf-8")
    return fixture


def _kind(fx: CidxMetaFixture, domain: str) -> DecisionKind:
    vis = MetaVisibility.restricted([ALPHA])
    reader = participants_reader(fx.dep_map_dir)
    path = f"dependency-map/{domain}.md"
    return classify(path, vis, reader, lambda _m: False).kind


def test_empty_membership_is_derived_from_the_roles_table(
    fx: CidxMetaFixture,
) -> None:
    assert domain_participants(fx.dep_map_dir, DERIVED) == {ALPHA, CANARY}
    assert domain_participants(fx.dep_map_dir, HIDDEN_ONLY) == {CANARY, GAMMA}
    model = load_depmap_model(fx.dep_map_dir)
    assert model.participants[DERIVED] == frozenset({ALPHA, CANARY})
    assert DERIVED in model.visible_domains(frozenset({ALPHA}))
    assert HIDDEN_ONLY not in model.visible_domains(frozenset({ALPHA}))


def test_path_policy_shows_a_derived_domain_with_a_visible_participant(
    fx: CidxMetaFixture,
) -> None:
    assert _kind(fx, DERIVED) is DecisionKind.TRANSFORM
    assert _kind(fx, HIDDEN_ONLY) is DecisionKind.DENY


def test_no_roles_table_and_no_membership_stays_hidden_with_a_diagnostic(
    fx: CidxMetaFixture, caplog: pytest.LogCaptureFixture
) -> None:
    assert domain_participants(fx.dep_map_dir, NO_ROLES) is None
    with caplog.at_level(logging.WARNING):
        assert _kind(fx, NO_ROLES) is DecisionKind.DENY
    assert any(NO_ROLES in r.getMessage() for r in caplog.records)
    assert NO_ROLES not in load_depmap_model(fx.dep_map_dir).participants


def test_every_door_agrees_on_derived_membership(fx: CidxMetaFixture) -> None:
    vis = MetaVisibility.restricted([ALPHA])
    source = source_for_visibility(vis, fx.dep_map_dir, fx.registry)
    assert DERIVED in source.domain_names()
    assert HIDDEN_ONLY not in source.domain_names()
    assert NO_ROLES not in source.domain_names()
    entries = {e["name"]: e for e in source.load_domains_json()}
    assert entries[DERIVED]["participating_repos"] == [ALPHA]
    assert HIDDEN_ONLY not in entries and NO_ROLES not in entries
    assert CANARY not in source.read_text(DERIVED)

    decide = catalog_decider(fx.root, vis, fx.access_service)
    assert decide(f"dependency-map/{DERIVED}.md").kind is DecisionKind.TRANSFORM
    assert decide(f"dependency-map/{HIDDEN_ONLY}.md").kind is DecisionKind.DENY

    service = DependencyMapDomainService(
        SimpleNamespace(cidx_meta_read_path=fx.root), None, source=source
    )
    listed = service.get_domain_list(accessible_repos=set(vis.visible))
    by_name = {d["name"]: d for d in listed["domains"]}
    assert by_name[DERIVED]["participating_repos"] == [ALPHA]
    assert HIDDEN_ONLY not in by_name and NO_ROLES not in by_name


def test_case_differences_do_not_split_the_path_policy_from_the_model(
    fx: CidxMetaFixture,
) -> None:
    # Roles-derived names are compared like the model compares them.
    vis = MetaVisibility.restricted([ALPHA.upper()])
    reader = participants_reader(fx.dep_map_dir)
    decision = classify(f"dependency-map/{DERIVED}.md", vis, reader, lambda _m: False)
    model = load_depmap_model(fx.dep_map_dir)
    assert DERIVED in model.visible_domains(frozenset({ALPHA}))
    assert decision.kind is DecisionKind.TRANSFORM
