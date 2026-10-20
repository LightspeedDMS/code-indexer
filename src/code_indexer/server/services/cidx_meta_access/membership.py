"""Which repositories participate in a dependency-map domain.

Membership comes from the domain file's frontmatter ``participating_repos``
(block or flow list, parsed with the strict YAML helper), else from
``_domains.json``, else from the repository column of the file's recognised
roles table (``depmap_model.document_participants``, the one derivation the
model uses too). When none names a repository, membership is unknown (None)
and the domain stays hidden from restricted callers, with one operator
diagnostic per (path, file fingerprint).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Optional, Set, Tuple

from ..dep_map_file_utils import load_domains_json
from .depmap_model import DepMapModel, document_participants, load_depmap_model

logger = logging.getLogger(__name__)

_WARNED_LIMIT = 4096
_warned: Set[Tuple[str, Tuple[int, int, int]]] = set()
_warned_lock = threading.Lock()

ParticipantsReader = Callable[[str], Optional[FrozenSet[str]]]


def domain_participants(dep_map_dir: Path, domain: str) -> Optional[FrozenSet[str]]:
    """Read one domain's membership fresh from disk (no caching)."""
    if not domain or "/" in domain or domain in (".", ".."):
        raise ValueError(f"not a domain name: {domain!r}")
    md_file = Path(dep_map_dir) / f"{domain}.md"
    content = md_file.read_text(encoding="utf-8") if md_file.is_file() else ""
    entry = next(
        (d for d in load_domains_json(Path(dep_map_dir)) if d.get("name") == domain),
        None,
    )
    return document_participants(content, entry)


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
        "(frontmatter, _domains.json and roles table); hidden from restricted "
        "callers",
        md_file,
    )


def _diagnosed(
    dep_map_dir: Path, lookup: Callable[[str], Optional[FrozenSet[str]]]
) -> ParticipantsReader:
    def _read(domain: str) -> Optional[FrozenSet[str]]:
        participants = lookup(domain)
        if participants is None:
            _warn_unknown_membership(Path(dep_map_dir) / f"{domain}.md")
        return participants

    return _read


def participants_reader(dep_map_dir: Path) -> ParticipantsReader:
    """A ``participants_fn`` for ``classify`` that reads membership fresh."""
    return _diagnosed(
        dep_map_dir, lambda domain: domain_participants(dep_map_dir, domain)
    )


def request_participants_reader(
    dep_map_dir: Path, model: Optional[Callable[[], DepMapModel]] = None
) -> ParticipantsReader:
    """Membership for ONE request, answered from ONE dependency-map model:
    ``model()`` (the request's own) when given, else the model of
    *dep_map_dir* loaded on first use. No domain file is read per domain,
    and every decision of the request agrees on membership."""
    source = model or (lambda: load_depmap_model(dep_map_dir))
    loaded: Dict[str, DepMapModel] = {}

    def _lookup(domain: str) -> Optional[FrozenSet[str]]:
        if "model" not in loaded:
            loaded["model"] = source()
        return loaded["model"].domain_participants(domain)

    return _diagnosed(dep_map_dir, _lookup)


def model_participants_reader(
    dep_map_dir: Path, model: DepMapModel
) -> ParticipantsReader:
    """A ``participants_fn`` answering from an already loaded *model* of
    *dep_map_dir* (no file read)."""
    return _diagnosed(dep_map_dir, model.domain_participants)
