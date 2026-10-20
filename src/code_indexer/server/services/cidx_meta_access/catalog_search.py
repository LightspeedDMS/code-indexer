"""One repository's search for one caller, catalog-aware.

``SemanticQueryManager._perform_search`` runs every repository of a request
through ``search_for_caller``. A repository is the catalog when its golden
identity is, or when the directory actually searched is one of the
catalog's locations (an alias pointer or clone that serves the catalog).
Every other repository, and every unrestricted caller, gets exactly
today's single pass. A restricted caller searching the catalog gets its
hits projected (``project_meta_hits``) before anything sorts, limits,
reranks or caches them, each marked with the catalog's identity
(``source_repo``) so later re-checks judge the same thing, with bounded
deepening so hidden top hits do not leave the page short (the schedule of
``fulltext_pages.deepening_ks``), stopping early when the page is full,
the corpus is exhausted, or the request's search budget is spent. A
literal full-text query (and the full-text half of hybrid) is answered per
document from single-word engine queries (``fulltext_pages``).

Rows outside that choke point (multi-repository results, stored job
results, the access filter's safety net) are judged per row by their
label or by the served path of their alias (``project_catalog_rows``).
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple

from .fulltext_pages import catalog_fulltext_rows, deepening_ks, requested_size
from .hit_projection import (
    CatalogScope,
    TextRematch,
    open_catalog_scope,
    project_meta_hits,
)
from ..constants import CIDX_META_REPO_GLOBAL, is_internal_meta_repo
from .fulltext_terms import (
    FulltextOptions,
    FullTextTerms,
    effective_edit_distance,
    parse_fulltext_terms,
    raise_fulltext_refusal,
)
from .served_identity import (
    CatalogLocations,
    golden_repos_dir_of,
    path_is_catalog_location,
    request_catalog_locations,
    request_served_paths,
)
from .universe_provider import registry_of
from .visibility import (
    is_internal_meta_identity,
    meta_identity,
    unrestricted_caller,
)

logger = logging.getLogger(__name__)

# (k, mode, query vector or None, engine query text) -> raw hits.
RunRound = Callable[[int, str, Optional[List[float]], str], List[Any]]
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


class LazyCatalogScope:
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


def _field(row: Any, key: str) -> Any:
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def _labels_catalog(row: Any) -> bool:
    for key in ("repository_alias", "source_repo"):
        value = _field(row, key)
        if isinstance(value, str) and is_internal_meta_repo(value):
            return True
    return False


def _marked_catalog(row: Any) -> Any:
    """*row* carrying the catalog's identity in ``source_repo``, replacing
    any other label: rereads judge the row by it, never by re-resolving an
    alias that may since point elsewhere."""
    label = _field(row, "source_repo")
    if isinstance(label, str) and is_internal_meta_repo(label):
        return row
    if isinstance(row, dict):
        return dict(row, source_repo=CIDX_META_REPO_GLOBAL)
    return dataclasses.replace(row, source_repo=CIDX_META_REPO_GLOBAL)


def _served_catalog_root(state: Any) -> Optional[Path]:
    from code_indexer.global_repos.alias_manager import AliasManager

    golden_repos_dir = getattr(state, "golden_repos_dir", None)
    if not golden_repos_dir:
        return None
    target = AliasManager(str(Path(golden_repos_dir) / "aliases")).read_alias(
        CIDX_META_REPO_GLOBAL
    )
    return Path(target) if target else None


class _ServedCatalogRoots:
    """Per call: the catalog copy each row alias is served from (None when
    the alias serves no catalog location), read once per alias."""

    def __init__(self, username: str, state: Optional[Any]) -> None:
        self._username = username
        self._golden = (
            None if state is None else getattr(state, "golden_repos_dir", None)
        )
        self._arm = (
            None if state is None else getattr(state, "activated_repo_manager", None)
        )
        self._roots: Dict[str, Optional[Path]] = {}

    def of(self, row: Any) -> Optional[Path]:
        alias = _field(row, "repository_alias")
        if not isinstance(alias, str) or not alias or not self._golden:
            return None
        if alias not in self._roots:
            golden = str(self._golden)
            served = request_served_paths(alias, self._username, self._arm, golden)
            self._roots[alias] = next(
                (Path(p) for p in served if path_is_catalog_location(p, golden)),
                None,
            )
        return self._roots[alias]


def project_grouped_results(
    grouped: Mapping[str, List[Any]],
    username: str,
    access_service: Optional[Any],
    rematch: Optional[TextRematch] = None,
) -> Dict[str, List[Any]]:
    """Results grouped by repository alias (multi-repository search) with
    the rows of every group that serves the catalog (by name or served
    path) projected for the caller, each labelled with its group alias.
    Other groups are untouched."""
    out: Dict[str, List[Any]] = {}
    for alias, rows in grouped.items():
        labelled = [dict(row, repository_alias=alias) for row in rows]
        projected = project_catalog_rows(labelled, username, access_service, rematch)
        out[alias] = rows if projected is labelled else projected
    return out


def project_multi_response(
    response: Any,
    username: str,
    access_service: Optional[Any],
    rematch: Optional[TextRematch] = None,
) -> None:
    """A multi-repository response (``results`` grouped by alias, with
    ``metadata.total_results``) as the caller may see it, in place: rows
    projected (``project_grouped_results``) and the count taken from the
    projected rows."""
    response.results = project_grouped_results(
        response.results, username, access_service, rematch
    )
    _recount_multi(response)


def _recount_multi(response: Any) -> None:
    """A multi-repository response's ``total_results`` from its rows."""
    response.metadata.total_results = sum(len(r) for r in response.results.values())


