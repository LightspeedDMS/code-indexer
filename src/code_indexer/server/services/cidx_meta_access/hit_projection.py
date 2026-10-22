"""Catalog search hits projected to the caller's view.

A restricted caller's catalog hit is kept only when the path policy allows
its file. DESCRIPTION and TRANSFORM hits get their text rebuilt from the
projected lines of the hit's own range (``MetaProjector.project_chunk``, so
a hit never shows more than its current document); a line the projection
removed becomes an empty line, so line numbers stay valid. A catalog hit
carries only ``CATALOG_HIT_FIELDS``: a field added to a hit shape later is
dropped, never passed through. A full-text hit must still match on what
the caller sees (``TextRematch``): a literal query is judged on the whole
shown document and nothing else (``CatalogScope.shown_match``, once per
path per request), and ``fulltext_document_rows`` builds one row per
document from its shown text, with a snippet of the request's size; a regex
match (full-text or ripgrep) keeps the engine's own match only when the
lines it covers are shown verbatim. The caller's pattern is never
evaluated here. So the existence of a hit never proves a hidden term is
in the document. A hit with no text left is dropped.

Projection is idempotent: a line that already equals its projected form is
recognised as such. One ``CatalogScope`` per request (``open_catalog_scope``):
one visibility, one projector, one read per document. Synchronous file
I/O: async doors call it through ``anyio.to_thread.run_sync``.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path, PurePosixPath
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

from ....utils.path_confinement import resolve_confined_path
from .depmap_projection import MetaProjector, render_line_aligned
from .fulltext_terms import (
    FulltextOptions,
    FullTextTerms,
    VisibleMatch,
    first_visible_match,
    parse_fulltext_terms,
)
from .membership import request_participants_reader
from .path_policy import (
    DEP_MAP_DIR,
    MEMORIES_DIR,
    XRAY_PATTERNS_DIR,
    Decision,
    DecisionKind,
    classify,
)
from .universe_provider import process_alias_universe
from .visibility import MetaVisibility, memory_visibility_fn, resolve_meta_visibility

logger = logging.getLogger(__name__)

CATALOG_HIT_FIELDS: FrozenSet[str] = frozenset(
    {
        "file_path",
        "path",
        "line",
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
    regex: bool = False
    edit_distance: int = 0
    options: FulltextOptions = field(default_factory=FulltextOptions, compare=False)

    @cached_property
    def terms(self) -> Optional[FullTextTerms]:
        """The query's required words; None for a refused literal form."""
        return parse_fulltext_terms(
            self.query, self.edit_distance, self.options.case_sensitive
        )

    def match(
        self, raw: str, view: LineView, match_text: Optional[str]
    ) -> Optional[str]:
        """The new ``match_text`` for a regex hit whose raw text *raw* is
        shown as *view*, or None when it no longer matches: the engine's own
        *match_text* only on lines shown verbatim; the pattern is never
        evaluated here. A literal query is judged per document
        (``CatalogScope.shown_match``), never here."""
        return engine_match_on_unchanged_lines(raw, view.unchanged, match_text)


@dataclass
class CatalogScope:
    """One restricted caller's view of one catalog copy, for one request."""

    vis: MetaVisibility
    root: Path
    projector: MetaProjector
    decide: Callable[[str], Decision]
    _documents: Dict[str, Optional[List[str]]] = field(default_factory=dict)
    _rendered: Dict[str, List[str]] = field(default_factory=dict)
    _matches: Dict[Tuple[str, FullTextTerms], Optional[VisibleMatch]] = field(
        default_factory=dict
    )
    _allowed: Optional[FrozenSet[str]] = None

    def _listed(self, relative_dir: str, *, files: bool) -> List[str]:
        """The non-dot files (or directories) of one catalog directory."""
        try:
            with os.scandir(self.root / relative_dir) as entries:
                return [
                    (Path(relative_dir) / e.name).as_posix()
                    for e in entries
                    if not e.name.startswith(".")
                    and (e.is_file(follow_symlinks=False) if files else e.is_dir())
                ]
        except FileNotFoundError:
            return []

    def allowed_paths(self) -> FrozenSet[str]:
        """Every catalog file the caller may see (not denied), relative to
        the copy, listed once per request without walking hidden subtrees:
        the flat directories, then only the pattern scopes the caller may
        see. Every file is still judged by ``decide``."""
        if self._allowed is None:
            listed: List[str] = []
            for flat in (".", MEMORIES_DIR, DEP_MAP_DIR):
                listed += self._listed(flat, files=True)
            for scope_dir in self._listed(XRAY_PATTERNS_DIR, files=False):
                if self.decide(f"{scope_dir}/_").kind is DecisionKind.DENY:
                    continue
                for directory, subdirs, names in os.walk(self.root / scope_dir):
                    subdirs[:] = [d for d in subdirs if not d.startswith(".")]
                    relative = Path(directory).relative_to(self.root)
                    listed += [(relative / name).as_posix() for name in names]
            self._allowed = frozenset(
                path
                for path in listed
                if self.decide(path).kind is not DecisionKind.DENY
            )
        return self._allowed

    def shows_projection(self, relative_path: str) -> bool:
        """True when the caller is shown a projection of the document (its
        text may differ from what the engine indexed)."""
        return self.decide(relative_path).kind in _PROJECTED_KINDS

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

    def shown_lines(self, relative_path: str) -> Optional[Tuple[List[str], List[str]]]:
        """The document's (raw, shown) lines: a projected kind shows its
        projection, any other allowed kind the document itself; None when
        the path is denied or unreadable."""
        kind = self.decide(relative_path).kind
        doc = None if kind is DecisionKind.DENY else self.document(relative_path)
        if doc is None:
            return None
        if kind in _PROJECTED_KINDS:
            return doc, self.rendered(relative_path, doc)
        return doc, doc

    def shown_match(
        self, relative_path: str, terms: FullTextTerms
    ) -> Optional[VisibleMatch]:
        """Where the shown document first holds the query when it holds
        every required word, else None; judged once per path per request."""
        key = (relative_path, terms)
        if key not in self._matches:
            lines = self.shown_lines(relative_path)
            found = None
            if lines is not None:
                _raw, shown = lines
                found = first_visible_match(shown, terms)
            self._matches[key] = found
        return self._matches[key]


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


