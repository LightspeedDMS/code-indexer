"""No decodable or normalisable form of a hidden name survives projection.

Every Markdown construct and frontmatter form below carries the hidden
repository name in one of many encodings (percent escapes up to six layers
deep, HTML entities, YAML escapes, backslashes, mixed, full-width forms).
The projected document, its full decoding (to a fixpoint, NFKC-normalised,
HTML comments removed), its Markdown rendering and its parsed frontmatter
never hold the name; the frontmatter still parses; every shown token is a
raw token, a marker token or a number; and visible lines are kept. Real
fixture tree and real projector; the rendering is markdown-it.
"""

from __future__ import annotations

import html
import re
import unicodedata
import urllib.parse
from typing import Iterator, List, Optional, Tuple

import pytest
import yaml
from markdown_it import MarkdownIt

from code_indexer.server.services.cidx_meta_access import MetaVisibility
from code_indexer.server.services.cidx_meta_access.depmap_projection import (
    DOMAIN_PLACEHOLDER,
    RESTRICTED_PLACEHOLDER,
    MetaProjector,
    render_line_aligned,
)
from code_indexer.server.services.cidx_meta_access.fulltext_terms import (
    engine_tokens,
)
from code_indexer.server.services.cidx_meta_access.universe_provider import (
    process_alias_universe,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    build_cidx_meta_fixture,
)

BS = chr(92)  # built at runtime: no tool or editor can decode the escapes
FIRST, REST = CANARY[0], CANARY[1:]
HEX = f"{ord(FIRST):02x}"
X = f"{BS}x{HEX}{REST}"
FORMS = {
    "plain": CANARY,
    "percent": f"%{HEX}{REST}",
    "double_percent": f"%25{HEX}{REST}",
    "entity": f"&#{ord(FIRST)};{REST}",
    "entity_no_semicolon": f"&#{ord(FIRST)}{REST}",
    "hex_entity": f"&#x{HEX};{REST}",
    "yaml_x": X,
    "yaml_u": f"{BS}u00{HEX}{REST}",
    "backslash": f"{CANARY[:2]}{BS}{CANARY[2:]}",
    "mixed_entity_percent": f"&#37;{HEX}{REST}",
    "mixed_percent_entity": f"%26%23{ord(FIRST)};{REST}",
    "five_percent": f"%25252525{HEX}{REST}",
    "six_percent": f"%2525252525{HEX}{REST}",
    "fullwidth": "".join(
        chr(ord(c) + 0xFEE0) if c.isascii() and c.isalnum() else c for c in CANARY
    ),
}
PATTERNS = {
    "inline": "[docs](https://example.com/{n})",
    "image": "![logo](https://example.com/{n}.png)",
    "autolink": "<https://example.com/{n}>",
    "title_double": '[docs](https://example.com/ "{n}")',
    "title_single": "[docs](https://example.com/ '{n}')",
    "title_paren": "[docs](https://example.com/ ({n}))",
    "reference": "[docs][ref]\n\n[ref]: https://example.com/{n}",
    "reference_multiline": '[docs][ref]\n\n[ref]:\n  https://example.com/{n}\n  "t"',
    "angle": "[docs](<https://example.com/{n}>)",
    "html_a": '<a href="https://example.com/{n}">docs</a>',
    "balanced": "[docs](https://example.com/(x){n})",
    "table": "| docs | [link](https://example.com/{n}) |\n|---|---|",
    "blockquote": "> [docs](https://example.com/{n})",
    "list": "- [docs](https://example.com/{n})",
}
FRONTMATTER = {
    "scalar": 'note: "{n}"',
    "list": 'items:\n  - alpha\n  - "{n}"',
    "flow": 'items: [alpha, "{n}"]',
    "block_literal": "note: |\n  {n}",
    "block_folded": "note: >\n  {n}",
    "flow_multiline": 'items: [alpha,\n  "{n}"]',
    "plain_same_indent": "note: alpha\n{n}",
    "same_indent_list": "items:\n- {n}",
}
SPECIAL = {
    "orphan_continuation": (f'note: "{CANARY}\n  {X}"\nother: kept', True),
    "flow_same_indent": (f"items: [alpha,\n%{HEX}{REST}]\nother: kept", True),
    "flow_deeper": (f"items: [alpha,\n  %{HEX}{REST}]\nother: kept", True),
    "block_comment": (f"note: |\n  {CANARY}\n  # c\n  {X}\nother: kept", True),
    "pass_bound_bare_url": (f"see https://example.com/%25252525{HEX}{REST}", False),
    "fullwidth_prose": (f"talks to {FORMS['fullwidth']} daily", False),
    "comment_split_lines": (f"talks to {CANARY[:6]}<!--\n-->{CANARY[6:]} daily", False),
    "comment_inline": (f"talks to {CANARY[:6]}<!-- x -->{CANARY[6:]} daily", False),
    "comment_unclosed": (f"talks <!-- open\n{CANARY[:6]}\n{CANARY[6:]}", False),
}
# Visible lines that must be shown unchanged (no over-redaction).
VISIBLE = {
    "visible_percent_url": "[docs](https://example.com/%61lpha)",
    "accented": "Café works with alpha",
    "visible_link": "[alpha](https://example.com/alpha)",
    "code_backslash": f"Use `path{BS}name` in code",
    "visible_comment": "alpha <!-- note --> visible",
}
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_YAML_HEX = re.compile(r"\\(?:x([0-9A-Fa-f]{2})|u([0-9A-Fa-f]{4}))")
MARKERS = {
    form for m in (RESTRICTED_PLACEHOLDER, DOMAIN_PLACEHOLDER) for form, _, _ in
    engine_tokens(m) if form
}  # fmt: skip


