"""Catalog search hits projected to the caller's view.

A restricted caller's catalog hit is kept only when the path policy allows
its file. DESCRIPTION and TRANSFORM hits get their text rebuilt from the
projected lines of the hit's own range (``MetaProjector.project_chunk``, so
a hit never shows more than its current document); a line the projection
removed becomes an empty line, so line numbers stay valid. A catalog hit
carries only ``CATALOG_HIT_FIELDS``: a field added to a hit shape later is
dropped, never passed through. A full-text hit must still match on what
the caller sees (``TextRematch``): literal terms by substring on the
projected text; a regex match (full-text or ripgrep) keeps the engine's own
match only when the lines it covers are shown verbatim. The caller's
pattern is never evaluated here. So the existence of a hit never proves a
hidden term is in the document. A hit with no text left is dropped.

Projection is idempotent: a line that already equals its projected form is
recognised as such. One ``CatalogScope`` per request (``open_catalog_scope``):
one visibility, one projector, one read per document. Synchronous file
I/O: async doors call it through ``anyio.to_thread.run_sync``.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence

from ....utils.path_confinement import resolve_confined_path
from .depmap_projection import MetaProjector, render_line_aligned
from .membership import request_participants_reader
from .path_policy import DEP_MAP_DIR, Decision, DecisionKind, classify
from .universe_provider import process_alias_universe
from .visibility import MetaVisibility, memory_visibility_fn, resolve_meta_visibility

logger = logging.getLogger(__name__)

CATALOG_HIT_FIELDS: FrozenSet[str] = frozenset(
    {
        "file_path",
        "path",
        "line_number",
        "line_start",
        "line_end",
        "code_snippet",
        "snippet",
        "content",
        "similarity_score",
        "score",
        "repository_alias",
        "source_repo",
        "source_provider",
        "match_text",
        "fusion_score",
        "contributing_providers",
        "content_unavailable",
        "activation_source_repos",
        "language",
        "repo_category",
        "wiki_url",
    }
)
_PATH_KEYS = ("file_path", "path")
_LINE_KEYS = ("line_number", "line_start")
_TEXT_KEYS = ("code_snippet", "snippet", "content")
_PROJECTED_KINDS = frozenset({DecisionKind.DESCRIPTION, DecisionKind.TRANSFORM})
_NEAR_WINDOW = 64


@dataclass(frozen=True)
class LineView:
    """Text as the caller sees it, line-aligned with the raw text, and for
    each line whether it is in the current document and shown verbatim."""

    lines: List[str]
    unchanged: List[bool]

    @classmethod
    def verbatim(cls, text: str) -> "LineView":
        lines = text.split("\n")
        return cls(lines=lines, unchanged=[True] * len(lines))

    @property
    def text(self) -> Optional[str]:
        """The shown text, or None when no line has anything left."""
        if not any(line.strip() for line in self.lines):
            return None
        return "\n".join(self.lines)


def engine_match_on_unchanged_lines(
    raw: str, unchanged: Sequence[bool], needle: Optional[str]
) -> Optional[str]:
    """*needle* (the text the search engine matched) when one of its
    occurrences in *raw* lies only on lines shown verbatim, else None."""
    if not needle:
        return None
    start = raw.find(needle)
    while start >= 0:
        first = raw.count("\n", 0, start)
        last = first + needle.count("\n")
        if last < len(unchanged) and all(unchanged[first : last + 1]):
            return needle
        start = raw.find(needle, start + 1)
    return None


@dataclass(frozen=True)
class TextRematch:
    """The full-text query a hit must still match on its projected text."""

    query: str
    case_sensitive: bool = False
    regex: bool = False

    def match(
        self, raw: str, view: LineView, match_text: Optional[str]
    ) -> Optional[str]:
        """The new ``match_text`` for a hit whose raw text *raw* is shown as
        *view*, or None when the hit no longer matches. A regex query keeps
        the engine's own *match_text* only on lines shown verbatim; it is
        never evaluated here."""
        if self.regex:
            return engine_match_on_unchanged_lines(raw, view.unchanged, match_text)
        text = "\n".join(view.lines)
        needles = [match_text] if match_text else self.query.split()
        haystack = text if self.case_sensitive else text.lower()
        for needle in needles:
            probe = needle if self.case_sensitive else needle.lower()
            position = haystack.find(probe) if probe else -1
            if position >= 0:
                return text[position : position + len(needle)]
        return None


@dataclass
class CatalogScope:
    """One restricted caller's view of one catalog copy, for one request."""

    vis: MetaVisibility
    root: Path
    projector: MetaProjector
    decide: Callable[[str], Decision]
    _documents: Dict[str, Optional[List[str]]] = field(default_factory=dict)
    _rendered: Dict[str, List[str]] = field(default_factory=dict)

    def document(self, relative_path: str) -> Optional[List[str]]:
        """The current lines of a catalog file, read once per request;
        None when it cannot be read inside the catalog."""
        if relative_path not in self._documents:
            try:
                path = resolve_confined_path(self.root, relative_path)
                lines: Optional[List[str]] = path.read_text(
                    encoding="utf-8"
                ).splitlines()
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                logger.info(
                    "Catalog hit document unreadable (%s); hit dropped",
                    type(exc).__name__,
                )
                lines = None
            self._documents[relative_path] = lines
        return self._documents[relative_path]

    def rendered(self, relative_path: str, document: List[str]) -> List[str]:
        """The document's projection with removed lines as empty lines."""
        if relative_path not in self._rendered:
            projected = self.projector.project(document, relative_path)
            self._rendered[relative_path] = render_line_aligned(projected).split("\n")
        return self._rendered[relative_path]


