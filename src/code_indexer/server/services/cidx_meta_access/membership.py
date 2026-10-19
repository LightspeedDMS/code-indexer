"""Which repositories participate in a dependency-map domain.

Membership comes from the domain file's frontmatter ``participating_repos``,
else from ``_domains.json``. Older or refined files may carry neither; then
membership is unknown (None) and the domain stays hidden from restricted
callers, with one operator diagnostic per (path, file fingerprint).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, FrozenSet, Mapping, Optional, Set, Tuple

from ..dep_map_file_utils import load_domains_json, parse_yaml_frontmatter
from ..repo_access_guard import normalize_repo_alias

logger = logging.getLogger(__name__)

_WARNED_LIMIT = 4096
_warned: Set[Tuple[str, Tuple[int, int, int]]] = set()
_warned_lock = threading.Lock()


def _names(value: Any) -> FrozenSet[str]:
    if not isinstance(value, list):
        return frozenset()
    names = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip().strip("\"'").strip()
        if name:
            names.add(normalize_repo_alias(name))
    return frozenset(names)


def participants_from(
    frontmatter: Optional[Mapping[str, Any]],
    domains_entry: Optional[Mapping[str, Any]],
) -> Optional[FrozenSet[str]]:
    """Frontmatter membership, else ``_domains.json`` membership, else None."""
    if frontmatter:
        names = _names(frontmatter.get("participating_repos"))
        if names:
            return names
    if domains_entry:
        names = _names(domains_entry.get("participating_repos"))
        if names:
            return names
    return None


def domain_participants(dep_map_dir: Path, domain: str) -> Optional[FrozenSet[str]]:
    """Read one domain's membership fresh from disk (no caching)."""
    if not domain or "/" in domain or domain in (".", ".."):
        raise ValueError(f"not a domain name: {domain!r}")
    md_file = Path(dep_map_dir) / f"{domain}.md"
    frontmatter = None
    if md_file.is_file():
        frontmatter = parse_yaml_frontmatter(md_file.read_text(encoding="utf-8"))
    entry = next(
        (d for d in load_domains_json(Path(dep_map_dir)) if d.get("name") == domain),
        None,
    )
    return participants_from(frontmatter, entry)


def _fingerprint(path: Path) -> Tuple[int, int, int]:
    try:
        st = path.stat()
    except FileNotFoundError:
        return (0, 0, 0)
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _warn_unknown_membership(md_file: Path) -> None:
    key = (str(md_file), _fingerprint(md_file))
    with _warned_lock:
        if key in _warned:
            return
        if len(_warned) >= _WARNED_LIMIT:
            _warned.clear()
        _warned.add(key)
    logger.warning(
        "Dependency-map domain file %s names no participating repositories "
        "(frontmatter and _domains.json); hidden from restricted callers",
        md_file,
    )


def participants_reader(
    dep_map_dir: Path,
) -> Callable[[str], Optional[FrozenSet[str]]]:
    """A ``participants_fn`` for ``classify`` that reads membership fresh."""

    def _read(domain: str) -> Optional[FrozenSet[str]]:
        participants = domain_participants(dep_map_dir, domain)
        if participants is None:
            _warn_unknown_membership(Path(dep_map_dir) / f"{domain}.md")
        return participants

    return _read
