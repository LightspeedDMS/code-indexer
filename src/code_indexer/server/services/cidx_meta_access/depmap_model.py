"""The dependency map parsed once per document set (no per-user data).

The model lists every domain and its participants, every repository name
that appears in a structured field, the analysed repositories and the
cross-domain edges of ``_index.md``. Projection derives each caller's view
from it.

Cache: a bounded per-process LRU. An immutable versioned snapshot is keyed
by its path (parsed once per process); a live directory is keyed by a stat
fingerprint of the directory and of every document in it, so any rewrite
yields a fresh model.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import threading
import unicodedata
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from ..dep_map_file_utils import load_domains_json
from ..dep_map_parser_hygiene import strip_backticks
from ..query_path_cache import is_immutable_versioned_snapshot
from ..repo_access_guard import normalize_repo_alias
from .membership import frontmatter_of, participants_from

INDEX_FILE = "_index.md"
DOMAINS_JSON = "_domains.json"
MODEL_CACHE_MAX_ENTRIES = 16

_ALIAS_SHAPE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_LINK = re.compile(r"^\[([^\]]*)\]\([^)]*\)$")
_WRAPPERS = "*_`\"'"
PLACEHOLDER_WORDS = frozenset({"none", "external", "n/a", "-", "empty"})
DECODE_PASSES = 3

_NEEDS_DECODE = re.compile(r"[&%<\\]")
_INVISIBLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f"
    "\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0]"
)
_INLINE_TAG = re.compile(r"<[^<>]*>")
_MD_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")

Edge = Tuple[str, str, FrozenSet[str]]


def needs_decoding(text: str) -> bool:
    """False when *text* is plain ASCII with nothing to decode."""
    return not text.isascii() or _NEEDS_DECODE.search(text) is not None


def decoded_form(text: str) -> str:
    """*text* as a Markdown renderer displays it: HTML entities and percent
    escapes decoded (a bounded number of passes, for nested encodings),
    NFKC-normalised, without invisible characters, inline tags or Markdown
    backslash escapes. Used for detection only, never for output."""
    if not needs_decoding(text):
        return text
    value = text
    for _attempt in range(DECODE_PASSES):
        decoded = urllib.parse.unquote(html.unescape(value))
        if decoded == value:
            break
        value = decoded
    value = _INVISIBLE.sub("", unicodedata.normalize("NFKC", value))
    return _MD_ESCAPE.sub(r"\1", _INLINE_TAG.sub("", value))


def normalize_name(text: str) -> str:
    """Comparison form of a name found in a document: its decoded form, link
    text, without emphasis, quote or backtick wrappers and ``-global``,
    lowercased."""
    value = decoded_form(text).strip()
    link = _LINK.match(value)
    if link:
        value = link.group(1).strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in _WRAPPERS:
        value = value[1:-1].strip()
    value = strip_backticks(value).strip()
    return normalize_repo_alias(value).lower()


def _alias_names(values: Iterable[str]) -> Set[str]:
    """Alias-shaped names; the placeholder words the generator writes into
    empty structured cells are never names."""
    names = set()
    for value in values:
        name = normalize_name(value)
        if _ALIAS_SHAPE.match(name) and name not in PLACEHOLDER_WORDS:
            names.add(name)
    return names


@dataclass(frozen=True)
class DepMapModel:
    """All names lowercased; ``domain_display`` maps them back for output."""

    fingerprint: str
    participants: Mapping[str, FrozenSet[str]]
    domains: FrozenSet[str]
    domain_display: Mapping[str, str]
    structured_names: FrozenSet[str]
    analyzed_repos: FrozenSet[str]
    edges: Tuple[Edge, ...]

    def visible_domains(self, visible_lc: FrozenSet[str]) -> FrozenSet[str]:
        """Domains with at least one visible participant."""
        return frozenset(
            domain for domain, repos in self.participants.items() if repos & visible_lc
        )


EMPTY_MODEL = DepMapModel("absent", {}, frozenset(), {}, frozenset(), frozenset(), ())


@dataclass(frozen=True)
class TableKind:
    """A recognised dependency-map table and the meaning of its columns.

    Only ``repo_columns``, ``domain_columns`` and ``list_column`` decide
    whether a row is kept; every other cell is free text.
    """

    name: str
    repo_columns: Tuple[int, ...] = ()
    domain_columns: Tuple[int, ...] = ()
    list_column: Optional[int] = None
    count_column: Optional[int] = None


ROLES = TableKind("roles", repo_columns=(0,))
OUTGOING = TableKind("outgoing", repo_columns=(0, 1), domain_columns=(2,))
INCOMING = TableKind("incoming", repo_columns=(0, 1), domain_columns=(2,))
MATRIX = TableKind("matrix", repo_columns=(0,), domain_columns=(1,))
CATALOG = TableKind("catalog", domain_columns=(0,), count_column=2)
GRAPH = TableKind("graph", domain_columns=(0, 1), list_column=2)
INTRA = TableKind("intra", repo_columns=(0, 1))
DEP_TYPES = TableKind("dep_types")

_SEPARATOR_CELL = re.compile(r"^:?-+:?$")
_TRAILING_NOTE = re.compile(r"\s*\([^()]*\)$")


def table_cells(line: str) -> Optional[List[str]]:
    """The cells of a ``|``-delimited line, or None when it is not one."""
    stripped = line.strip()
    if len(stripped) < 2 or not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def is_separator(cells: List[str]) -> bool:
    return bool(cells) and all(_SEPARATOR_CELL.match(cell) for cell in cells)


def table_kind(header: List[str]) -> Optional[TableKind]:
    """The kind of table whose header row (the row right before a separator)
    is *header*, compared case-insensitively; None when unrecognised."""
    names = [" ".join(cell.strip("*").split()).lower() for cell in header]
    first = names[0] if names else ""
    if first in ("repository", "repo"):
        if names[1:] in (["domain"], ["domains"]):
            return MATRIX
        return ROLES if len(names) >= 3 else None
    if names[:2] == ["source repo", "target repo"]:
        return INTRA if len(names) >= 3 else None
    if names == ["type", "description", "example"]:
        return DEP_TYPES
    if names[:3] == ["this repo", "depends on", "target domain"]:
        return OUTGOING if len(names) >= 4 else None
    if names[:3] == ["external repo", "depends on", "source domain"]:
        return INCOMING if len(names) >= 6 else None
    if names == ["domain", "description", "repo count"]:
        return CATALOG
    if names[:3] == ["source domain", "target domain", "via repos"]:
        return GRAPH if len(names) >= 5 else None
    return None


def cell_names(cell: str) -> List[str]:
    """Comparison names of a structured cell: each comma entry as a whole
    value, or the name before a trailing ``(note)``."""
    names = []
    for entry in cell.split(","):
        name = normalize_name(entry)
        if not _ALIAS_SHAPE.match(name):
            name = normalize_name(_TRAILING_NOTE.sub("", entry.strip()))
        names.append(name)
    return names


def single_repo_name(cell: str) -> Optional[str]:
    """The one repository name a structured cell holds (link, backticks and
    a trailing ``(note)`` normalised), or a placeholder word; None for a
    list, a slash pair, punctuation around the name, a non-ASCII value, an
    unbalanced backtick or a pipe."""
    if cell.count("`") % 2 or "|" in cell:
        return None
    names = cell_names(cell)
    if len(names) != 1:
        return None
    name = names[0]
    if name in PLACEHOLDER_WORDS or _ALIAS_SHAPE.match(name):
        return name
    return None


def gfm_cells(line: str) -> Optional[List[str]]:
    """Cells of a GFM table line whose outer pipes are optional; None when
    the line has no pipe."""
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def is_gfm_separator(line: str) -> bool:
    """A GFM delimiter row: a pipe, and every cell ``:?-+:?``."""
    if "-" not in line:
        return False
    cells = gfm_cells(line)
    if not cells:
        return False
    return all(_SEPARATOR_CELL.match(cell) for cell in cells)


_HTML_TABLE = re.compile(
    r"<\s*/?\s*(?:table|thead|tbody|tfoot|tr|td|th)(?![A-Za-z0-9])", re.IGNORECASE
)
_CODE_FENCE = re.compile(r"^ {0,3}(?:```|~~~)")

LINE_TEXT = "text"
LINE_HEADER = "header"
LINE_SEPARATOR = "separator"
LINE_ROW = "row"
LINE_DROP = "drop"

#: (role, table kind, the row's cells for a ROW line else None)
LineRole = Tuple[str, Optional[TableKind], Optional[List[str]]]


def _is_piped(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 2 and stripped[0] == "|" and stripped[-1] == "|"


def _table_body_role(line: str, kind: Optional[TableKind], width: int) -> LineRole:
    cells = table_cells(line)
    if kind is None or cells is None or len(cells) != width:
        return LINE_DROP, kind, None
    if is_separator(cells):
        return LINE_SEPARATOR, kind, None
    if any(cell.count("`") % 2 for cell in cells):
        return LINE_DROP, kind, None
    return LINE_ROW, kind, cells


def _in_table_body(line: str) -> bool:
    """A table body runs to a blank line or a code fence."""
    return bool(line.strip()) and not _CODE_FENCE.match(line)


def classify_lines(lines: Sequence[str]) -> List[LineRole]:
    """The role of every line under GFM table structure: a line followed by
    a delimiter row is a header; the table runs to the next blank line (or
    code fence), and a body line is a ROW only as an outer-piped row of the
    header's width. An unrecognised header drops its whole table, and so does
    a header whose width differs from the delimiter row's. Outside tables, an
    outer-piped row and HTML table markup are dropped."""
    roles: List[LineRole] = []
    total = len(lines)
    index = 0
    while index < total:
        line = lines[index]
        following = lines[index + 1] if index + 1 < total else ""
        if line.strip() and is_gfm_separator(following) and not is_gfm_separator(line):
            header = gfm_cells(line) or [line.strip()]
            width = len(header)
            kind = (
                table_kind(header) if width == len(gfm_cells(following) or []) else None
            )
            if kind is None:
                roles += [(LINE_DROP, None, None), (LINE_DROP, None, None)]
            else:
                roles += [(LINE_HEADER, kind, None), (LINE_SEPARATOR, kind, None)]
            index += 2
            while index < total and _in_table_body(lines[index]):
                roles.append(_table_body_role(lines[index], kind, width))
                index += 1
            continue
        dropped = _is_piped(line) or (
            "<" in line and _HTML_TABLE.search(line) is not None
        )
        roles.append((LINE_DROP if dropped else LINE_TEXT, None, None))
        index += 1
    return roles


def iter_recognised_rows(
    lines: Sequence[str],
) -> Iterator[Tuple[TableKind, List[str]]]:
    """Data rows of recognised tables, as the projection keeps them."""
    for role, kind, cells in classify_lines(lines):
        if role == LINE_ROW and kind is not None and cells is not None:
            yield kind, cells


def _table_names(lines: Sequence[str]) -> Set[str]:
    cells: List[str] = []
    for kind, row in iter_recognised_rows(lines):
        cells.extend(row[column] for column in kind.repo_columns)
        if kind.list_column is not None:
            cells.append(row[kind.list_column])
    return _alias_names(name for cell in cells for name in cell_names(cell))


def _index_parts(content: str) -> Tuple[Optional[Set[str]], Set[str], List[Edge]]:
    frontmatter = frontmatter_of(content) or {}
    analyzed_raw = frontmatter.get("repos_analyzed")
    analyzed = (
        _alias_names(str(item) for item in analyzed_raw)
        if isinstance(analyzed_raw, list)
        else None
    )
    lines = content.splitlines()
    edges: List[Edge] = []
    for kind, row in iter_recognised_rows(lines):
        if kind is GRAPH:
            via = frozenset(_alias_names(row[2].split(",")))
            edges.append((normalize_name(row[0]), normalize_name(row[1]), via))
    return analyzed, _table_names(lines), edges


def build_depmap_model(dep_map_dir: Path, fingerprint: str) -> DepMapModel:
    """Parse the dependency-map directory (reads every document once)."""
    entries = {
        str(d["name"]): d
        for d in load_domains_json(dep_map_dir)
        if isinstance(d, dict) and d.get("name")
    }
    files: Dict[str, Path] = {}
    for p in dep_map_dir.glob("*.md"):
        if p.name.startswith("_"):
            continue
        if p.is_symlink() or not p.is_file():
            _skip_document(str(p))
            continue
        files[p.stem] = p
    participants: Dict[str, FrozenSet[str]] = {}
    display: Dict[str, str] = {}
    structured: Set[str] = set()
    for name in sorted(set(entries) | set(files)):
        key = normalize_name(name)
        display[key] = name
        content = files[name].read_text(encoding="utf-8") if name in files else ""
        members = participants_from(
            frontmatter_of(content) if content else None, entries.get(name)
        )
        if members:
            lowered = frozenset(m.lower() for m in members)
            participants[key] = lowered
            structured |= lowered
        if content:
            structured |= _table_names(content.splitlines())
    analyzed: Optional[Set[str]] = None
    edges: List[Edge] = []
    index_file = dep_map_dir / INDEX_FILE
    if index_file.is_file() and not index_file.is_symlink():
        analyzed, matrix, edges = _index_parts(index_file.read_text(encoding="utf-8"))
        structured |= matrix
        structured |= analyzed or set()
        for _source, _target, via in edges:
            structured |= via
    if analyzed is None:
        analyzed = set().union(*participants.values()) if participants else set()
    return DepMapModel(
        fingerprint=fingerprint,
        participants=participants,
        domains=frozenset(display),
        domain_display=display,
        structured_names=frozenset(structured),
        analyzed_repos=frozenset(analyzed),
        edges=tuple(edges),
    )


FINGERPRINT_ATTEMPTS = 5
_SKIPPED_LIMIT = 4096

logger = logging.getLogger(__name__)
_skipped_documents: Set[str] = set()
_skipped_lock = threading.Lock()


def _skip_document(path: str) -> None:
    """One WARNING per skipped path: a symlink or non-regular ``*.md`` is
    never read as a document."""
    with _skipped_lock:
        if path in _skipped_documents:
            return
        if len(_skipped_documents) >= _SKIPPED_LIMIT:
            _skipped_documents.clear()
        _skipped_documents.add(path)
    logger.warning(
        "Dependency-map entry %s is a symlink or not a regular file; skipped", path
    )


def _scan_fingerprint(dep_map_dir: Path) -> Optional[str]:
    """One scan-and-stat pass; raises FileNotFoundError when a listed
    document vanished before its stat (the directory is changing)."""
    try:
        st = dep_map_dir.stat()
    except FileNotFoundError:
        return None
    parts = [f".:{st.st_mtime_ns}:{st.st_ino}"]
    with os.scandir(dep_map_dir) as scan:
        names = sorted(
            (e.name, e)
            for e in scan
            if e.name.endswith(".md") or e.name == DOMAINS_JSON
        )
    for name, entry in names:
        if name.endswith(".md") and (
            entry.is_symlink() or not entry.is_file(follow_symlinks=False)
        ):
            _skip_document(entry.path)
            continue
        est = entry.stat()
        parts.append(f"{name}:{est.st_mtime_ns}:{est.st_size}:{est.st_ino}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _live_fingerprint(dep_map_dir: Path) -> Optional[str]:
    """Stat fingerprint of the directory and its documents; None if absent."""
    for _attempt in range(FINGERPRINT_ATTEMPTS):
        try:
            return _scan_fingerprint(dep_map_dir)
        except FileNotFoundError:
            continue
    raise RuntimeError(
        f"Dependency map {dep_map_dir} kept changing during "
        f"{FINGERPRINT_ATTEMPTS} fingerprint attempts"
    )


_cache: "OrderedDict[Tuple[str, ...], DepMapModel]" = OrderedDict()
_cache_lock = threading.Lock()


def _cached(key: Tuple[str, ...]) -> Optional[DepMapModel]:
    with _cache_lock:
        model = _cache.get(key)
        if model is not None:
            _cache.move_to_end(key)
        return model


def _store(key: Tuple[str, ...], model: DepMapModel) -> DepMapModel:
    with _cache_lock:
        _cache[key] = model
        _cache.move_to_end(key)
        while len(_cache) > MODEL_CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)
    return model


def load_depmap_model(dep_map_dir: Path) -> DepMapModel:
    """The model of *dep_map_dir*, from the per-process cache when current.

    Synchronous file I/O: async callers run it through
    ``anyio.to_thread.run_sync``.
    """
    path = Path(dep_map_dir)
    if is_immutable_versioned_snapshot(str(path)):
        key: Tuple[str, ...] = ("snapshot", str(path))
        model = _cached(key)
        if model is not None:
            return model
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        return _store(key, build_depmap_model(path, f"snapshot:{digest}"))
    fingerprint = _live_fingerprint(path)
    if fingerprint is None:
        return EMPTY_MODEL
    key = ("live", str(path), fingerprint)
    model = _cached(key)
    if model is not None:
        return model
    return _store(key, build_depmap_model(path, f"live:{fingerprint}"))
