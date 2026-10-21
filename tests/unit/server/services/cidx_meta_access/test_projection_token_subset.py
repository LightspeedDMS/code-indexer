"""Projection outside the synthesizing set only removes text or adds the
introducible vocabulary (marker tokens and numbers).

The restricted catalog full-text page finds candidates through the engine,
which indexed the RAW text: that is complete only when every token a caller
is shown also occurs in the raw document, apart from marker tokens and
numbers. Real fixture tree, real projector and the engine's own analyzer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Set

import pytest
import yaml

from code_indexer.server.services.cidx_meta_access.depmap_projection import (
    DOMAIN_PLACEHOLDER,
    RESTRICTED_PLACEHOLDER,
)
from code_indexer.server.services.cidx_meta_access.fulltext_terms import (
    engine_tokens,
)
from code_indexer.server.services.cidx_meta_access.hit_projection import (
    CatalogScope,
    open_catalog_scope,
)
from tests.shared.cidx_meta_visibility_fixture import (
    CANARY,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

INDEX = "dependency-map/_index.md"
PAYMENTS = "dependency-map/payments.md"
HIDDEN_DOMAIN = "inventory"  # a hidden domain that is not a repository
ESCAPED = 'notes: "caf\\xe9 service"'
ESCAPED_HIDDEN = f'detail: "caf\\xe9 near {HIDDEN_DOMAIN}"'
TABBED = f'remark: "tab\there near {CANARY}"'
SUMMARY = (
    "**Summary**: 7 cross-domain edges across 9 domains. 2 standalone domains: Zq."
)
ANCHOR = "description: Payment processing across services\n"


def _tokens(lines: Iterable[str]) -> Set[str]:
    return {form for line in lines for form, _, _ in engine_tokens(line) if form}


@pytest.fixture
def fixture(tmp_path: Path) -> CidxMetaFixture:
    fx = build_cidx_meta_fixture(tmp_path)
    domain = fx.root / PAYMENTS
    text = domain.read_text(encoding="utf-8")
    assert ANCHOR in text
    text = text.replace(ANCHOR, f"{ANCHOR}{ESCAPED}\n{ESCAPED_HIDDEN}\n{TABBED}\n", 1)
    domain.write_text(text + f"\n{SUMMARY}\n", encoding="utf-8")
    return fx


def _scope(fx: CidxMetaFixture) -> CatalogScope:
    scope = open_catalog_scope(RESTRICTED_USER, fx.access_service, fx.root, fx.registry)
    assert scope is not None
    return scope


def test_projected_documents_show_only_raw_tokens_markers_and_numbers(
    fixture: CidxMetaFixture,
) -> None:
    scope = _scope(fixture)
    markers = _tokens([RESTRICTED_PLACEHOLDER, DOMAIN_PLACEHOLDER])
    projected = [
        path
        for path in sorted(scope.allowed_paths())
        if scope.shows_projection(path) and path != INDEX
    ]
    assert PAYMENTS in projected and "alpha.md" in projected
    for path in projected:
        lines = scope.shown_lines(path)
        assert lines is not None
        raw, shown = lines
        extra = _tokens(shown) - _tokens(raw) - markers
        assert all(token.isdigit() for token in extra), (path, sorted(extra))


def test_redacted_frontmatter_keeps_its_encoding_and_stays_yaml(
    fixture: CidxMetaFixture,
) -> None:
    lines = _scope(fixture).shown_lines(PAYMENTS)
    assert lines is not None
    _raw, shown = lines
    block = shown[1 : shown.index("---", 1)]
    parsed = yaml.safe_load("\n".join(block))
    assert isinstance(parsed, dict)
    assert ESCAPED in block
    assert parsed["notes"] == "caf\xe9 service"
    assert not any(line.startswith("detail:") for line in block)
    tabbed = next(line for line in block if line.startswith("remark:"))
    assert "\t" in tabbed and "\\t" not in tabbed
    assert parsed["remark"] == f"tab\there near {RESTRICTED_PLACEHOLDER}"
    assert CANARY not in "\n".join(shown) and HIDDEN_DOMAIN not in "\n".join(shown)
