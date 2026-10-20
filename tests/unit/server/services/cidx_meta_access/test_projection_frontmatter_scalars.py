"""Redacted frontmatter stays valid YAML for restricted callers.

A hidden name inside a frontmatter scalar becomes a placeholder inside a
correctly quoted YAML string, so the frontmatter still parses, keeps its
other keys, and the domain is still summarised. List items naming a hidden
repository are removed, never placeholdered. Real fixture, no mocks.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.server.services.cidx_meta_access import MetaVisibility
from code_indexer.server.services.cidx_meta_access.depmap_projection import (
    RESTRICTED_PLACEHOLDER,
)
from code_indexer.server.services.cidx_meta_access.depmap_source import (
    source_for_visibility,
)
from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser
from code_indexer.server.services.dep_map_parser_tables import (
    parse_frontmatter_strict,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

DOMAIN = "refunds"
TRICKY = 'say "hi" \\ then\ttab'


def _refunds_document() -> str:
    return "\n".join(
        [
            "---",
            f"domain: {DOMAIN}",
            f"description: {CANARY} handles refunds",
            f"owner: {CANARY}",
            f'summary: "Settled by {CANARY} nightly"',
            f"motto: '{CANARY}: always on'",
            f"tricky: {json.dumps(TRICKY + ' ' + CANARY)}",
            "notes: |",
            f"  {CANARY} keeps the ledger",
            "last_analyzed: 2026-01-01T00:00:00+00:00",
            "participating_repos:",
            f"  - {ALPHA}",
            f"  - {CANARY} settlement worker",
            f"tags: [{ALPHA}, {CANARY} handles]",
            "---",
            "",
            f"# Domain Analysis: {DOMAIN}",
            "",
            f"{ALPHA} and {CANARY} share refunds.",
            "",
        ]
    )


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    fixture = build_cidx_meta_fixture(tmp_path)
    (fixture.dep_map_dir / f"{DOMAIN}.md").write_text(
        _refunds_document(), encoding="utf-8"
    )
    domains_file = fixture.dep_map_dir / "_domains.json"
    domains = json.loads(domains_file.read_text(encoding="utf-8"))
    domains.append(
        {"name": DOMAIN, "description": "Refunds", "participating_repos": [ALPHA]}
    )
    domains_file.write_text(json.dumps(domains), encoding="utf-8")
    return fixture


def _projected_frontmatter(fx: CidxMetaFixture) -> Dict[str, Any]:
    source = source_for_visibility(
        MetaVisibility.restricted([ALPHA]), fx.dep_map_dir, fx.registry
    )
    text = source.read_text(DOMAIN)
    assert CANARY not in text
    frontmatter = parse_frontmatter_strict(text)
    assert frontmatter is not None
    return frontmatter


def test_projected_frontmatter_parses_and_keeps_its_other_keys(
    fx: CidxMetaFixture,
) -> None:
    fm = _projected_frontmatter(fx)
    assert fm["domain"] == DOMAIN
    assert fm["description"] == f"{RESTRICTED_PLACEHOLDER} handles refunds"
    assert fm["summary"] == f"Settled by {RESTRICTED_PLACEHOLDER} nightly"
    assert fm["motto"] == f"{RESTRICTED_PLACEHOLDER}: always on"
    assert fm["notes"] == f"{RESTRICTED_PLACEHOLDER} keeps the ledger\n"
    assert str(fm["last_analyzed"]).startswith("2026-01-01")


def test_an_exact_alias_value_stays_a_string(fx: CidxMetaFixture) -> None:
    assert _projected_frontmatter(fx)["owner"] == RESTRICTED_PLACEHOLDER


def test_escaping_round_trips_quotes_backslashes_and_tabs(
    fx: CidxMetaFixture,
) -> None:
    assert _projected_frontmatter(fx)["tricky"] == f"{TRICKY} {RESTRICTED_PLACEHOLDER}"


def test_list_items_naming_hidden_repositories_are_removed(
    fx: CidxMetaFixture,
) -> None:
    fm = _projected_frontmatter(fx)
    assert fm["participating_repos"] == [ALPHA]
    assert fm["tags"] == [ALPHA]


def test_frontmatter_that_cannot_stay_valid_is_removed_with_a_diagnostic(
    fx: CidxMetaFixture, caplog: pytest.LogCaptureFixture
) -> None:
    (fx.dep_map_dir / "spaced.md").write_text(
        "---\ndomain: spaced\nparticipating_repos:\n"
        f"  - {ALPHA}\nrelease owner: {CANARY} team\n---\n\n"
        f"# Domain Analysis: spaced\n\n{ALPHA} body text.\n",
        encoding="utf-8",
    )
    source = source_for_visibility(
        MetaVisibility.restricted([ALPHA]), fx.dep_map_dir, fx.registry
    )
    with caplog.at_level(logging.WARNING):
        text = source.read_text("spaced")
    assert CANARY not in text
    assert "participating_repos" not in text and "---" not in text
    assert f"{ALPHA} body text." in text
    assert any(
        "spaced.md" in r.getMessage() and "malformed" in r.getMessage()
        for r in caplog.records
    )


def test_the_domain_is_still_summarised_for_a_restricted_caller(
    fx: CidxMetaFixture,
) -> None:
    source = source_for_visibility(
        MetaVisibility.restricted([ALPHA]), fx.dep_map_dir, fx.registry
    )
    summary, _anomalies = DepMapMCPParser(source=source).get_domain_summary(DOMAIN)
    assert summary is not None
    assert summary["description"] == f"{RESTRICTED_PLACEHOLDER} handles refunds"
    assert summary["name"] == DOMAIN