def _cases() -> Iterator[Tuple[str, str, bool]]:
    for kind, pattern in PATTERNS.items():
        for encoding, name in FORMS.items():
            yield f"{kind}/{encoding}", pattern.format(n=name), False
    for kind, pattern in FRONTMATTER.items():
        for encoding, name in FORMS.items():
            yield f"fm_{kind}/{encoding}", pattern.format(n=name), True
    for kind, (raw, is_fm) in SPECIAL.items():
        yield kind, raw, is_fm


CASES = list(_cases())


def _decoded(text: str) -> str:
    """*text* decoded to a fixpoint the way a reader may decode it."""
    for _ in range(16):
        value = urllib.parse.unquote(html.unescape(_COMMENT.sub("", text)))
        value = _YAML_HEX.sub(
            lambda m: chr(int(m.group(1) or m.group(2), 16)), value
        ).replace(BS, "")
        value = unicodedata.normalize("NFKC", value)
        if value == text:
            break
        text = value
    return text


@pytest.fixture(scope="module")
def projector(tmp_path_factory: pytest.TempPathFactory) -> MetaProjector:
    fx = build_cidx_meta_fixture(tmp_path_factory.mktemp("differential"))
    return MetaProjector(
        MetaVisibility.restricted([ALPHA]),
        dep_map_dir=fx.dep_map_dir,
        universe=process_alias_universe(fx.registry),
    )


def _document(raw: str, is_fm: bool) -> List[str]:
    if is_fm:
        head = "---\ndomain: sample\nparticipating_repos: [alpha]\n"
        return f"{head}{raw}\n---\n\n# sample\n\nalpha visible".splitlines()
    return f"# alpha\n\n{raw}\n\nalpha visible".splitlines()


def _tokens(lines: List[Optional[str]]) -> set:
    return {f for line in lines if line for f, _, _ in engine_tokens(line) if f}


@pytest.mark.parametrize("label,raw,is_fm", CASES, ids=[c[0] for c in CASES])
def test_no_form_of_a_hidden_name_survives(
    projector: MetaProjector, label: str, raw: str, is_fm: bool
) -> None:
    lines = _document(raw, is_fm)
    projected = projector.project(lines, "alpha.md")
    served = render_line_aligned(projected)
    rendered = MarkdownIt().render(served)
    parsed = None
    if is_fm and served.startswith("---"):
        parsed = yaml.safe_load(served.split("---", 2)[1])
    for text in (served, _decoded(served), rendered, _decoded(rendered), str(parsed)):
        assert CANARY not in text, (label, served)
    extra = _tokens(list(projected)) - _tokens(list(lines)) - MARKERS
    assert all(token.isdigit() for token in extra), (label, sorted(extra))


@pytest.mark.parametrize("label", sorted(VISIBLE))
def test_visible_lines_are_kept(projector: MetaProjector, label: str) -> None:
    lines = _document(VISIBLE[label], False)
    assert VISIBLE[label] in projector.project(lines, "alpha.md")