def _multi_catalog_group(
    alias: str,
    root: Path,
    request: Any,
    terms: FullTextTerms,
    limit: int,
    scope: CatalogScope,
) -> List[Any]:
    """The caller's rows of one catalog group: one per shown document
    (``catalog_fulltext_rows``) over the copy at *root* with *request*'s
    filters, in the multi-search row shape."""
    from ...multi.fts_rows import MULTI_FTS_SNIPPET_LINES, multi_fts_filters, multi_row
    from ...query.filtered_search import extension_overfetch_deadline

    deadline = extension_overfetch_deadline(request.file_extensions)
    options = FulltextOptions(
        snippet_lines=MULTI_FTS_SNIPPET_LINES,
        filters=multi_fts_filters(request, deadline),
    )
    rows = catalog_fulltext_rows(root, options, terms, scope, limit)
    return [_marked_catalog(dict(multi_row(r), repository_alias=alias)) for r in rows]


def project_multi_fulltext(
    response: Any,
    request: Any,
    per_repo_limit: Callable[[], int],
    username: str,
    access_service: Optional[Any],
) -> None:
    """A multi-repository full-text response as the caller may see it, in
    place. For a restricted caller, the group of every requested alias that
    serves the catalog (by name or served path) and did not fail is built
    per shown document (``fulltext_page``), whatever the whole-query search
    returned for it: a document whose words sit in different chunks has no
    whole-query hit, and *per_repo_limit()* (read only then) sizes it.
    Other groups are untouched; ``total_results`` is taken from the rows.

    Raises:
        SearchParameterError: a catalog group's query form cannot be judged
            on shown text (the doors refuse it before searching).
    """
    if unrestricted_caller(username, access_service):
        return
    state = server_app_state()
    served = _ServedCatalogRoots(username, state)
    failed = response.errors or {}
    default_root = None if state is None else _served_catalog_root(state)
    registry = None if state is None else registry_of(state)
    terms = parse_fulltext_terms(request.query, 0)
    for alias in dict.fromkeys(request.repositories):
        row = {"repository_alias": alias}
        if alias in failed or not (_labels_catalog(row) or served.of(row)):
            continue
        if terms is None:
            raise_fulltext_refusal()
        root = served.of(row) or default_root
        scope = (
            open_catalog_scope(username, access_service, root, registry)
            if root is not None and registry is not None
            else None
        )
        if root is None or scope is None:
            logger.warning("Catalog copy unresolvable; catalog rows dropped")
            response.results.pop(alias, None)
            continue
        response.results[alias] = _multi_catalog_group(
            alias, root, request, terms, per_repo_limit(), scope
        )
    _recount_multi(response)


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
    """*rows* in order, every catalog row projected for the caller (and
    re-matched with *rematch* when given); other rows are untouched, and the
    same list object is returned when nothing is catalog or the caller is
    unrestricted. A row is catalog when labelled so or when its alias is
    served from a catalog location; it is projected over that copy, else
    over the copy ``cidx-meta-global`` serves. For a restricted caller,
    catalog rows are dropped when no copy, registry or scope is available."""
    if not rows or unrestricted_caller(username, access_service):
        return rows
    state = server_app_state()
    served = _ServedCatalogRoots(username, state)
    catalog = [_labels_catalog(row) or served.of(row) is not None for row in rows]
    if not any(catalog):
        return rows
    default_root = None if state is None else _served_catalog_root(state)
    registry = None if state is None else registry_of(state)
    scopes: Dict[Optional[Path], Optional[CatalogScope]] = {}
    out: List[Any] = []
    for row, is_catalog in zip(rows, catalog):
        if not is_catalog:
            out.append(row)
            continue
        root = served.of(row) or default_root
        if root not in scopes:
            scopes[root] = (
                open_catalog_scope(username, access_service, root, registry)
                if root is not None and registry is not None
                else None
            )
            if scopes[root] is None:
                logger.warning("Catalog copy unresolvable; catalog rows dropped")
        scope = scopes[root]
        if scope is not None:
            projected = project_meta_hits([row], scope, rematch)
            out.extend(_marked_catalog(hit) for hit in projected)
    return out


