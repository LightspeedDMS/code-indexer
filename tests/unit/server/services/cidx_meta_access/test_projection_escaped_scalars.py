"""A frontmatter scalar is redacted by what it DECODES to, never rewritten
while encoded.

A hidden repository name spelled with YAML escapes (hex, unicode, an
escaped hyphen), in a double-quoted or a plain scalar, never reaches a YAML
reader: neither the file door nor the shown text search judges holds it
once parsed. A scalar that needs redaction and is written with escapes,
doubled quotes or as a flow collection is removed, never re-encoded, so the
shown tokens stay a subset of the raw ones. Real fixture tree, real
projector, real file-door judgement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

import pytest
import yaml

from code_indexer.server.services.cidx_meta_access.depmap_projection import (
    DOMAIN_PLACEHOLDER,
    RESTRICTED_PLACEHOLDER,
)
from code_indexer.server.services.cidx_meta_access.file_doors import (
    judge_file_door,
)
from code_indexer.server.services.cidx_meta_access.fulltext_terms import (
    engine_tokens,
)
from code_indexer.server.services.cidx_meta_access.hit_projection import (
    open_catalog_scope,
)
from tests.shared.cidx_meta_visibility_fixture import (
    CANARY,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

PAYMENTS = "dependency-map/payments.md"
ANCHOR = "description: Payment processing across services\n"
BS = chr(92)  # built at runtime: no tool or editor can decode the escapes
FIRST, REST = CANARY[0], CANARY[1:]
HEAD, TAIL = CANARY.split("-", 1)
ESCAPED_SPELLINGS = {
    "quoted_hex": f'notes: "{BS}x{ord(FIRST):02x}{REST} service"',
    "quoted_unicode": f'notes: "{BS}u{ord(FIRST):04x}{REST} service"',
    "quoted_long_unicode": f'notes: "{BS}U{ord(FIRST):08x}{REST} service"',
    "quoted_hex_hyphen": f'notes: "{HEAD}{BS}x2d{TAIL} near"',
    "quoted_unicode_hyphen": f'notes: "{HEAD}{BS}u002d{TAIL} near"',
    "plain_hex": f"notes: {BS}x{ord(FIRST):02x}{REST} service",
    "plain_unicode": f"notes: {BS}u{ord(FIRST):04x}{REST} service",
    "plain_hex_hyphen": f"notes: {HEAD}{BS}x2d{TAIL} near",
    "tagged_scalar": f'notes: !!str "{BS}x{ord(FIRST):02x}{REST}"',
}
ESCAPED_NAME = f'"{BS}x{ord(FIRST):02x}{REST}"'
# Scalars that need redaction but are not written literally: removed.
ENCODED_NEEDING_REDACTION = {
    "escape_after_name": f'notes: "{CANARY}{BS}x41 near"',
    "doubled_quote": f"notes: 'it''s {CANARY}'",
    "flow_mapping": f"notes: {{owner: {CANARY}}}",
    "flow_map_value": f"notes: {{k: {ESCAPED_NAME}}}",
    "flow_map_key": f"notes: {{{ESCAPED_NAME}: v}}",
    "nested_flow": f"notes: {{k: [x, {{y: {ESCAPED_NAME}}}]}}",
}


def _fixture(tmp_path: Path, line: str) -> CidxMetaFixture:
    fx = build_cidx_meta_fixture(tmp_path)
    path = fx.root / PAYMENTS
    text = path.read_text(encoding="utf-8")
    assert ANCHOR in text
    path.write_text(text.replace(ANCHOR, f"{ANCHOR}{line}\n", 1), encoding="utf-8")
    return fx


def _block(lines: List[str]) -> Dict[str, Any]:
    """The parsed frontmatter ({} when it was removed whole)."""
    if lines[0] != "---":
        return {}
    parsed = yaml.safe_load("\n".join(lines[1 : lines.index("---", 1)]))
    assert isinstance(parsed, dict)
    return parsed


def _file_door(fx: CidxMetaFixture) -> List[str]:
    access = judge_file_door(
        "cidx-meta-global",
        str(fx.root),
        RESTRICTED_USER,
        fx.access_service,
        None,
        str(fx.root.parent),
        fx.registry,
    )
    raw = (fx.root / PAYMENTS).read_text(encoding="utf-8")
    return access.served_text(PAYMENTS, raw).split("\n")


def _search_view(fx: CidxMetaFixture) -> List[List[str]]:
    scope = open_catalog_scope(RESTRICTED_USER, fx.access_service, fx.root, fx.registry)
    assert scope is not None
    lines = scope.shown_lines(PAYMENTS)
    assert lines is not None
    raw, shown = lines
    return [list(raw), list(shown)]


def _tokens(lines: Iterable[str]) -> Set[str]:
    return {form for line in lines for form, _, _ in engine_tokens(line) if form}


def _assert_tokens_subset(raw: List[str], shown: List[str]) -> None:
    markers = _tokens([RESTRICTED_PLACEHOLDER, DOMAIN_PLACEHOLDER])
    extra = _tokens(shown) - _tokens(raw) - markers
    assert all(token.isdigit() for token in extra), sorted(extra)


@pytest.mark.parametrize("spelling", sorted(ESCAPED_SPELLINGS))
def test_escaped_hidden_name_never_decodes_at_any_door(
    tmp_path: Path, spelling: str
) -> None:
    fx = _fixture(tmp_path, ESCAPED_SPELLINGS[spelling])
    raw, shown = _search_view(fx)
    for door in (_file_door(fx), shown):
        assert CANARY not in json.dumps(_block(door), default=str)
        assert CANARY not in "\n".join(door)
    _assert_tokens_subset(raw, shown)


@pytest.mark.parametrize("case", sorted(ENCODED_NEEDING_REDACTION))
def test_encoded_scalar_needing_redaction_is_removed(tmp_path: Path, case: str) -> None:
    fx = _fixture(tmp_path, ENCODED_NEEDING_REDACTION[case])
    raw, shown = _search_view(fx)
    for door in (_file_door(fx), shown):
        parsed = _block(door)
        assert "notes" not in parsed, parsed
        assert "description" in parsed
        assert CANARY not in json.dumps(parsed, default=str)
    _assert_tokens_subset(raw, shown)


# Block-sequence items naming the hidden repository, however written.
LIST_ITEMS = {
    "escaped_item": ESCAPED_NAME,
    "escaped_in_text": f'"see {BS}u{ord(FIRST):04x}{REST}"',
    "quoted_item": f"'{CANARY}'",
    "plain_item": CANARY,
    "mapping_item": f"name: {ESCAPED_NAME}",
    "flow_item": f"[a, {ESCAPED_NAME}]",
}


@pytest.mark.parametrize("case", sorted(LIST_ITEMS))
def test_list_item_naming_hidden_repository_is_removed(
    tmp_path: Path, case: str
) -> None:
    fx = _fixture(tmp_path, f"tags:\n  - alpha\n  - {LIST_ITEMS[case]}")
    raw, shown = _search_view(fx)
    for door in (_file_door(fx), shown):
        parsed = _block(door)
        assert parsed.get("tags") == ["alpha"], parsed
        assert "description" in parsed
        assert CANARY not in json.dumps(parsed, default=str)
    _assert_tokens_subset(raw, shown)


def test_inline_list_drops_only_the_escaped_entry(tmp_path: Path) -> None:
    fx = _fixture(tmp_path, f"notes: [a, {ESCAPED_NAME}]")
    raw, shown = _search_view(fx)
    for door in (_file_door(fx), shown):
        parsed = _block(door)
        assert parsed.get("notes") == ["a"], parsed
        assert CANARY not in json.dumps(parsed, default=str)
    _assert_tokens_subset(raw, shown)


def test_multi_line_quoted_item_never_decodes(tmp_path: Path) -> None:
    # An escaped line break joins the two lines into the hidden name.
    split = f'  - "see {HEAD}-{BS}\n    {TAIL}"'
    fx = _fixture(tmp_path, f"tags:\n  - alpha\n{split}")
    raw, shown = _search_view(fx)
    for door in (_file_door(fx), shown):
        assert CANARY not in json.dumps(_block(door), default=str)
    _assert_tokens_subset(raw, shown)