def _text_lines(text: str) -> List[str]:
    """*text* split into lines, a CRLF ending read like LF (as
    ``CatalogScope.document`` reads the file)."""
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


def _chunk_candidates(lines: List[str]) -> List[List[str]]:
    """*lines*, then, when the text ended in a line break, *lines* without
    the empty element that break left (it may be a real empty line)."""
    if len(lines) > 1 and lines[-1] == "":
        return [lines, lines[:-1]]
    return [lines]


def project_chunk_view(
    scope: CatalogScope, relative_path: str, text: str, hint: Optional[int]
) -> Optional[LineView]:
    """*text* (lines of the document starting near *hint*) as the caller
    sees it; None when it is not in the current document or no line of it
    has anything left (``LineView.text`` is None)."""
    doc = scope.document(relative_path)
    if doc is None:
        return None
    shown = scope.rendered(relative_path, doc)
    chunk, first = [""], None
    for chunk in _chunk_candidates(_text_lines(text)):
        first = _locate(chunk, doc, shown, hint)
        if first is not None:
            break
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


def shown_snippet(shown: List[str], line: int, snippet_lines: int) -> str:
    """The shown lines within *snippet_lines* of 1-based *line*, clipped to
    the document; empty when no context is requested."""
    if snippet_lines <= 0:
        return ""
    first = max(0, line - 1 - snippet_lines)
    return "\n".join(shown[first : line + snippet_lines])


def _project_fulltext(
    hit: Any,
    scope: CatalogScope,
    path: str,
    terms: Optional[FullTextTerms],
    snippet_lines: int,
) -> Optional[Any]:
    """A literal full-text hit, kept only when the shown document holds
    every required word, anchored on its first shown occurrence with a
    snippet of the request's size; dropped for a form that cannot be judged
    on shown text."""
    found = None if terms is None else scope.shown_match(path, terms)
    lines = scope.shown_lines(path)
    if found is None or lines is None:
        return None
    _raw, shown = lines
    updates: Dict[str, Optional[str]] = {}
    for key in _TEXT_KEYS:
        value = _get(hit, (key,))
        if isinstance(value, str) and value:
            updates[key] = shown_snippet(shown, found.line, snippet_lines)
    if _first_key(hit, ("match_text",)) is not None:
        updates["match_text"] = found.text
    rebuilt = _rebuild(hit, updates)
    for key in ("line", "line_number", "line_start", "line_end"):
        value = _get(hit, (key,))
        if isinstance(value, int) and value > 0:
            rebuilt = _with_line(rebuilt, key, found.line)
    return rebuilt


def _with_line(hit: Any, key: str, line: int) -> Any:
    if isinstance(hit, dict):
        return dict(hit, **{key: line})
    return dataclasses.replace(hit, **{key: line})


def _project_one(
    hit: Any, scope: CatalogScope, rematch: Optional[TextRematch]
) -> Optional[Any]:
    path = _get(hit, _PATH_KEYS)
    if not isinstance(path, str):
        return None
    decision = scope.decide(path)
    if decision.kind is DecisionKind.DENY:
        return None
    if rematch is not None and not rematch.regex:
        return _project_fulltext(
            hit, scope, path, rematch.terms, rematch.options.snippet_lines
        )
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


def fulltext_document_rows(
    paths: Iterable[str], scope: CatalogScope, terms: FullTextTerms, snippet_lines: int
) -> List[Dict[str, Any]]:
    """One engine-shaped row per candidate path whose shown text holds
    every required word, built from the shown document alone (anchor at the
    first shown occurrence, a snippet of the request's size, the language
    as the indexer records it). Rows are ordered by how often the required
    words occur in the shown text, then by path."""
    ranked: List[Tuple[int, str, Dict[str, Any]]] = []
    for path in sorted(set(paths)):
        found = scope.shown_match(path, terms)
        lines = scope.shown_lines(path)
        if found is None or lines is None:
            continue
        _raw, shown = lines
        row = {
            "path": path,
            "line": found.line,
            "match_text": found.text,
            "snippet": shown_snippet(shown, found.line, snippet_lines),
            "language": PurePosixPath(path).suffix.lstrip(".") or "txt",
        }
        ranked.append((-found.count, path, row))
    ranked.sort(key=lambda entry: (entry[0], entry[1]))
    return [row for _, _, row in ranked]


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