def rows_include_catalog(rows: List[Any], username: str) -> bool:
    """True when any row is the catalog: labelled so, or served from a
    catalog location under its alias."""
    served = _ServedCatalogRoots(username, server_app_state())
    return any(_labels_catalog(row) or served.of(row) is not None for row in rows)


class FullTextQuery(Protocol):
    """The full-text fields of a query request."""

    query_text: str
    case_sensitive: bool
    regex: bool
    fuzzy: bool
    edit_distance: int
    limit: int
    snippet_lines: int


def rest_rows_for_caller(
    access_service: Optional[Any],
    username: str,
    rows: List[Dict[str, Any]],
    fts_query: Optional[FullTextQuery] = None,
) -> List[Dict[str, Any]]:
    """REST query rows as the caller may see them: catalog rows projected
    (re-matched against *fts_query* for full-text rows), then the access
    filter. Without the access service, rows of ordinary repositories (whose
    access was decided before the search) are returned unchanged.

    Raises:
        AccessFilteringServiceUnavailableError: a row is the catalog and the
            access service is not wired, so its view cannot be decided.
    """
    from ..repo_access_guard import AccessFilteringServiceUnavailableError

    if access_service is None:
        if rows_include_catalog(rows, username):
            raise AccessFilteringServiceUnavailableError(
                "access filtering service unavailable for catalog results"
            )
        return rows
    rematch = (
        None
        if fts_query is None
        else TextRematch(
            fts_query.query_text,
            fts_query.regex,
            effective_edit_distance(fts_query.fuzzy, fts_query.edit_distance),
            FulltextOptions(snippet_lines=fts_query.snippet_lines),
        )
    )
    projected = project_catalog_rows(rows, username, access_service, rematch)
    return list(access_service.filter_query_results(projected, username))


