"""Link destinations, image sources, autolinks, link titles and reference
definitions are judged after decoding.

A Markdown renderer shows a link's TEXT, but a reader follows (and a client
decodes) its target. A hidden repository name spelled there with percent
escapes, HTML entities or both never reaches a caller: the line is removed
at the file door and from the text search judges, and the rest of the
document stays visible. Real fixture tree, real projector, real file-door
judgement.
"""

from __future__ import annotations

import html
import urllib.parse
from pathlib import Path
from typing import Iterable, List, Set

import pytest

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
    ALPHA,
    CANARY,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

DESCRIPTION = f"{ALPHA}.md"
BS = chr(92)  # built at runtime: no tool or editor can decode the escapes
FIRST, REST = CANARY[0], CANARY[1:]
PCT = f"%{ord(FIRST):02X}{REST}"  # percent-encoded first character
ENT = f"&#{ord(FIRST)};{REST}"  # HTML-entity first character
ENCODED_LINKS = {
    "link_percent": f"see [docs](https://example.com/{PCT})",
    "link_entity": f"see [docs](https://example.com/{ENT})",
    "image_percent": f"![logo](img/{PCT}.png)",
    "autolink_percent": f"<https://example.com/{PCT}>",
    "link_title": f'see [docs](https://example.com/a "{PCT}")',
    "reference_definition": f"[ref]: https://example.com/{PCT}",
    "entity_in_percent": f"see [docs](https://example.com/%26%23{ord(FIRST)};{REST})",
    "percent_in_entity": f"see [docs](https://example.com/&#37;{ord(FIRST):02X}{REST})",
    "balanced_paren": f"[docs](https://example.com/(x){PCT})",
    "balanced_paren_inner": f"see [docs](https://example.com/a(b){PCT})",
    "nested_paren": f"see [docs](https://example.com/((x)y){PCT}) here",
    "escaped_paren": f"see [docs](https://example.com/a{BS})/{PCT}) here",
    "angle_destination": f"see [x](<https://example.com/a b/{PCT}>) here",
    "title_with_parens": f'see [docs](https://example.com/a "(t) {PCT}") here',
    "double_percent": f"see [docs](https://example.com/%25{ord(FIRST):02X}{REST})",
    "prose_percent": f"talks to {PCT} daily",
    "prose_entity": f"talks to {ENT} daily",
}


def _decoded(text: str) -> str:
    """*text* after repeated HTML-entity, percent and backslash-escape
    decoding."""
    for _ in range(4):
        decoded = urllib.parse.unquote(html.unescape(text)).replace(BS, "")
        if decoded == text:
            break
        text = decoded
    return text


def _fixture(tmp_path: Path, line: str) -> CidxMetaFixture:
    fx = build_cidx_meta_fixture(tmp_path)
    path = fx.root / DESCRIPTION
    path.write_text(path.read_text(encoding="utf-8") + f"\n{line}\n", encoding="utf-8")
    return fx


def _file_door(fx: CidxMetaFixture) -> str:
    access = judge_file_door(
        "cidx-meta-global",
        str(fx.root),
        RESTRICTED_USER,
        fx.access_service,
        None,
        str(fx.root.parent),
        fx.registry,
    )
    raw = (fx.root / DESCRIPTION).read_text(encoding="utf-8")
    return access.served_text(DESCRIPTION, raw)


def _tokens(lines: Iterable[str]) -> Set[str]:
    return {form for line in lines for form, _, _ in engine_tokens(line) if form}


@pytest.mark.parametrize("case", sorted(ENCODED_LINKS))
def test_encoded_link_part_naming_hidden_repository_is_removed(
    tmp_path: Path, case: str
) -> None:
    fx = _fixture(tmp_path, ENCODED_LINKS[case])
    scope = open_catalog_scope(RESTRICTED_USER, fx.access_service, fx.root, fx.registry)
    assert scope is not None
    lines = scope.shown_lines(DESCRIPTION)
    assert lines is not None
    raw: List[str] = list(lines[0])
    shown: List[str] = list(lines[1])
    for text in (_file_door(fx), "\n".join(shown)):
        assert CANARY not in _decoded(text)
        assert f"# {ALPHA}\n" in text
    markers = _tokens([RESTRICTED_PLACEHOLDER, DOMAIN_PLACEHOLDER])
    extra = _tokens(shown) - _tokens(raw) - markers
    assert all(token.isdigit() for token in extra), sorted(extra)
