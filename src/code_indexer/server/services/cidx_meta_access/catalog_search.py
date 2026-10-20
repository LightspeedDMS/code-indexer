"""One repository's search for one caller, catalog-aware.

``SemanticQueryManager._perform_search`` runs every repository of a request
through ``search_for_caller``. A repository whose golden identity is not the
catalog, and every unrestricted caller, get exactly today's single pass. A
restricted caller searching the catalog gets its hits projected
(``project_meta_hits``) before anything sorts, limits, reranks or caches
them, with bounded deepening so hidden top hits do not leave the page short:
round r fetches ``limit * 2**(r + 1)`` raw hits (capped at
``META_DEEPEN_MAX_K``), at most ``META_DEEPEN_MAX_ROUNDS`` rounds, stopping
early when the page is full, the corpus is exhausted, or the request's
search budget is spent. Constants, not settings.

Repeated rounds embed the same query text, which the server's query
embedding cache answers after the first round.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .hit_projection import (
    CatalogScope,
    TextRematch,
    open_catalog_scope,
    project_meta_hits,
)
from ..constants import CIDX_META_REPO_GLOBAL, is_internal_meta_repo
from .universe_provider import registry_of
from .visibility import (
    is_internal_meta_identity,
    meta_identity,
    resolve_meta_visibility,
)

logger = logging.getLogger(__name__)

META_DEEPEN_MAX_ROUNDS = 4
META_DEEPEN_MAX_K = 1024

RunRound = Callable[[int, str], List[Any]]
Merge = Callable[[List[Any], List[Any], int], List[Any]]


class CatalogProjectionUnavailable(RuntimeError):
    """A catalog search cannot be projected (no running server state)."""


def server_app_state() -> Optional[Any]:
    """The running server's app state (the access service and registry
    every catalog door reads), or None when no app is initialised; never
    builds one."""
    from code_indexer.server.app import peek_app

    app = peek_app()
    return None if app is None else app.state


class _LazyScope:
    """The restricted caller's scope, opened at the first projection (after
    the first raw round ran, so a search error surfaces unchanged)."""

    def __init__(self, open_scope: Callable[[], Optional[CatalogScope]]) -> None:
        self._open = open_scope
        self._scope: Optional[CatalogScope] = None

    def get(self) -> CatalogScope:
        if self._scope is None:
            scope = self._open()
            if scope is None:
                raise CatalogProjectionUnavailable("caller became unrestricted")
            self._scope = scope
        return self._scope


def _labels_catalog(row: Any) -> bool:
    for key in ("repository_alias", "source_repo"):
        value = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
        if isinstance(value, str) and is_internal_meta_repo(value):
            return True
    return False


def _served_catalog_root(state: Any) -> Optional[Path]:
    from code_indexer.global_repos.alias_manager import AliasManager

    golden_repos_dir = getattr(state, "golden_repos_dir", None)
    if not golden_repos_dir:
        return None
    target = AliasManager(str(Path(golden_repos_dir) / "aliases")).read_alias(
        CIDX_META_REPO_GLOBAL
    )
    return Path(target) if target else None


def project_grouped_results(
    grouped: Mapping[str, List[Any]],
    username: str,
    access_service: Optional[Any],
    rematch: Optional[TextRematch] = None,
) -> Dict[str, List[Any]]:
    """Results grouped by repository alias (multi-repository search) with
    every catalog group's rows projected for the caller; each catalog row
    is labelled with its group alias first. Other groups are untouched."""
    out: Dict[str, List[Any]] = {}
    for alias, rows in grouped.items():
        if is_internal_meta_repo(alias):
            labelled = [dict(row, repository_alias=alias) for row in rows]
            out[alias] = project_catalog_rows(
                labelled, username, access_service, rematch
            )
        else:
            out[alias] = rows
    return out


def reproject_job_result(
    result: object, username: str, access_service: Optional[Any]
) -> object:
    """A stored job result as the caller may see it NOW: the catalog rows of
    its ``results`` list projected with the caller's current visibility
    (a revoked grant takes effect on the next read). Any other value, or a
    result with nothing to project, is returned unchanged."""
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return result
    rows = project_catalog_rows(result["results"], username, access_service)
    if rows is result["results"]:
        return result
    updated = dict(result)
    updated["results"] = rows
    if "total_results" in updated:
        updated["total_results"] = len(rows)
    return updated


def project_catalog_rows(
    rows: List[Any],
    username: str,
    access_service: Optional[Any],
    rematch: Optional[TextRematch] = None,
) -> List[Any]:
    """*rows* in order, every catalog-labelled row projected for the caller
    over the copy the ``cidx-meta-global`` alias serves (and re-matched
    with *rematch* when given); other rows are untouched, and the same list
    object is returned when nothing is catalog or the caller is
    unrestricted. For a restricted caller, catalog rows are dropped when
    that copy, the registry or the scope cannot be obtained."""
    if not any(_labels_catalog(row) for row in rows):
        return rows
    if resolve_meta_visibility(username, access_service).unrestricted:
        return rows
    state = server_app_state()
    root = None if state is None else _served_catalog_root(state)
    registry = None if state is None else registry_of(state)
    scope = (
        open_catalog_scope(username, access_service, root, registry)
        if root is not None and registry is not None
        else None
    )
    if scope is None:
        logger.warning("Catalog copy unresolvable; catalog rows dropped")
    out: List[Any] = []
    for row in rows:
        if not _labels_catalog(row):
            out.append(row)
        elif scope is not None:
            out.extend(project_meta_hits([row], scope, rematch))
    return out


def _round(
    run: RunRound,
    k: int,
    mode: str,
    scope: _LazyScope,
    rematch: TextRematch,
    merge: Merge,
) -> Tuple[List[Any], bool]:
    """One round's projected page and whether the corpus is exhausted."""
    if mode == "hybrid":
        fts_raw = run(k, "fts")
        semantic_raw = run(k, "semantic")
        page = merge(
            project_meta_hits(fts_raw, scope.get(), rematch),
            project_meta_hits(semantic_raw, scope.get()),
            k,
        )
        return page, len(fts_raw) < k and len(semantic_raw) < k
    raw = run(k, mode)
    page = project_meta_hits(raw, scope.get(), rematch if mode == "fts" else None)
    return page, len(raw) < k


