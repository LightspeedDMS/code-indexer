"""The catalog path policy applied to wiki article paths.

Wiki doors name catalog documents as articles: the path without its
extension, ``""`` for the wiki home page. These helpers map an article to
its catalog-relative file and decide it with ``classify`` for one request
(dependency-map membership read fresh, at most once per domain per
request). Synchronous file I/O: async callers use a worker thread.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, TypeVar

from .membership import participants_reader
from .path_policy import DEP_MAP_DIR, Decision, DecisionKind, classify
from .visibility import MetaVisibility, memory_visibility_fn

T = TypeVar("T")
HOME_ARTICLE_FILE = "home.md"


def article_relative_path(article_path: str) -> str:
    """The catalog file an article names: ``""`` is ``home.md``; an
    extension-less path is its ``.md`` file."""
    if not article_path:
        return HOME_ARTICLE_FILE
    if PurePosixPath(article_path).suffix:
        return article_path
    return f"{article_path}.md"


def catalog_decider(
    catalog_root: Path, vis: MetaVisibility, access_service: Optional[Any]
) -> Callable[[str], Decision]:
    """``classify`` for one request over the catalog at *catalog_root*."""
    read_participants = participants_reader(Path(catalog_root) / DEP_MAP_DIR)
    memo: Dict[str, Optional[FrozenSet[str]]] = {}

    def participants(domain: str) -> Optional[FrozenSet[str]]:
        if domain not in memo:
            memo[domain] = read_participants(domain)
        return memo[domain]

    memory_fn = memory_visibility_fn(access_service, vis)
    return lambda relative_path: classify(relative_path, vis, participants, memory_fn)


def visible_articles(
    items: Iterable[T], key: Callable[[T], str], decide: Callable[[str], Decision]
) -> List[T]:
    """The *items* whose article path (``key(item)``) is not denied."""
    return [
        item
        for item in items
        if decide(article_relative_path(key(item))).kind is not DecisionKind.DENY
    ]