def _catalog_fulltext_page(
    run: RunRound,
    repo_path: Any,
    alias: str,
    rematch: TextRematch,
    scope: LazyCatalogScope,
    limit: int,
) -> List[Any]:
    """A literal query's catalog page: one row per shown document, chosen
    and ordered from shown text (``catalog_fulltext_rows``). A copy without
    a full-text index goes through the engine path first, which raises its
    own missing-index error before any projection is set up."""
    from ...query.semantic_query_manager import fts_query_results

    terms = rematch.terms
    if terms is None:
        raise_fulltext_refusal()
    root = Path(repo_path)
    if not (root / ".code-indexer" / "tantivy_index").exists():
        run(limit, "fts", None, rematch.query)
    rows = catalog_fulltext_rows(
        root, rematch.options, terms, scope.get(), limit, rematch.edit_distance
    )
    return fts_query_results(rows, alias, rematch.options.min_score)


def _fulltext_rows(
    run: RunRound,
    k: int,
    scope: LazyCatalogScope,
    rematch: TextRematch,
    vector: Optional[List[float]],
    fts_page: Optional[List[Any]],
) -> Tuple[List[Any], bool]:
    """One round's full-text rows and whether they are complete: the
    literal page computed up front, else a regex query's hits projected
    one by one."""
    if fts_page is not None:
        return fts_page, True
    raw = run(k, "fts", vector, rematch.query)
    return project_meta_hits(raw, scope.get(), rematch), len(raw) < k


def _round(
    run: RunRound,
    k: int,
    mode: str,
    scope: LazyCatalogScope,
    rematch: TextRematch,
    merge: Merge,
    vector: Optional[List[float]],
    fts_page: Optional[List[Any]],
) -> Tuple[List[Any], bool]:
    """One round's projected page and whether the corpus is exhausted."""
    if mode == "fts":
        return _fulltext_rows(run, k, scope, rematch, vector, fts_page)
    if mode == "hybrid":
        fts_rows, fts_done = _fulltext_rows(run, k, scope, rematch, vector, fts_page)
        semantic_raw = run(k, "semantic", vector, rematch.query)
        page = merge(fts_rows, project_meta_hits(semantic_raw, scope.get()), k)
        return page, fts_done and len(semantic_raw) < k
    raw = run(k, mode, vector, rematch.query)
    return project_meta_hits(raw, scope.get()), len(raw) < k


def catalog_query_vector(
    repo_path: Any, query_text: str, no_embedding_cache_shortcut: bool
) -> List[float]:
    """The query's embedding from *repo_path*'s own provider, through the
    server's query-embedding entry point (one provider call at most)."""
    from .. import search_service
    from ..governed_call import coalesced_query_embedding

    config = search_service._load_repo_config(str(repo_path))
    provider = search_service.EmbeddingProviderFactory.create(
        config=config, http_client_factory=search_service._get_http_client_factory()
    )
    vector, _meta = coalesced_query_embedding(
        provider, query_text, no_embedding_cache_shortcut=no_embedding_cache_shortcut
    )
    return list(vector)


def catalog_rematch(
    query_text: str,
    request_kwargs: Mapping[str, Any],
    activated_repo_manager: Optional[Any],
) -> Tuple[TextRematch, Optional[CatalogLocations]]:
    """One search request's full-text query (as every repository of it
    runs it) and its catalog locations, looked up at most once."""
    from ...query.filtered_search import fts_filter_kwargs

    distance = effective_edit_distance(
        bool(request_kwargs.get("fuzzy")), int(request_kwargs.get("edit_distance") or 0)
    )
    snippet_lines = request_kwargs.get("snippet_lines")
    options = FulltextOptions(
        case_sensitive=bool(request_kwargs.get("case_sensitive")),
        snippet_lines=(
            FulltextOptions().snippet_lines
            if snippet_lines is None
            else int(snippet_lines)
        ),
        min_score=request_kwargs.get("min_score"),
        filters=fts_filter_kwargs(
            language=request_kwargs.get("language"),
            path_filter=request_kwargs.get("path_filter"),
            exclude_language=request_kwargs.get("exclude_language"),
            exclude_path=request_kwargs.get("exclude_path"),
            file_extensions=request_kwargs.get("file_extensions"),
            deadline=request_kwargs.get("extension_deadline"),
        ),
    )
    rematch = TextRematch(
        query_text, bool(request_kwargs.get("regex")), distance, options
    )
    golden_repos_dir = golden_repos_dir_of(activated_repo_manager)
    locations = (
        request_catalog_locations(golden_repos_dir) if golden_repos_dir else None
    )
    return rematch, locations


