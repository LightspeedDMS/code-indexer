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
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

from ..dep_map_file_utils import load_domains_json
from ..dep_map_parser_hygiene import strip_backticks
from ..dep_map_parser_tables import iter_table_rows
from ..query_path_cache import is_immutable_versioned_snapshot
from ..repo_access_guard import normalize_repo_alias
from .membership import frontmatter_of, participants_from

INDEX_FILE = "_index.md"
DOMAINS_JSON = "_domains.json"
MODEL_CACHE_MAX_ENTRIES = 16

_ALIAS_SHAPE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_LINK = re.compile(r"^\[([^\]]*)\]\([^)]*\)$")
_WRAPPERS = "*_`\"'"

Edge = Tuple[str, str, FrozenSet[str]]


def normalize_name(text: str) -> str:
    """Comparison form of a name found in a document: link text, without
    emphasis, quote or backtick wrappers and ``-global``, lowercased."""
    value = text.strip()
    link = _LINK.match(value)
    if link:
        value = link.group(1).strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in _WRAPPERS:
        value = value[1:-1].strip()
    value = strip_backticks(value).strip()
    return normalize_repo_alias(value).lower()


def _alias_names(values: Iterable[str]) -> Set[str]:
    names = set()
    for value in values:
        name = normalize_name(value)
        if _ALIAS_SHAPE.match(name):
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


def _table_names(content: str) -> Set[str]:
    cells: List[str] = []
    for row in iter_table_rows(content, "## Repository Roles", 3, "Repository"):
        cells.append(row[0])
    for row in iter_table_rows(content, "### Outgoing Dependencies", 4, "This Repo"):
        cells.extend(row[:2])
    for row in iter_table_rows(
        content, "### Incoming Dependencies", 6, "External Repo"
    ):
        cells.extend(row[:2])
    return _alias_names(cells)


def _index_parts(content: str) -> Tuple[Optional[Set[str]], Set[str], List[Edge]]:
    frontmatter = frontmatter_of(content) or {}
    analyzed_raw = frontmatter.get("repos_analyzed")
    analyzed = (
        _alias_names(str(item) for item in analyzed_raw)
        if isinstance(analyzed_raw, list)
        else None
    )
    matrix = _alias_names(
        row[0]
        for row in iter_table_rows(content, "## Repo-to-Domain Matrix", 2, "Repository")
    )
    edges: List[Edge] = []
    for row in iter_table_rows(
        content, "## Cross-Domain Dependencies", 5, "Source Domain"
    ):
        via = frozenset(_alias_names(row[2].split(",")))
        edges.append((normalize_name(row[0]), normalize_name(row[1]), via))
    return analyzed, matrix, edges


def build_depmap_model(dep_map_dir: Path, fingerprint: str) -> DepMapModel:
    """Parse the dependency-map directory (reads every document once)."""
    entries = {
        str(d["name"]): d
        for d in load_domains_json(dep_map_dir)
        if isinstance(d, dict) and d.get("name")
    }
    files = {
        p.stem: p
        for p in dep_map_dir.glob("*.md")
        if not p.name.startswith("_") and p.is_file()
    }
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
            structured |= _table_names(content)
    analyzed: Optional[Set[str]] = None
    edges: List[Edge] = []
    index_file = dep_map_dir / INDEX_FILE
    if index_file.is_file():
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


def _live_fingerprint(dep_map_dir: Path) -> Optional[str]:
    """Stat fingerprint of the directory and its documents; None if absent."""
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
        est = entry.stat()
        parts.append(f"{name}:{est.st_mtime_ns}:{est.st_size}:{est.st_ino}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


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
