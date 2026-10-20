"""Projected frontmatter never carries a hidden repository name.

Exact hidden names in YAML list items are removed whatever their length
(prose redaction skips short names, list filtering does not); a hidden name
spelled through YAML escapes or a backslash line continuation never
survives the parse; a redacted scalar stays a string whatever its key. The
model and the universe are built directly (the real projection code runs).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from code_indexer.server.services.cidx_meta_access.alias_universe import (
    UniverseSnapshot,
)
from code_indexer.server.services.cidx_meta_access.depmap_model import DepMapModel
from code_indexer.server.services.cidx_meta_access.depmap_projection import (
    RESTRICTED_PLACEHOLDER,
    project_lines,
)
from code_indexer.server.services.cidx_meta_access.depmap_source import (
    source_for_visibility,
)
from code_indexer.server.services.cidx_meta_access.visibility import MetaVisibility
from code_indexer.server.services.dep_map_parser_tables import (
    parse_frontmatter_strict,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    SHORT_ALIAS,
    build_cidx_meta_fixture,
)

VISIBLE = "alpha-svc"
HIDDEN = "beta-team"
SHORT_HIDDEN = "api"
NAMES = frozenset({VISIBLE, HIDDEN, SHORT_HIDDEN})

UNIVERSE = UniverseSnapshot(1, NAMES, hashlib.sha256(b"hardening").hexdigest())
MODEL = DepMapModel(
    "hardening-fp",
    {"payments": NAMES},
    frozenset({"payments"}),
    {"payments": "payments"},
    NAMES,
    NAMES,
    (),
)
VIS = MetaVisibility.restricted([VISIBLE])


def _project(frontmatter: List[str]) -> List[Optional[str]]:
    lines = ["---", *frontmatter, "---", "", "# Domain Analysis: payments", ""]
    return project_lines(lines, "dependency-map/payments.md", VIS, MODEL, UNIVERSE)


def _parsed(frontmatter: List[str]) -> Dict[str, Any]:
    kept = [line for line in _project(frontmatter) if line is not None]
    text = "\n".join(kept) + "\n"
    parsed = parse_frontmatter_strict(text) if kept and kept[0] == "---" else {}
    decoded = json.dumps(parsed or {}, default=str)
    assert HIDDEN not in decoded
    assert f'"{SHORT_HIDDEN}"' not in decoded
    return parsed or {}


def test_a_short_hidden_alias_in_a_block_list_is_removed() -> None:
    fm = _parsed(
        [
            "domain: payments",
            "participating_repos:",
            f"  - {VISIBLE}",
            f"  - {SHORT_HIDDEN}",
        ]
    )
    assert fm["participating_repos"] == [VISIBLE]


def test_repos_analyzed_drops_short_hidden_aliases_and_the_count_matches() -> None:
    fm = _parsed(
        [
            "repos_analyzed_count: 3",
            "repos_analyzed:",
            f"  - {VISIBLE}",
            f"  - {SHORT_HIDDEN}",
            f'  - "{HIDDEN}"',
        ]
    )
    assert fm["repos_analyzed"] == [VISIBLE]
    assert fm["repos_analyzed_count"] == len(fm["repos_analyzed"])


def test_yaml_escaped_hidden_names_never_survive() -> None:
    fm = _parsed(
        [
            "domain: payments",
            'title: "be\\x74a-team owns it"',
            "participating_repos:",
            f"  - {VISIBLE}",
            '  - "be\\u0074a-team"',
        ]
    )
    assert "title" not in fm
    assert fm["participating_repos"] == [VISIBLE]
    assert fm["domain"] == "payments"


def test_a_backslash_continued_quoted_value_never_reveals_a_hidden_name() -> None:
    _parsed(["domain: payments", 'title: "the beta-\\', '  team owns it"'])


def test_shared_fixture_short_hidden_alias_never_reaches_a_restricted_reader(
    tmp_path: Path,
) -> None:
    fx = build_cidx_meta_fixture(tmp_path)
    source = source_for_visibility(
        MetaVisibility.restricted([ALPHA]), fx.dep_map_dir, fx.registry
    )
    ledger = parse_frontmatter_strict(source.read_text("ledger")) or {}
    assert ledger["participating_repos"] == [ALPHA]
    index_text = source.read_text("_index")
    index = parse_frontmatter_strict(index_text) or {}
    assert SHORT_ALIAS not in index["repos_analyzed"]
    assert index["repos_analyzed_count"] == len(index["repos_analyzed"])
    assert not any(
        line.startswith(f"| {SHORT_ALIAS} ") for line in index_text.splitlines()
    )


def test_a_redacted_scalar_under_a_dotted_key_stays_a_string() -> None:
    fm = _parsed(["domain: payments", f"owner.team: {HIDDEN} handles refunds"])
    assert fm["owner.team"] == f"{RESTRICTED_PLACEHOLDER} handles refunds"
