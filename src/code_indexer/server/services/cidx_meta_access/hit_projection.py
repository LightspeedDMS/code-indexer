"""Catalog search hits projected to the caller's view.

A restricted caller's catalog hit is kept only when the path policy allows
its file. DESCRIPTION and TRANSFORM hits get their text rebuilt from the
projected lines of the hit's own range (``MetaProjector.project_chunk``, so
a hit never shows more than its current document); a line the projection
removed becomes an empty line, so line numbers stay valid. A catalog hit
carries only ``CATALOG_HIT_FIELDS``: a field added to a hit shape later is
dropped, never passed through. A full-text hit is re-matched on the
projected text (``TextRematch``), so the existence of a hit never proves a
hidden term is in the document. A hit with no text left is dropped.

Projection is idempotent: a line that already equals its projected form is
recognised as such. One ``CatalogScope`` per request (``open_catalog_scope``):
one visibility, one projector, one read per document. Synchronous file
I/O: async doors call it through ``anyio.to_thread.run_sync``.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence

from ....utils.path_confinement import resolve_confined_path
from .depmap_projection import MetaProjector, render_line_aligned
from .membership import participants_reader
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
class TextRematch:
    """The full-text query a hit must still match on its projected text."""

    query: str
    case_sensitive: bool = False
    regex: bool = False

    def _flags(self) -> int:
        return 0 if self.case_sensitive else re.IGNORECASE

    def match(self, text: str, match_text: Optional[str]) -> Optional[str]:
        """The match in *text* (the new ``match_text``), or None when the
        projected text no longer matches. A pattern Python cannot compile
        never matches (deny by default)."""
        if self.regex:
            try:
                found = re.search(self.query, text, self._flags() | re.MULTILINE)
            except re.error:
                return None
            return found.group(0) if found and found.group(0) else None
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
    read_participants = participants_reader(root / DEP_MAP_DIR)
    memo: Dict[str, Optional[FrozenSet[str]]] = {}

    def participants(domain: str) -> Optional[FrozenSet[str]]:
        if domain not in memo:
            memo[domain] = read_participants(domain)
        return memo[domain]

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
) -> Optional[List[str]]:
    """*lines* taken to sit at 1-based *first* of the current document, as
    the caller sees them (aligned; removed lines empty). A whole line takes
    its projected form, a partial line is kept only inside its projected
    line, and a line that is not in the document there is emptied. None
    when the document cannot be read or *first* is not a line."""
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
    for line, raw, i, ok, verdict in zip(lines, raw_lines, indexes, current, verdicts):
        if not ok or verdict is None:
            out.append("")
        elif line == raw or line == shown[i]:
            out.append(verdict)
        else:
            out.append(line if line in verdict else "")
    return out


def project_text(
    scope: CatalogScope, relative_path: str, text: str, hint: Optional[int]
) -> Optional[str]:
    """*text* (lines of the document starting near *hint*) as the caller
    sees it, line-aligned; None when nothing of it remains."""
    doc = scope.document(relative_path)
    if doc is None:
        return None
    chunk = text.split("\n")
    first = _locate(chunk, doc, scope.rendered(relative_path, doc), hint)
    out = (
        None if first is None else _project_lines_at(scope, relative_path, chunk, first)
    )
    if out is None or not any(line.strip() for line in out):
        return None
    return "\n".join(out)


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
    if projected_kind:
        hint = _get(hit, _LINE_KEYS)
        for key, value in texts.items():
            updates[key] = (
                project_text(scope, path, value, hint)
                if isinstance(value, str)
                else None
            )
        if texts and not any(updates.values()):
            return None
    shown = [
        text
        for text in (updates.get(k, v) for k, v in texts.items())
        if isinstance(text, str) and text
    ]
    match_text = _get(hit, ("match_text",))
    if rematch is not None:
        prior = match_text if isinstance(match_text, str) else None
        found = next((m for m in (rematch.match(t, prior) for t in shown) if m), None)
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


def _compile(pattern: str, case_sensitive: bool, multiline: bool) -> Optional[Any]:
    flags = 0 if case_sensitive else re.IGNORECASE
    if multiline:
        flags |= re.MULTILINE | re.DOTALL
    try:
        return re.compile(pattern, flags)
    except re.error:
        return None


def _project_match(
    match: Dict[str, Any], scope: CatalogScope, compiled: Optional[Any]
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
    if compiled is None or not isinstance(line_number, int):
        return None
    lines = _project_lines_at(
        scope, path, before + content + after, line_number - len(before)
    )
    if lines is None:
        return None
    projected_content = "\n".join(lines[len(before) : len(before) + len(content)])
    found = compiled.search(projected_content)
    if found is None:
        return None
    line_start = projected_content.rfind("\n", 0, found.start()) + 1
    kept.update(
        context_before=lines[: len(before)],
        line_content=projected_content,
        context_after=lines[len(before) + len(content) :],
        column=found.start() - line_start + 1,
    )
    return kept


def project_regex_matches(
    matches: Sequence[Dict[str, Any]],
    scope: CatalogScope,
    *,
    pattern: str,
    case_sensitive: bool,
    multiline: bool,
) -> List[Dict[str, Any]]:
    """The catalog regex *matches* the caller may see, projected and
    re-matched on the projected text, in input order."""
    compiled = _compile(pattern, case_sensitive, multiline)
    out: List[Dict[str, Any]] = []
    for match in matches:
        projected = _project_match(match, scope, compiled)
        if projected is not None:
            out.append(projected)
    return out
