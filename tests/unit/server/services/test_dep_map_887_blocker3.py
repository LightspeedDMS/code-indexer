"""
RED tests for Story #887 Blocker 3 — source_domain not normalized before incoming_claims.

collect_incoming_claims() adds raw source_domain to the frozenset without stripping
backticks or lowercasing. When outgoing uses clean "src" and incoming uses "`src`",
the frozensets never match and a false BIDIRECTIONAL_MISMATCH is emitted.

Fix: strip + lowercase source_domain BEFORE inserting into incoming_claims frozenset.
"""

from pathlib import Path

import pytest

from tests.unit.server.services.test_dep_map_887_fixtures import (
    import_hygiene_symbol,
    make_parser,
    write_domain_md_graph,
    write_domains_json,
)


@pytest.fixture
def AnomalyType():
    return import_hygiene_symbol("AnomalyType")


def _write_backtick_incoming_fixture(dep_map_dir: Path) -> None:
    """Write a matched outgoing/incoming pair where incoming source_domain is backtick-wrapped.

    src-domain has a clean outgoing row pointing to tgt-domain.
    tgt-domain has an incoming row where source_domain is "`src-domain`" (backtick-wrapped).

    After fix: the frozensets must match and NO BIDIRECTIONAL_MISMATCH emitted.
    Before fix: frozenset({"src-domain","tgt-domain"}) != frozenset({"`src-domain`","tgt-domain"})
    so a false mismatch is emitted.
    """
    write_domains_json(
        dep_map_dir,
        [
            {"name": "src-domain", "description": "d", "participating_repos": []},
            {"name": "tgt-domain", "description": "d", "participating_repos": []},
        ],
    )
    # Clean outgoing
    write_domain_md_graph(
        dep_map_dir,
        "src-domain",
        outgoing_rows=[
            {
                "this_repo": "repo-s",
                "depends_on": "repo-t",
                "target_domain": "tgt-domain",
                "dep_type": "Code-level",
            }
        ],
    )
    # Incoming with backtick-wrapped source_domain
    frontmatter = "---\nname: tgt-domain\n---\n"
    body = (
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        # source_domain backtick-wrapped — matches outgoing only after strip
        "| repo-s | repo-t | `src-domain` | Code-level | why | ev |\n"
    )
    (dep_map_dir / "tgt-domain.md").write_text(frontmatter + body, encoding="utf-8")


def _write_mixed_case_target_domain_fixture(dep_map_dir: Path) -> None:
    """Write a matched outgoing/incoming pair where the target domain_name has mixed case.

    src-domain (lowercase in JSON) has a clean outgoing row pointing to "TgtDom".
    The target domain is listed in _domains.json as "TgtDom" (mixed case).
    The incoming table in TgtDom.md uses "src-domain" (clean lowercase) as source_domain.

    After fix: apply_edge_hygiene normalizes the edge key src-domain→tgtdom, and
    collect_incoming_claims normalizes domain_name "TgtDom" to "tgtdom" before frozenset
    insertion, so frozenset({"src-domain","tgtdom"}) == frozenset({"src-domain","tgtdom"}).
    Before fix: incoming_claims has frozenset({"src-domain","TgtDom"}) which does NOT
    match the normalized edge key frozenset({"src-domain","tgtdom"}).
    """
    write_domains_json(
        dep_map_dir,
        [
            {"name": "src-domain", "description": "d", "participating_repos": []},
            {"name": "TgtDom", "description": "d", "participating_repos": []},
        ],
    )
    # Clean outgoing from src-domain pointing to TgtDom (mixed case)
    write_domain_md_graph(
        dep_map_dir,
        "src-domain",
        outgoing_rows=[
            {
                "this_repo": "repo-s",
                "depends_on": "repo-t",
                "target_domain": "TgtDom",
                "dep_type": "Code-level",
            }
        ],
    )
    # TgtDom.md — incoming table claims src-domain as source (clean lowercase)
    frontmatter = "---\nname: TgtDom\n---\n"
    body = (
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| repo-s | repo-t | src-domain | Code-level | why | ev |\n"
    )
    (dep_map_dir / "TgtDom.md").write_text(frontmatter + body, encoding="utf-8")


