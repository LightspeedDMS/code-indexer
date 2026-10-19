"""Line projection of a catalog document for one caller.

The result is aligned 1:1 with the input lines; ``None`` marks a removed
line. ``project_lines`` takes a whole document and fails closed: only
recognised dependency-map tables (header plus separator, matching cell
count) and parseable, closed frontmatter reach a restricted caller; any
other table, header-less row or malformed block is removed. A search or
regex chunk goes through ``project_chunk``, which takes each line's verdict
from the projection of the current whole document (so a chunk never shows
more than its document) and removes lines that no longer match it.

A table is recognised by its header row (the row right before a separator,
compared case-insensitively; see ``depmap_model.table_kind``). In a
recognised table only the repo-bearing cells, the domain cells and the Via
Repos list decide whether a row is dropped, by whole-value or name-shaped
matching; every free-text cell only goes through prose redaction. YAML list
items and counts are filtered and recomputed from the caller's view, so
counts equal the rows kept. Exact hidden names in prose become
``[restricted repository]``, or ``[restricted domain]`` for a hidden domain
that is not also a repository. Unrestricted callers get the input unchanged.

Callers are synchronous; async doors run them through
``anyio.to_thread.run_sync``. A door projecting several hits opens one
``MetaProjector`` per request (one model and universe load per request).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Pattern,
    Sequence,
    Set,
    Tuple,
)

import yaml

from ..constants import is_internal_meta_repo
from ..dep_map_parser_tables import parse_frontmatter_strict
from ..repo_access_guard import normalize_repo_alias
from .alias_universe import AliasUniverse, UniverseSnapshot
from .depmap_model import (
    CATALOG,
    GRAPH,
    DepMapModel,
    TableKind,
    cell_names,
    is_separator,
    load_depmap_model,
    normalize_name,
    table_cells,
    table_kind,
)
from .visibility import MetaVisibility

logger = logging.getLogger(__name__)

RESTRICTED_PLACEHOLDER = "[restricted repository]"
DOMAIN_PLACEHOLDER = "[restricted domain]"
MIN_PROSE_ALIAS_LENGTH = 4
VIEW_CACHE_MAX_ENTRIES = 64
PROJECTION_CACHE_MAX_ENTRIES = 256
_DIAGNOSTIC_LIMIT = 4096

_ALIAS_CHARS = "A-Za-z0-9_-"
_LIST_ITEM = re.compile(r"^\s*-\s+(?P<value>.+?)\s*$")
_YAML_SHAPED = re.compile(r"^\s*(?:[A-Za-z_][\w-]*\s*:.*|-\s+.*)?$")
_INLINE_LIST = re.compile(r"^(?P<prefix>\s*[A-Za-z_][\w-]*:\s*)\[(?P<body>.*)\]\s*$")
_COUNT_LINE = re.compile(
    r"^(?P<prefix>\s*(?P<key>repos_analyzed_count|domains_count):\s*)\d+\s*$"
)
_SUMMARY_LINE = re.compile(
    r"^\*\*Summary\*\*: \d+ cross-domain edges across \d+ domains\."
)


@dataclass(frozen=True)
class ProjectionCacheStats:
    view_builds: int
    projections_computed: int
    projection_hits: int


@dataclass(frozen=True)
class _View:
    """``blocked_repos`` decides repo cells and YAML items; ``blocked_domains``
    decides domain cells; catalog/graph rows need a visible domain."""

    blocked_repos: FrozenSet[str]
    blocked_domains: FrozenSet[str]
    visible: FrozenSet[str]
    visible_domains: FrozenSet[str]
    domain_only: FrozenSet[str]
    domain_counts: Mapping[str, int]
    counts: Mapping[str, int]
    summary: str
    prose: Optional[Pattern[str]]


_lock = threading.Lock()
_views: "OrderedDict[Tuple[str, str, str], _View]" = OrderedDict()
_projections: "OrderedDict[Tuple[str, ...], Tuple[Optional[str], ...]]" = OrderedDict()
_diagnosed: Set[Tuple[str, str]] = set()
_stats = {"view_builds": 0, "projections_computed": 0, "projection_hits": 0}


def projection_cache_stats() -> ProjectionCacheStats:
    """Process-wide counters of the projection caches (diagnostics)."""
    with _lock:
        return ProjectionCacheStats(**_stats)


def _trie_pattern(node: Dict[str, Any]) -> str:
    branches: List[str] = []
    singles: List[str] = []
    for char in sorted(key for key in node if key):
        child = node[char]
        if set(child) == {""}:
            singles.append(re.escape(char))
        else:
            branches.append(re.escape(char) + _trie_pattern(child))
    if singles:
        branches.append(singles[0] if len(singles) == 1 else f"[{''.join(singles)}]")
    body = branches[0] if len(branches) == 1 else f"(?:{'|'.join(branches)})"
    return f"(?:{body})?" if "" in node else body


def _redaction_regex(names: Sequence[str]) -> Optional[Pattern[str]]:
    """One alternation (a character trie, linear per position) over *names*."""
    if not names:
        return None
    trie: Dict[str, Any] = {}
    for name in names:
        node = trie
        for char in name:
            node = node.setdefault(char, {})
        node[""] = {}
    return re.compile(
        rf"(?<![{_ALIAS_CHARS}]){_trie_pattern(trie)}(?:-global)?(?![{_ALIAS_CHARS}])",
        re.IGNORECASE,
    )


def _build_view(
    model: DepMapModel, universe: UniverseSnapshot, vis: MetaVisibility
) -> _View:
    visible = frozenset(name.lower() for name in vis.visible)
    visible_domains = model.visible_domains(visible)
    universe_names = frozenset(
        normalize_repo_alias(name).lower() for name in universe.names
    )
    repo_names = frozenset(
        name
        for name in (universe_names | model.structured_names) - visible
        if not is_internal_meta_repo(name)
    )
    hidden_domains = model.domains - visible_domains - visible
    hidden = frozenset(repo_names | hidden_domains)
    edges = [
        (source, target)
        for source, target, via in model.edges
        if source in visible_domains and target in visible_domains and via & visible
    ]
    linked = {domain for edge in edges for domain in edge}
    standalone = sorted(
        model.domain_display.get(d, d) for d in visible_domains - linked
    )
    summary = (
        f"**Summary**: {len(edges)} cross-domain edges across "
        f"{len(visible_domains)} domains."
    )
    if standalone:
        summary += f" {len(standalone)} standalone domains: {', '.join(standalone)}."
    return _View(
        blocked_repos=hidden,
        blocked_domains=frozenset(hidden_domains | (repo_names - visible_domains)),
        visible=visible,
        visible_domains=visible_domains,
        domain_only=frozenset(hidden_domains - repo_names),
        domain_counts={
            d: len(model.participants[d] & visible) for d in visible_domains
        },
        counts={
            "repos_analyzed_count": len(model.analyzed_repos & visible),
            "domains_count": len(visible_domains),
        },
        summary=summary,
        prose=_redaction_regex(
            [
                name
                for name in hidden - visible_domains
                if len(name) >= MIN_PROSE_ALIAS_LENGTH
            ]
        ),
    )


def _view_for(
    model: DepMapModel, universe: UniverseSnapshot, vis: MetaVisibility
) -> _View:
    key = (model.fingerprint, universe.digest, vis.vis_hash)
    with _lock:
        view = _views.get(key)
        if view is not None:
            _views.move_to_end(key)
            return view
    view = _build_view(model, universe, vis)
    with _lock:
        _stats["view_builds"] += 1
        _views[key] = view
        while len(_views) > VIEW_CACHE_MAX_ENTRIES:
            _views.popitem(last=False)
    return view


def _redact(line: str, view: _View) -> str:
    if view.prose is None:
        return line

    def _placeholder(match: "re.Match[str]") -> str:
        name = normalize_repo_alias(match.group(0)).lower()
        return (
            DOMAIN_PLACEHOLDER if name in view.domain_only else RESTRICTED_PLACEHOLDER
        )

    return view.prose.sub(_placeholder, line)


def _blocked(cell: str, names: FrozenSet[str]) -> bool:
    return any(name in names for name in cell_names(cell))


def _row_kept(kind: TableKind, cells: List[str], view: _View) -> bool:
    """Only repo, domain and list columns decide. Catalog and graph rows are
    kept only for visible domains: the set the summary and the counts are
    computed from, so every count equals the rows kept."""
    if any(_blocked(cells[c], view.blocked_repos) for c in kind.repo_columns):
        return False
    domains = [normalize_name(cells[c]) for c in kind.domain_columns]
    if kind in (CATALOG, GRAPH):
        return all(domain in view.visible_domains for domain in domains)
    return not any(
        _blocked(cells[c], view.blocked_domains) for c in kind.domain_columns
    )


def _project_row(
    line: str, kind: TableKind, cells: List[str], view: _View
) -> Optional[str]:
    if not _row_kept(kind, cells, view):
        return None
    projected = list(cells)
    if kind.list_column is not None:
        entries = [e.strip() for e in cells[kind.list_column].split(",")]
        kept = [e for e in entries if normalize_name(e) in view.visible]
        if not kept:
            return None
        projected[kind.list_column] = ", ".join(kept)
    if kind.count_column is not None:
        domain = normalize_name(cells[kind.domain_columns[0]])
        projected[kind.count_column] = str(view.domain_counts[domain])
    if projected != cells:
        line = "| " + " | ".join(projected) + " |"
    return _redact(line, view)


def _project_text(line: str, view: _View) -> Optional[str]:
    count = _COUNT_LINE.match(line)
    if count:
        return f"{count.group('prefix')}{view.counts[count.group('key')]}"
    if _SUMMARY_LINE.match(line):
        return view.summary
    inline = _INLINE_LIST.match(line)
    if inline:
        entries = [e.strip() for e in inline.group("body").split(",") if e.strip()]
        kept = [e for e in entries if normalize_name(e) not in view.blocked_repos]
        if len(kept) != len(entries):
            line = f"{inline.group('prefix')}[{', '.join(kept)}]"
    else:
        item = _LIST_ITEM.match(line)
        if item and normalize_name(item.group("value")) in view.blocked_repos:
            return None
    return _redact(line, view)


def _frontmatter_span(lines: Sequence[str]) -> int:
    """How many leading lines are frontmatter that must be removed: an
    unparseable or unclosed block, or YAML-shaped lines before a lone
    delimiter (a frontmatter fragment of unknown validity); 0 otherwise."""
    if not lines:
        return 0
    delimiters = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if lines[0].strip() == "---":
        closing = next((i for i in delimiters if i > 0), None)
        if closing is None:
            return len(lines)
        try:
            parse_frontmatter_strict("\n".join(lines[: closing + 1]) + "\n")
        except (yaml.YAMLError, ValueError):
            return closing + 1
        return 0
    if delimiters:
        head = lines[: delimiters[0]]
        if any(line.strip() for line in head) and all(
            _YAML_SHAPED.match(line) for line in head
        ):
            return delimiters[0] + 1
    return 0


def _diagnose(relative_path: str, fingerprint: str, line_numbers: List[int]) -> None:
    key = (relative_path, fingerprint)
    with _lock:
        if key in _diagnosed:
            return
        if len(_diagnosed) >= _DIAGNOSTIC_LIMIT:
            _diagnosed.clear()
        _diagnosed.add(key)
    logger.warning(
        "Catalog document %s: %d malformed line(s) removed for restricted "
        "callers (lines %s)",
        relative_path,
        len(line_numbers),
        ", ".join(str(n) for n in line_numbers[:20]),
    )


_UNRECOGNISED_TABLE = (None, -1)


def _compute(
    lines: Sequence[str], relative_path: str, fingerprint: str, view: _View
) -> List[Optional[str]]:
    span = _frontmatter_span(lines)
    out: List[Optional[str]] = [None] * span
    malformed: List[int] = list(range(1, span + 1))
    # None: outside a table; (kind, width) inside one; kind None: unrecognised.
    table: Optional[Tuple[Optional[TableKind], int]] = None
    for index in range(span, len(lines)):
        line = lines[index]
        cells = table_cells(line)
        if cells is None:
            table = None
            out.append(_project_text(line, view))
            continue
        if table is None:
            following = (
                table_cells(lines[index + 1]) if index + 1 < len(lines) else None
            )
            kind = None if is_separator(cells) else table_kind(cells)
            if following is not None and is_separator(following) and kind:
                table = (kind, len(cells))
                out.append(_redact(line, view))
                continue
            table = _UNRECOGNISED_TABLE
        kind, width = table
        if kind is None or len(cells) != width:
            malformed.append(index + 1)
            out.append(None)
        elif is_separator(cells):
            out.append(line)
        else:
            out.append(_project_row(line, kind, cells, view))
    if malformed:
        _diagnose(relative_path, fingerprint, malformed)
    return out


def project_lines(
    lines: Sequence[str],
    relative_path: str,
    vis: MetaVisibility,
    model: DepMapModel,
    universe: UniverseSnapshot,
) -> List[Optional[str]]:
    """The caller's view of *lines* (a whole document or a chunk of one),
    aligned 1:1 with the input; ``None`` marks a removed line."""
    if vis.unrestricted:
        return list(lines)
    view = _view_for(model, universe, vis)
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    key = (digest, relative_path, model.fingerprint, universe.digest, vis.vis_hash)
    with _lock:
        cached = _projections.get(key)
        if cached is not None:
            _projections.move_to_end(key)
            _stats["projection_hits"] += 1
            return list(cached)
    projected = _compute(lines, relative_path, model.fingerprint, view)
    if len(projected) != len(lines):
        raise RuntimeError("projection lost line alignment")
    with _lock:
        _stats["projections_computed"] += 1
        _projections[key] = tuple(projected)
        while len(_projections) > PROJECTION_CACHE_MAX_ENTRIES:
            _projections.popitem(last=False)
    return projected


def project_meta_document(
    lines: Sequence[str],
    relative_path: str,
    vis: MetaVisibility,
    *,
    dep_map_dir: Path,
    universe: AliasUniverse,
) -> List[Optional[str]]:
    """One document through a one-shot ``MetaProjector`` (a door projecting
    several hits opens one ``MetaProjector`` per request instead)."""
    projector = MetaProjector(vis, dep_map_dir=dep_map_dir, universe=universe)
    return projector.project(lines, relative_path)


def project_chunk(
    chunk_lines: Sequence[str],
    first_line: int,
    document_lines: Sequence[str],
    relative_path: str,
    vis: MetaVisibility,
    model: DepMapModel,
    universe: UniverseSnapshot,
) -> List[Optional[str]]:
    """The caller's view of a chunk starting at 1-based *first_line* of the
    current *document_lines*: each line takes its verdict from the whole
    document's projection, so a chunk never shows more than the document.
    A chunk line that lies outside the document or differs from it (a stale
    index) is removed."""
    if first_line < 1:
        raise ValueError("first_line is 1-based")
    if vis.unrestricted:
        return list(chunk_lines)
    full = project_lines(document_lines, relative_path, vis, model, universe)
    out: List[Optional[str]] = []
    for offset, line in enumerate(chunk_lines):
        index = first_line - 1 + offset
        current = index < len(document_lines) and document_lines[index] == line
        out.append(full[index] if current else None)
    return out


class MetaProjector:
    """Per-request projection handle: the model and the universe are loaded
    once, at the first restricted projection, and reused for every hit of
    the request (an unrestricted caller costs no I/O). Create one per
    request; synchronous, so async doors call it through a worker thread."""

    def __init__(
        self, vis: MetaVisibility, *, dep_map_dir: Path, universe: AliasUniverse
    ) -> None:
        self._vis = vis
        self._dep_map_dir = Path(dep_map_dir)
        self._universe = universe
        self._lock = threading.Lock()
        self._loaded: Optional[Tuple[DepMapModel, UniverseSnapshot]] = None

    def _sources(self) -> Tuple[DepMapModel, UniverseSnapshot]:
        with self._lock:
            if self._loaded is None:
                self._loaded = (
                    load_depmap_model(self._dep_map_dir),
                    self._universe.snapshot(),
                )
            return self._loaded

    def project(self, lines: Sequence[str], relative_path: str) -> List[Optional[str]]:
        if self._vis.unrestricted:
            return list(lines)
        model, universe = self._sources()
        return project_lines(lines, relative_path, self._vis, model, universe)

    def project_chunk(
        self,
        chunk_lines: Sequence[str],
        first_line: int,
        document_lines: Sequence[str],
        relative_path: str,
    ) -> List[Optional[str]]:
        if first_line < 1:
            raise ValueError("first_line is 1-based")
        if self._vis.unrestricted:
            return list(chunk_lines)
        model, universe = self._sources()
        return project_chunk(
            chunk_lines,
            first_line,
            document_lines,
            relative_path,
            self._vis,
            model,
            universe,
        )


def render_line_aligned(projected: Sequence[Optional[str]]) -> str:
    """Removed lines as empty lines: line numbers stay valid (search,
    regex, paged reads)."""
    return "\n".join("" if line is None else line for line in projected)


def render_for_display(projected: Sequence[Optional[str]]) -> str:
    """Removed lines dropped: tables stay contiguous (wiki, Web UI)."""
    return "\n".join(line for line in projected if line is not None)