def open_catalog_scope(
    username: str, access_service: Optional[Any], catalog_root: Path, registry: Any
) -> Optional[CatalogScope]:
    """The caller's scope over the catalog at *catalog_root*, or None for an
    unrestricted caller (no projection at all). Raises without a registry:
    a restricted projection never runs with an unknown alias universe."""
    vis = resolve_meta_visibility(username, access_service)
    if vis.unrestricted:
        return None
    root = Path(catalog_root)
    projector = MetaProjector(
        vis, dep_map_dir=root / DEP_MAP_DIR, universe=process_alias_universe(registry)
    )
    participants = request_participants_reader(root / DEP_MAP_DIR, projector.model)
    memory_fn = memory_visibility_fn(access_service, vis)
    return CatalogScope(
        vis=vis,
        root=root,
        projector=projector,
        decide=lambda path: classify(path, vis, participants, memory_fn),
    )


def _get(hit: Any, keys: Sequence[str]) -> Any:
    for key in keys:
        value = hit.get(key) if isinstance(hit, dict) else getattr(hit, key, None)
        if value is not None:
            return value
    return None


def _first_key(hit: Any, keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        present = key in hit if isinstance(hit, dict) else hasattr(hit, key)
        if present:
            return key
    return None


def _line_fits(chunk_line: str, raw: str, shown: str) -> bool:
    return chunk_line in (raw, shown) or chunk_line in raw


def _aligned(chunk: List[str], doc: List[str], shown: List[str], first: int) -> bool:
    start = first - 1
    if start < 0 or start + len(chunk) > len(doc):
        return False
    return all(
        _line_fits(line, doc[start + i], shown[start + i])
        for i, line in enumerate(chunk)
    )


def _locate(
    chunk: List[str], doc: List[str], shown: List[str], hint: Optional[int]
) -> Optional[int]:
    """1-based line where *chunk* sits in the document: the hint, then the
    lines just above it (a snippet starts before its match line), then
    anywhere; None when the chunk is not in the current document."""
    candidates: List[int] = []
    if isinstance(hint, int) and hint >= 1:
        candidates = [hint] + [hint - d for d in range(1, _NEAR_WINDOW) if hint > d]
    candidates += range(1, len(doc) - len(chunk) + 2)
    for first in candidates:
        if _aligned(chunk, doc, shown, first):
            return first
    return None


def _project_lines_at(
    scope: CatalogScope, relative_path: str, lines: List[str], first: int
) -> Optional[LineView]:
    """*lines* taken to sit at 1-based *first* of the current document, as
    the caller sees them (aligned; removed lines empty). A whole line takes
    its projected form, a partial line is kept only inside its projected
    line, and a line that is not in the document there is emptied. A line
    is unchanged when its document line projects to itself. None when the
    document cannot be read or *first* is not a line."""
    doc = scope.document(relative_path)
    if doc is None or first < 1:
        return None
    shown = scope.rendered(relative_path, doc)
    indexes = [first - 1 + i for i in range(len(lines))]
    current = [
        i < len(doc) and _line_fits(line, doc[i], shown[i])
        for line, i in zip(lines, indexes)
    ]
    raw_lines = [doc[i] if ok else line for line, i, ok in zip(lines, indexes, current)]
    verdicts = scope.projector.project_chunk(raw_lines, first, doc, relative_path)
    out: List[str] = []
    unchanged: List[bool] = []
    for line, raw, i, ok, verdict in zip(lines, raw_lines, indexes, current, verdicts):
        unchanged.append(ok and verdict == raw)
        if not ok or verdict is None:
            out.append("")
        elif line == raw or line == shown[i]:
            out.append(verdict)
        else:
            out.append(line if line in verdict else "")
    return LineView(lines=out, unchanged=unchanged)


def project_chunk_view(
    scope: CatalogScope, relative_path: str, text: str, hint: Optional[int]
) -> Optional[LineView]:
    """*text* (lines of the document starting near *hint*) as the caller
    sees it; None when it is not in the current document or no line of it
    has anything left (``LineView.text`` is None)."""
    doc = scope.document(relative_path)
    if doc is None:
        return None
    chunk = text.split("\n")
    first = _locate(chunk, doc, scope.rendered(relative_path, doc), hint)
    view = (
        None if first is None else _project_lines_at(scope, relative_path, chunk, first)
    )
    if view is None or view.text is None:
        return None
    return view


def project_text(
    scope: CatalogScope, relative_path: str, text: str, hint: Optional[int]
) -> Optional[str]:
    """*text* (lines of the document starting near *hint*) as the caller
    sees it, line-aligned; None when nothing of it remains."""
    view = project_chunk_view(scope, relative_path, text, hint)
    return None if view is None else view.text


def _rebuild(hit: Any, updates: Dict[str, Optional[str]]) -> Any:
    """A copy of *hit* with only allow-listed fields, then *updates*. An
    update of None removes a dict field; on a dataclass it empties the
    field (``""``, or None for ``match_text``). Updates only touch fields
    the hit carries: a dataclass never gains a field it does not declare."""
    if isinstance(hit, dict):
        kept = {k: v for k, v in hit.items() if k in CATALOG_HIT_FIELDS}
        for key, value in updates.items():
            if value is None:
                kept.pop(key, None)
            else:
                kept[key] = value
        return kept
    if dataclasses.is_dataclass(hit) and not isinstance(hit, type):
        values = {
            f.name: getattr(hit, f.name)
            for f in dataclasses.fields(hit)
            if f.name in CATALOG_HIT_FIELDS
        }
        for key, value in updates.items():
            if key not in values:
                continue
            empty = None if key == "match_text" else ""
            values[key] = empty if value is None else value
        return type(hit)(**values)
    raise TypeError(f"unsupported catalog hit type: {type(hit).__name__}")


def _project_one(
    hit: Any, scope: CatalogScope, rematch: Optional[TextRematch]
) -> Optional[Any]:
    path = _get(hit, _PATH_KEYS)
    if not isinstance(path, str):
        return None
    decision = scope.decide(path)
    if decision.kind is DecisionKind.DENY:
        return None
    texts = {key: value for key in _TEXT_KEYS if (value := _get(hit, (key,)))}
    projected_kind = decision.kind in _PROJECTED_KINDS
    updates: Dict[str, Optional[str]] = {}
    views: Dict[str, LineView] = {}
    hint = _get(hit, _LINE_KEYS)
    for key, value in texts.items():
        view: Optional[LineView] = None
        if isinstance(value, str):
            view = (
                project_chunk_view(scope, path, value, hint)
                if projected_kind
                else LineView.verbatim(value)
            )
        if view is not None:
            views[key] = view
        if projected_kind:
            updates[key] = None if view is None else view.text
    if projected_kind and texts and not views:
        return None
    shown = [text for view in views.values() if (text := view.text)]
    match_text = _get(hit, ("match_text",))
    if rematch is not None:
        prior = match_text if isinstance(match_text, str) else None
        found = next(
            (m for k, v in views.items() if (m := rematch.match(texts[k], v, prior))),
            None,
        )
        if found is None:
            return None
        if _first_key(hit, ("match_text",)) is not None:
            updates["match_text"] = found
    elif projected_kind and match_text is not None:
        if not (isinstance(match_text, str) and any(match_text in t for t in shown)):
            updates["match_text"] = None
    return _rebuild(hit, updates)


def project_meta_hits(
    hits: Sequence[Any], scope: CatalogScope, rematch: Optional[TextRematch] = None
) -> List[Any]:
    """The catalog *hits* the caller may see, projected, in input order."""
    out: List[Any] = []
    for hit in hits:
        projected = _project_one(hit, scope, rematch)
        if projected is not None:
            out.append(projected)
    return out


REGEX_MATCH_FIELDS: FrozenSet[str] = frozenset(
    {
        "file_path",
        "line_number",
        "column",
        "line_content",
        "context_before",
        "context_after",
        "source_repo",
        "wiki_url",
    }
)


def _project_match(
    match: Dict[str, Any], scope: CatalogScope
) -> Optional[Dict[str, Any]]:
    path = match.get("file_path")
    if not isinstance(path, str):
        return None
    decision = scope.decide(path)
    if decision.kind is DecisionKind.DENY:
        return None
    kept = {k: v for k, v in match.items() if k in REGEX_MATCH_FIELDS}
    if decision.kind not in _PROJECTED_KINDS:
        return kept
    before = [str(x) for x in match.get("context_before") or []]
    content = str(match.get("line_content") or "").split("\n")
    after = [str(x) for x in match.get("context_after") or []]
    line_number = match.get("line_number")
    if not isinstance(line_number, int):
        return None
    view = _project_lines_at(
        scope, path, before + content + after, line_number - len(before)
    )
    match_lines = slice(len(before), len(before) + len(content))
    if view is None or not all(view.unchanged[match_lines]):
        return None
    kept.update(
        context_before=view.lines[: len(before)],
        line_content="\n".join(view.lines[match_lines]),
        context_after=view.lines[match_lines.stop :],
    )
    return kept


def project_regex_matches(
    matches: Sequence[Dict[str, Any]], scope: CatalogScope
) -> List[Dict[str, Any]]:
    """The catalog regex *matches* the caller may see, in input order: a
    match is kept (with the engine's line and column) only when every line
    it covers is shown verbatim; its context lines are projected."""
    out: List[Dict[str, Any]] = []
    for match in matches:
        projected = _project_match(match, scope)
        if projected is not None:
            out.append(projected)
    return out
