"""Line projection of a catalog document for one caller.

The result is aligned 1:1 with the input lines; ``None`` marks a removed
line. Every rule looks at one line, so a stored search chunk projects
exactly like the same lines of the whole file (the only exceptions need
context a chunk may lack: malformed table rows are recognised once the
table's header was seen, and unparseable frontmatter once its closing
``---`` was seen; a chunk without that context still gets every per-line
rule, so no hidden name passes).

Structured names are filtered exactly (table rows, comma lists, YAML list
items, counts); exact hidden aliases in prose become
``[restricted repository]``. Unrestricted callers get the input unchanged.

Callers are synchronous; async doors run them through
``anyio.to_thread.run_sync``.
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
from .depmap_model import DepMapModel, load_depmap_model, normalize_name
from .visibility import MetaVisibility

logger = logging.getLogger(__name__)

RESTRICTED_PLACEHOLDER = "[restricted repository]"
MIN_PROSE_ALIAS_LENGTH = 4
VIEW_CACHE_MAX_ENTRIES = 64
PROJECTION_CACHE_MAX_ENTRIES = 256
_DIAGNOSTIC_LIMIT = 4096

_ALIAS_CHARS = "A-Za-z0-9_-"
_LEADING_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]*")
_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_LIST_ITEM = re.compile(r"^\s*-\s+(?P<value>.+?)\s*$")
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
    hidden: FrozenSet[str]
    known: FrozenSet[str]
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
    repo_names = {
        name
        for name in (universe_names | model.structured_names) - visible
        if not is_internal_meta_repo(name)
    }
    hidden = frozenset(repo_names | (model.domains - visible_domains - visible))
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
        hidden=hidden,
        known=frozenset(
            universe_names | model.structured_names | visible | model.domains
        ),
        domain_counts={
            d: len(model.participants[d] & visible) for d in visible_domains
        },
        counts={
            "repos_analyzed_count": len(model.analyzed_repos & visible),
            "domains_count": len(visible_domains),
        },
        summary=summary,
        prose=_redaction_regex(
            [name for name in hidden if len(name) >= MIN_PROSE_ALIAS_LENGTH]
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
    return view.prose.sub(RESTRICTED_PLACEHOLDER, line)


def _cell_hidden(cell: str, view: _View) -> bool:
    name = normalize_name(cell)
    if name in view.hidden:
        return True
    token = _LEADING_TOKEN.match(name)
    if token is None:
        return False
    lead = normalize_repo_alias(token.group(0))
    return len(lead) >= MIN_PROSE_ALIAS_LENGTH and lead in view.hidden


def _project_row(line: str, cells: List[str], view: _View) -> Optional[str]:
    changed = False
    projected: List[str] = []
    for cell in cells:
        entries = [entry.strip() for entry in cell.split(",")]
        if len(entries) >= 2 and all(
            entry and normalize_name(entry) in view.known for entry in entries
        ):
            kept = [e for e in entries if normalize_name(e) not in view.hidden]
            if not kept:
                return None
            if len(kept) != len(entries):
                cell, changed = ", ".join(kept), True
        elif _cell_hidden(cell, view):
            return None
        projected.append(cell)
    domain = normalize_name(projected[0]) if projected else ""
    if len(projected) == 3 and domain in view.domain_counts and projected[2].isdigit():
        count = str(view.domain_counts[domain])
        if count != projected[2]:
            projected[2], changed = count, True
    if changed:
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
        kept = [e for e in entries if normalize_name(e) not in view.hidden]
        if len(kept) != len(entries):
            line = f"{inline.group('prefix')}[{', '.join(kept)}]"
    else:
        item = _LIST_ITEM.match(line)
        if item and normalize_name(item.group("value")) in view.hidden:
            return None
    return _redact(line, view)


def _table_cells(line: str) -> Optional[List[str]]:
    stripped = line.strip()
    if len(stripped) < 2 or not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _frontmatter_end(lines: Sequence[str]) -> Optional[int]:
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index
    return None


def _diagnose(relative_path: str, digest: str, line_numbers: List[int]) -> None:
    key = (relative_path, digest)
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


def _compute(
    lines: Sequence[str], relative_path: str, digest: str, view: _View
) -> List[Optional[str]]:
    out: List[Optional[str]] = []
    malformed: List[int] = []
    start = 0
    closing = _frontmatter_end(lines)
    if closing is not None:
        try:
            parse_frontmatter_strict("\n".join(lines[: closing + 1]) + "\n")
        except (yaml.YAMLError, ValueError):
            out = [None] * (closing + 1)
            malformed.extend(range(1, closing + 2))
            start = closing + 1
    expected: Optional[int] = None
    for index in range(start, len(lines)):
        line = lines[index]
        cells = _table_cells(line)
        if cells is None:
            expected = None
            out.append(_project_text(line, view))
        elif all(_SEPARATOR_CELL.match(cell) for cell in cells):
            expected = len(cells)
            out.append(line)
        elif expected is not None and len(cells) != expected:
            malformed.append(index + 1)
            out.append(None)
        else:
            out.append(_project_row(line, cells, view))
    if malformed:
        _diagnose(relative_path, digest, malformed)
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
    projected = _compute(lines, relative_path, digest, view)
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
    """``project_lines`` that loads the model and the universe only for a
    restricted caller (an unrestricted caller costs no I/O)."""
    if vis.unrestricted:
        return list(lines)
    return project_lines(
        lines, relative_path, vis, load_depmap_model(dep_map_dir), universe.snapshot()
    )


def render_line_aligned(projected: Sequence[Optional[str]]) -> str:
    """Removed lines as empty lines: line numbers stay valid (search,
    regex, paged reads)."""
    return "\n".join("" if line is None else line for line in projected)


def render_for_display(projected: Sequence[Optional[str]]) -> str:
    """Removed lines dropped: tables stay contiguous (wiki, Web UI)."""
    return "\n".join(line for line in projected if line is not None)