def _catalog_judgement(
    repo_info: Mapping[str, Any],
    repo_path: Any,
    golden_repos_dir: Optional[str],
    locations: Optional[CatalogLocations] = None,
) -> Optional[bool]:
    """True when the entry is proven to be the catalog (its golden identity,
    or the directory searched is a catalog location); None when only an
    unresolved identity makes it count as the catalog; False otherwise."""
    identities = meta_identity(repo_info)
    if any(is_internal_meta_repo(identity) for identity in identities):
        return True
    if (
        repo_path
        and golden_repos_dir
        and path_is_catalog_location(str(repo_path), str(golden_repos_dir), locations)
    ):
        return True
    return None if is_internal_meta_identity(identities) else False


def search_for_caller(
    run: RunRound,
    *,
    username: str,
    repo_info: Mapping[str, Any],
    repo_path: Any,
    activated_repo_manager: Optional[Any],
    limit: int,
    search_mode: str,
    rematch: TextRematch,
    merge: Merge,
    budget_spent: Callable[[], bool],
    query_vector: Optional[List[float]] = None,
    no_embedding_cache_shortcut: bool = False,
    locations: Optional[CatalogLocations] = None,
) -> List[Any]:
    """One repository's hits for the caller (see the module docstring).
    *run(k, mode, vector, text)* is the raw single-repository search of
    *text* (the query, or one of its words) over *repo_path* (*vector*:
    the query embedding to reuse, or None); the golden repositories beside
    *activated_repo_manager* hold the alias pointers. Every deepening round
    reuses one query embedding: *query_vector*, else one computed before
    the first round."""
    query = rematch.query
    state = server_app_state()
    if state is None:
        if is_internal_meta_identity(meta_identity(repo_info)):
            raise CatalogProjectionUnavailable("catalog search needs the server state")
        return run(limit, search_mode, query_vector, query)
    access_service = getattr(state, "access_filtering_service", None)
    if unrestricted_caller(username, access_service):
        return run(limit, search_mode, query_vector, query)
    golden_repos_dir = golden_repos_dir_of(activated_repo_manager)
    proven = _catalog_judgement(repo_info, repo_path, golden_repos_dir, locations)
    if proven is False:
        return run(limit, search_mode, query_vector, query)
    fulltext = search_mode in ("fts", "hybrid") and not rematch.regex
    if fulltext and rematch.terms is None:
        raise_fulltext_refusal()
    scope = LazyCatalogScope(
        lambda: open_catalog_scope(
            username, access_service, Path(repo_path), registry_of(state)
        )
    )
    fts_page = (
        _catalog_fulltext_page(
            run,
            repo_path,
            str(repo_info.get("user_alias") or ""),
            rematch,
            scope,
            limit,
        )
        if fulltext
        else None
    )
    if fts_page is not None and search_mode == "fts":
        return [_marked_catalog(row) for row in fts_page]
    vector = query_vector
    embeds = search_mode in ("semantic", "hybrid")
    requested = requested_size(limit)
    page: List[Any] = []
    schedule = deepening_ks(limit)
    for round_index, k in enumerate(schedule):
        # An unresolved identity embeds only to deepen, never up front.
        if vector is None and embeds and (proven or round_index > 0):
            vector = catalog_query_vector(repo_path, query, no_embedding_cache_shortcut)
        logger.debug("catalog round %d k=%d", round_index, k)
        page, exhausted = _round(
            run, k, search_mode, scope, rematch, merge, vector, fts_page
        )
        if len(page) >= requested or exhausted or k >= schedule[-1] or budget_spent():
            break
    return [_marked_catalog(row) for row in page[:requested]]