class TestSourceDomainNormalizationInIncomingClaims:
    """Blocker 3: source_domain AND domain_name must be normalized before insertion
    into incoming_claims.

    Without the fix on source_domain side, backtick-wrapped source_domain produces a
    non-matching frozenset → false BIDIRECTIONAL_MISMATCH.

    Without the fix on domain_name (target) side, mixed-case domain_name in _domains.json
    produces a non-matching frozenset because apply_edge_hygiene normalizes the edge key
    to lowercase but incoming_claims contains the raw mixed-case domain_name.
    """

    def test_backtick_wrapped_source_domain_in_incoming_does_not_produce_false_bidi_mismatch(
        self, tmp_path: Path, AnomalyType
    ) -> None:
        """Clean outgoing src→tgt + backtick-wrapped incoming `src`→tgt must NOT
        produce a BIDIRECTIONAL_MISMATCH anomaly.

        Without the fix, collect_incoming_claims adds "`src-domain`" raw to the
        frozenset so it never matches the outgoing frozenset {"src-domain","tgt-domain"}.
        """
        dep_map_dir = tmp_path / "dependency-map"
        dep_map_dir.mkdir()
        _write_backtick_incoming_fixture(dep_map_dir)

        _, _, _, data_anomalies = make_parser(
            tmp_path
        ).get_cross_domain_graph_with_channels()

        bidi_mismatches = [
            a for a in data_anomalies if a.type == AnomalyType.BIDIRECTIONAL_MISMATCH
        ]
        assert len(bidi_mismatches) == 0, (
            f"Expected NO BIDIRECTIONAL_MISMATCH when outgoing is clean and incoming "
            f"source_domain is backtick-wrapped (they are the same domain). "
            f"Got {len(bidi_mismatches)} mismatch(es): {bidi_mismatches}. "
            f"Blocker 3: source_domain not stripped before incoming_claims frozenset."
        )

    def test_mixed_case_domain_name_in_incoming_claims_does_not_produce_false_bidi_mismatch(
        self, tmp_path: Path, AnomalyType
    ) -> None:
        """Clean outgoing src-domain→TgtDom + incoming TgtDom.md claiming src-domain
        must NOT produce a BIDIRECTIONAL_MISMATCH anomaly.

        apply_edge_hygiene normalizes the edge key to src-domain→tgtdom (lowercase).
        Without the fix, collect_incoming_claims inserts frozenset({"src-domain","TgtDom"})
        into incoming_claims, which never matches frozenset({"src-domain","tgtdom"})
        so a false BIDIRECTIONAL_MISMATCH is emitted.
        After the fix, domain_name is normalized to "tgtdom" before frozenset insertion
        so the sets match and no mismatch is emitted.
        """
        dep_map_dir = tmp_path / "dependency-map"
        dep_map_dir.mkdir()
        _write_mixed_case_target_domain_fixture(dep_map_dir)

        _, _, _, data_anomalies = make_parser(
            tmp_path
        ).get_cross_domain_graph_with_channels()

        bidi_mismatches = [
            a for a in data_anomalies if a.type == AnomalyType.BIDIRECTIONAL_MISMATCH
        ]
        assert len(bidi_mismatches) == 0, (
            f"Expected NO BIDIRECTIONAL_MISMATCH when outgoing src-domain→TgtDom "
            f"is confirmed by TgtDom.md incoming table. "
            f"Got {len(bidi_mismatches)} mismatch(es): {bidi_mismatches}. "
            f"Blocker 3 (target side): domain_name not normalized before "
            f"incoming_claims frozenset insertion."
        )
