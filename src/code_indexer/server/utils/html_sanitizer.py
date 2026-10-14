"""Allowlist sanitisation for HTML the server emits unescaped (``| safe``).

Every element, attribute and URL scheme not named by an ``HtmlAllowlist`` is
removed or escaped, so script elements, event-handler attributes, ``style``
attributes and ``javascript:``/``vbscript:``/``data:`` URLs never reach a page.
"""

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterator, Mapping, Tuple

import bleach  # type: ignore[import-untyped]
from bleach.html5lib_shim import Filter  # type: ignore[import-untyped,attr-defined]

NOOPENER_REL = "noopener noreferrer"


@dataclass(frozen=True)
class HtmlAllowlist:
    """Elements, attributes and URL schemes kept by ``sanitize_html``.

    strip: True removes a disallowed element and keeps its text; False escapes
    the disallowed markup so it shows as text.
    isolate_target_links: every kept ``<a>`` with a ``target`` gets
    ``rel="noopener noreferrer"``, replacing any ``rel`` it carried.
    """

    tags: FrozenSet[str]
    attributes: Mapping[str, Tuple[str, ...]]
    protocols: FrozenSet[str]
    strip: bool
    isolate_target_links: bool = False


class _TargetLinksNoOpener(Filter):
    """Runs after sanitising: a link that opens a new browsing context never
    keeps a reference to the opening page."""

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for token in super().__iter__():
            if (
                token["type"] in ("StartTag", "EmptyTag")
                and token["name"] == "a"
                and (None, "target") in token["data"]
            ):
                token["data"][(None, "rel")] = NOOPENER_REL
            yield token


def sanitize_html(html: str, allowlist: HtmlAllowlist) -> str:
    """Return ``html`` reduced to what ``allowlist`` permits (comments dropped)."""
    cleaner = bleach.sanitizer.Cleaner(
        tags=set(allowlist.tags),
        attributes={tag: list(attrs) for tag, attrs in allowlist.attributes.items()},
        protocols=set(allowlist.protocols),
        strip=allowlist.strip,
        strip_comments=True,
        # Cleaner instantiates each filter class itself, after sanitising.
        filters=[_TargetLinksNoOpener] if allowlist.isolate_target_links else None,
    )
    return str(cleaner.clean(html))


_WEB_URL_SCHEMES = frozenset({"http", "https", "mailto"})
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")

# Markdown output of wiki articles, plus the attributes the wiki adds after
# rendering: heading ids (anchors) and target/rel on external links.
WIKI_ARTICLE_ALLOWLIST = HtmlAllowlist(
    tags=frozenset(
        _HEADINGS
        + ("p", "br", "hr", "em", "strong", "b", "i", "del", "s")
        + ("code", "pre", "blockquote", "ul", "ol", "li", "a", "img")
        + ("table", "thead", "tbody", "tr", "th", "td")
        + ("details", "summary", "sup", "sub", "kbd", "div", "span")
    ),
    attributes={
        **{heading: ("id",) for heading in _HEADINGS},
        "a": ("href", "title", "target", "rel"),
        "img": ("src", "alt", "title"),
        "code": ("class",),
        "pre": ("class",),
        "div": ("class",),
        "span": ("class",),
        "ol": ("start",),
        "details": ("open",),
    },
    protocols=_WEB_URL_SCHEMES,
    strip=True,
    isolate_target_links=True,
)

RESEARCH_ASSISTANT_ALLOWLIST = HtmlAllowlist(
    tags=frozenset(
        _HEADINGS
        + ("p", "br", "strong", "em", "code", "pre", "blockquote")
        + ("ul", "ol", "li", "a", "table", "thead", "tbody", "tr", "th", "td")
        + ("div", "span", "hr")
    ),
    attributes={
        "a": ("href", "title"),
        "code": ("class",),
        "div": ("class",),
        "span": ("class",),
        "pre": ("class",),
    },
    protocols=_WEB_URL_SCHEMES,
    strip=False,
)