def search_for_caller(
    run: RunRound,
    *,
    username: str,
    repo_info: Mapping[str, Any],
    repo_path: Any,
    limit: int,
    search_mode: str,
    rematch: TextRematch,
    merge: Merge,
    budget_spent: Callable[[], bool],
) -> List[Any]:
    """One repository's hits for the caller (see the module docstring).
    *run(k, mode)* is the raw single-repository search."""
    if not is_internal_meta_identity(meta_identity(repo_info)):
        return run(limit, search_mode)
    state = server_app_state()
    if state is None:
        raise CatalogProjectionUnavailable("catalog search needs the server state")
    access_service = getattr(state, "access_filtering_service", None)
    if resolve_meta_visibility(username, access_service).unrestricted:
        return run(limit, search_mode)
    scope = _LazyScope(
        lambda: open_catalog_scope(
            username, access_service, Path(repo_path), registry_of(state)
        )
    )
    requested = max(1, limit)
    page: List[Any] = []
    for round_index in range(META_DEEPEN_MAX_ROUNDS):
        k = min(requested * 2 ** (round_index + 1), META_DEEPEN_MAX_K)
        logger.debug("catalog round %d k=%d", round_index, k)
        page, exhausted = _round(run, k, search_mode, scope, rematch, merge)
        if (
            len(page) >= requested
            or exhausted
            or k >= META_DEEPEN_MAX_K
            or budget_spent()
        ):
            break
    return page[:requested]
