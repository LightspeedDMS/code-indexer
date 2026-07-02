"""
Dependency-map MCP handlers — Story #855.

Provides depmap_find_consumers_handler and _register() for wiring into
the HANDLER_REGISTRY via _legacy.py.

dep_map_path is resolved fresh on every call via
app.state.dependency_map_service.cidx_meta_read_path — never cached.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, cast

from code_indexer.server.services.dep_map_parser_hygiene import (
    AnomalyAggregate,
    AnomalyEntry,
)

from code_indexer.server.services.dep_map_parser_graph import _aggregate_graph

from . import _utils
from ._utils import _mcp_response
from ._depmap_aliases import (
    apply_consumer_aliases,
    apply_domain_membership_aliases,
    assert_resolution_valid,
    assert_success_resolution_consistent,
)

logger = logging.getLogger(__name__)


def _anomaly_to_dict(anomaly: "AnomalyEntry | AnomalyAggregate") -> Dict[str, str]:
    """Convert an AnomalyEntry or AnomalyAggregate to a JSON-serializable dict.

    AnomalyEntry    → {"file": entry.file, "error": entry.message}
    AnomalyAggregate → {"file": "<aggregated>",
                        "error": "<N> occurrences: <type>"}
    """
    if isinstance(anomaly, AnomalyAggregate):
        return {
            "file": "<aggregated>",
            "error": f"{anomaly.count} occurrences: {anomaly.type.value}",
        }
    return {"file": anomaly.file, "error": anomaly.message}


def _is_repo_indexed(output_dir: Path, repo_name: str) -> bool:
    """Return True if repo_name appears in any domain's participating_repos.

    output_dir is dep_map_path/"dependency-map" — the same path used internally
    by DepMapMCPParser.  This function is called only when output_dir exists.
    """
    from code_indexer.server.services.dep_map_file_utils import load_domains_json

    domains = load_domains_json(output_dir)
    return any(
        isinstance(d, dict) and repo_name in (d.get("participating_repos") or [])
        for d in domains
    )


def depmap_find_consumers_handler(params: Dict[str, Any], user: Any) -> Dict[str, Any]:
    """
    MCP handler for depmap_find_consumers.

    Reads dep_map_path fresh from app.state on every invocation so that
    path changes from cidx-meta refreshes are always picked up.

    Args:
        params: Tool arguments. Expected key: ``repo_name`` (str).
        user: Authenticated user (unused for path resolution, kept for
              handler signature compatibility).

    Returns:
        MCP-compliant response dict. Every response includes both ``success``
        and ``resolution`` fields (AC1/AC6).

        resolution values (AC7):
        - ``invalid_input``:         empty repo_name (BREAKING CHANGE — was success=true)
        - ``repo_not_indexed``:      dep_map_path missing or repo absent from all domains
        - ``repo_has_no_consumers``: repo indexed but no consumers depend on it
        - ``ok``:                    consumers found; canonical+alias dual-write applied
    """
    repo_name = params.get("repo_name", "") if isinstance(params, dict) else ""
    if not isinstance(repo_name, str):
        repo_name = ""

    # AC2 BREAKING CHANGE: empty input → invalid_input (was success=true, [])
    if not repo_name:
        success, resolution = False, "invalid_input"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {
                "success": success,
                "resolution": resolution,
                "error": "repo_name must not be empty",
                "consumers": [],
                "anomalies": [],
            }
        )

    # Resolve dep_map_path fresh — NEVER cached
    dep_map_path: Path = (
        _utils.app_module.app.state.dependency_map_service.cidx_meta_read_path
    )

    if not dep_map_path.exists():
        logger.warning(
            "depmap_find_consumers: dep_map_path not found: %s", dep_map_path
        )
        success, resolution = False, "repo_not_indexed"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {
                "success": success,
                "resolution": resolution,
                "error": "dep_map_path not found",
                "consumers": [],
                "anomalies": [],
            }
        )

    from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser

    parser = DepMapMCPParser(dep_map_path)
    consumers, anomalies = parser.find_consumers(repo_name)

    if consumers:
        # AC3/AC5: canonical 'repo' + deprecated 'consuming_repo' alias dual-write
        enriched = [apply_consumer_aliases(c) for c in consumers]
        success, resolution = True, "ok"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {
                "success": success,
                "resolution": resolution,
                "consumers": enriched,
                "anomalies": anomalies,
            }
        )

    # No consumers found — determine whether repo is indexed at all (AC7)
    # output_dir is the same "dependency-map" sub-path DepMapMCPParser uses internally
    output_dir = dep_map_path / "dependency-map"
    if not output_dir.exists():
        logger.warning(
            "depmap_find_consumers: dependency-map dir not found: %s", output_dir
        )
        success, resolution = False, "repo_not_indexed"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {
                "success": success,
                "resolution": resolution,
                "error": "dependency-map directory not found",
                "consumers": [],
                "anomalies": anomalies,
            }
        )

    resolution = (
        "repo_has_no_consumers"
        if _is_repo_indexed(output_dir, repo_name)
        else "repo_not_indexed"
    )
    success = False
    assert_resolution_valid(resolution)
    assert_success_resolution_consistent(success, resolution)
    return _mcp_response(
        {
            "success": success,
            "resolution": resolution,
            "consumers": [],
            "anomalies": anomalies,
        }
    )


def _resolve_parser(tool_name: str):
    """Resolve a fresh dep_map_path and construct a DepMapMCPParser.

    Reads dep_map_path from app.state on every call — never cached.

    Args:
        tool_name: Caller name used in the warning log when path is missing.

    Returns:
        (DepMapMCPParser, None) when dep_map_path exists.
        (None, error_response_dict) when dep_map_path does not exist.
    """
    from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser

    dep_map_path: Path = (
        _utils.app_module.app.state.dependency_map_service.cidx_meta_read_path
    )
    if not dep_map_path.exists():
        logger.warning("%s: dep_map_path not found: %s", tool_name, dep_map_path)
        return None, {"success": False, "error": "dep_map_path not found"}
    return DepMapMCPParser(dep_map_path), None


def _str_param(params: Dict[str, Any], key: str) -> str:
    """Extract a string parameter from params, defaulting to empty string."""
    if not isinstance(params, dict):
        return ""
    value = params.get(key, "")
    return value if isinstance(value, str) else ""


def depmap_get_repo_domains_handler(
    params: Dict[str, Any], user: Any
) -> Dict[str, Any]:
    """
    MCP handler for depmap_get_repo_domains.

    Returns all domains that the given repo participates in, with its role in each.
    dep_map_path is resolved fresh on every call via app.state.

    Args:
        params: Tool arguments. Expected key: ``repo_name`` (str).
        user: Authenticated user (unused, kept for handler signature compatibility).

    Returns:
        MCP-compliant response dict. Every response includes both ``success``
        and ``resolution`` fields (AC1/AC6).

        resolution values (AC8):
        - ``invalid_input``:    empty repo_name
        - ``repo_not_indexed``: dep_map_path missing or repo absent from all domains
        - ``ok``:               domains found; canonical+alias dual-write applied
    """
    repo_name = _str_param(params, "repo_name")
    if not repo_name:
        success, resolution = False, "invalid_input"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {
                "success": success,
                "resolution": resolution,
                "error": "repo_name must not be empty",
                "domains": [],
                "anomalies": [],
            }
        )

    parser, err = _resolve_parser("depmap_get_repo_domains")
    if err is not None:
        success, resolution = False, "repo_not_indexed"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {**err, "resolution": resolution, "domains": [], "anomalies": []}
        )

    domains, anomalies = parser.get_repo_domains(repo_name)

    if not domains:
        success, resolution = False, "repo_not_indexed"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {
                "success": success,
                "resolution": resolution,
                "domains": [],
                "anomalies": anomalies,
            }
        )

    # AC3/AC5: canonical 'domain' + deprecated 'domain_name' alias dual-write
    enriched = [apply_domain_membership_aliases(d) for d in domains]
    success, resolution = True, "ok"
    assert_resolution_valid(resolution)
    assert_success_resolution_consistent(success, resolution)
    return _mcp_response(
        {
            "success": success,
            "resolution": resolution,
            "domains": enriched,
            "anomalies": anomalies,
        }
    )


def depmap_get_domain_summary_handler(
    params: Dict[str, Any], user: Any
) -> Dict[str, Any]:
    """
    MCP handler for depmap_get_domain_summary.

    Returns structured summary for a named domain.
    dep_map_path is resolved fresh on every call via app.state.

    Args:
        params: Tool arguments. Expected key: ``domain_name`` (str).
        user: Authenticated user (unused, kept for handler signature compatibility).

    Returns:
        MCP-compliant response dict. Every response includes both ``success``
        and ``resolution`` fields (AC1/AC6).

        resolution values (AC8):
        - ``invalid_input``:      empty domain_name
        - ``domain_not_indexed``: dep_map_path missing or domain not in _domains.json
        - ``ok``:                 summary found
    """
    domain_name = _str_param(params, "domain_name")
    if not domain_name:
        success, resolution = False, "invalid_input"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {
                "success": success,
                "resolution": resolution,
                "error": "domain_name must not be empty",
                "summary": None,
                "anomalies": [],
            }
        )

    parser, err = _resolve_parser("depmap_get_domain_summary")
    if err is not None:
        success, resolution = False, "domain_not_indexed"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {**err, "resolution": resolution, "summary": None, "anomalies": []}
        )

    summary, anomalies = parser.get_domain_summary(domain_name)
    if summary is None:
        success, resolution = False, "domain_not_indexed"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {
                "success": success,
                "resolution": resolution,
                "summary": None,
                "anomalies": anomalies,
            }
        )
    success, resolution = True, "ok"
    assert_resolution_valid(resolution)
    assert_success_resolution_consistent(success, resolution)
    return _mcp_response(
        {
            "success": success,
            "resolution": resolution,
            "summary": summary,
            "anomalies": anomalies,
        }
    )


def depmap_get_stale_domains_handler(
    params: Dict[str, Any], user: Any
) -> Dict[str, Any]:
    """
    MCP handler for depmap_get_stale_domains.

    Returns domains whose last_analyzed frontmatter field is older than
    days_threshold days. dep_map_path is resolved fresh on every call via app.state.

    Args:
        params: Tool arguments. Expected key: ``days_threshold`` (non-negative int).
        user: Authenticated user (unused, kept for handler signature compatibility).

    Returns:
        MCP-compliant response dict:
        - success=true:  {"success": true, "stale_domains": [...], "anomalies": [...]}
        - success=false: {"success": false, "error": "...", "stale_domains": [], "anomalies": []}
    """
    raw = params.get("days_threshold") if isinstance(params, dict) else None
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        success, resolution = False, "invalid_input"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {
                "success": success,
                "resolution": resolution,
                "error": "days_threshold must be a non-negative integer",
                "stale_domains": [],
                "anomalies": [],
            }
        )

    parser, err = _resolve_parser("depmap_get_stale_domains")
    if err is not None:
        success, resolution = False, "invalid_input"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {**err, "resolution": resolution, "stale_domains": [], "anomalies": []}
        )

    stale_domains, anomalies = parser.get_stale_domains(raw)
    success, resolution = True, "ok"
    assert_resolution_valid(resolution)
    assert_success_resolution_consistent(success, resolution)
    return _mcp_response(
        {
            "success": success,
            "resolution": resolution,
            "stale_domains": stale_domains,
            "anomalies": anomalies,
        }
    )


def _apply_graph_filters(
    edges: List[Dict[str, Any]],
    source_domain: Optional[Union[str, List[str]]] = None,
    target_domain: Optional[Union[str, List[str]]] = None,
    min_count: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Apply optional AND-composition filters to the edges list.

    Filters applied only when provided (not None):
    - source_domain (str | list[str]): edge source_domain must be in the set
    - target_domain (str | list[str]): edge target_domain must be in the set
    - min_count (int):                 edge dependency_count >= min_count

    Returns a new list; the original edges list is not mutated.

    Invariant (MESSI rule 15): every returned edge satisfies all filters.
    Stripped under python -O.
    """
    # Normalise str → frozenset for O(1) membership check
    src_set: Optional[frozenset] = None
    if source_domain is not None:
        src_set = (
            frozenset([source_domain])
            if isinstance(source_domain, str)
            else frozenset(source_domain)
        )

    tgt_set: Optional[frozenset] = None
    if target_domain is not None:
        tgt_set = (
            frozenset([target_domain])
            if isinstance(target_domain, str)
            else frozenset(target_domain)
        )

    result: List[Dict[str, Any]] = []
    for edge in edges:
        if src_set is not None and edge["source_domain"] not in src_set:
            continue
        if tgt_set is not None and edge["target_domain"] not in tgt_set:
            continue
        if min_count is not None and edge["dependency_count"] < min_count:
            continue
        result.append(edge)

    assert all(
        (src_set is None or e["source_domain"] in src_set)
        and (tgt_set is None or e["target_domain"] in tgt_set)
        and (min_count is None or e["dependency_count"] >= min_count)
        for e in result
    ), "Invariant: every returned edge must satisfy all provided filters"
    return result


def depmap_get_cross_domain_graph_handler(
    params: Dict[str, Any], user: Any
) -> Dict[str, Any]:
    """
    MCP handler for depmap_get_cross_domain_graph.

    Returns the full directed domain-to-domain edge graph across all domains.
    Takes no required arguments; unknown arguments are ignored gracefully.
    dep_map_path is resolved fresh on every call via app.state.

    Returns:
        MCP-compliant response dict:
        - success=true:  {"success": true, "edges": [...], "anomalies": [...],
                          "parser_anomalies": [...], "data_anomalies": [...]}
        - success=false: {"success": false, "error": "...", "edges": [],
                          "anomalies": [], "parser_anomalies": [], "data_anomalies": []}
    """
    parser, err = _resolve_parser("depmap_get_cross_domain_graph")
    if err is not None:
        success, resolution = False, "invalid_input"
        assert_resolution_valid(resolution)
        assert_success_resolution_consistent(success, resolution)
        return _mcp_response(
            {
                **err,
                "resolution": resolution,
                "edges": [],
                "anomalies": [],
                "parser_anomalies": [],
                "data_anomalies": [],
            }
        )

    # AC1/AC2 (Story #889): optional filter params — all default to None (omitted = no filter)
    raw_source = params.get("source_domain") if isinstance(params, dict) else None
    raw_target = params.get("target_domain") if isinstance(params, dict) else None
    raw_min_count = params.get("min_count") if isinstance(params, dict) else None

    # Strict validation: domain filters accept only None, str, or list[str].
    # Rejects dict, tuple, set, bool, int, and lists containing non-str elements.
    def _validate_domain_filter(
        value: Any, field: str
    ) -> "tuple[Optional[Union[str, List[str]]], Optional[str]]":
        if value is None:
            return None, None
        if isinstance(value, str):
            return value, None
        if isinstance(value, list) and all(isinstance(el, str) for el in value):
            return value, None
        return None, (
            f"'{field}' must be a string or list of strings, got {type(value).__name__}"
        )

    # Strict validation: min_count accepts only None or plain int >= 1.
    # Rejects bool (bool is int subclass), float, str, and values < 1.
    def _validate_min_count(
        value: Any,
    ) -> "tuple[Optional[int], Optional[str]]":
        if value is None:
            return None, None
        if type(value) is not int:
            return None, (
                f"'min_count' must be a positive integer, got {type(value).__name__}"
            )
        if value < 1:
            return None, (f"'min_count' must be >= 1 (schema minimum), got {value}")
        return value, None

    def _invalid_graph_response(error_msg: str) -> Dict[str, Any]:
        _success, _resolution = False, "invalid_input"
        assert_resolution_valid(_resolution)
        assert_success_resolution_consistent(_success, _resolution)
        return _mcp_response(
            {
                "success": _success,
                "resolution": _resolution,
                "error": error_msg,
                "edges": [],
                "anomalies": [],
                "parser_anomalies": [],
                "data_anomalies": [],
            }
        )

    source_filter, src_err = _validate_domain_filter(raw_source, "source_domain")
    if src_err is not None:
        return _invalid_graph_response(src_err)

    target_filter, tgt_err = _validate_domain_filter(raw_target, "target_domain")
    if tgt_err is not None:
        return _invalid_graph_response(tgt_err)

    min_count_filter, mc_err = _validate_min_count(raw_min_count)
    if mc_err is not None:
        return _invalid_graph_response(mc_err)

    edges, all_anomalies, parser_anomalies, data_anomalies = (
        parser.get_cross_domain_graph_with_channels()
    )

    filtered_edges = _apply_graph_filters(
        edges,
        source_domain=source_filter,
        target_domain=target_filter,
        min_count=min_count_filter,
    )

    success, resolution = True, "ok"
    assert_resolution_valid(resolution)
    assert_success_resolution_consistent(success, resolution)
    return _mcp_response(
        {
            "success": success,
            "resolution": resolution,
            "edges": filtered_edges,
            "anomalies": [_anomaly_to_dict(a) for a in all_anomalies],
            "parser_anomalies": [_anomaly_to_dict(a) for a in parser_anomalies],
            "data_anomalies": [_anomaly_to_dict(a) for a in data_anomalies],
        }
    )


_VALID_BY_VALUES = frozenset({"out_degree", "in_degree", "total_degree"})

_BY_TO_METRIC_KEY = {
    "out_degree": "out_degree",
    "in_degree": "in_degree",
    "total_degree": "total",
}


def _compute_hub_domains(
    output_dir: Path,
    top_n: int = 5,
    by: str = "total_degree",
) -> List[Dict[str, Any]]:
    """Compute hub domain rankings from edge_data returned by _aggregate_graph.

    AC4 (Story #889): reuses _aggregate_graph — no duplicate parsing logic.
    AC7 (Story #889): on-the-fly computation on every call — no cache.

    Args:
        output_dir: Path to the dependency-map directory.
        top_n:      Maximum number of entries to return; must be a positive int.
        by:         Ranking metric — must be one of _VALID_BY_VALUES.

    Raises:
        ValueError: if by is not in _VALID_BY_VALUES or top_n is invalid.

    Returns:
        List of {domain, in_degree, out_degree, total} dicts sorted descending by `by`,
        truncated to top_n. Returns [] when output_dir is absent or graph is empty.
    """
    if by not in _VALID_BY_VALUES:
        raise ValueError(f"by must be one of {sorted(_VALID_BY_VALUES)}, got {by!r}")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
        raise ValueError(f"top_n must be a positive integer, got {top_n!r}")

    edge_data, _ = _aggregate_graph(output_dir)
    if not edge_data:
        return []

    degrees: Dict[str, Dict[str, int]] = {}

    def _ensure(domain: str) -> None:
        if domain not in degrees:
            degrees[domain] = {"in_degree": 0, "out_degree": 0}

    for src, tgt in edge_data:
        _ensure(src)
        _ensure(tgt)
        degrees[src]["out_degree"] += 1
        degrees[tgt]["in_degree"] += 1

    metric_key = _BY_TO_METRIC_KEY[by]
    hubs = [
        {
            "domain": domain,
            "in_degree": v["in_degree"],
            "out_degree": v["out_degree"],
            "total": v["in_degree"] + v["out_degree"],
        }
        for domain, v in degrees.items()
    ]
    # cast: h[metric_key] is always int (in_degree/out_degree/total set above);
    # mypy types h as dict[str, object] so cast is needed — runtime no-op.
    hubs.sort(key=lambda h: int(cast(int, h[metric_key])), reverse=True)
    return hubs[:top_n]


DEFAULT_HUB_DOMAINS_LIMIT = 5


def _hub_invalid_input_response(error_msg: str) -> Dict[str, Any]:
    """Build a standard invalid_input MCP response for hub_domains validation errors."""
    success, resolution = False, "invalid_input"
    assert_resolution_valid(resolution)
    assert_success_resolution_consistent(success, resolution)
    return _mcp_response(
        {"success": success, "resolution": resolution, "error": error_msg, "hubs": []}
    )


def depmap_get_hub_domains_handler(params: Dict[str, Any], user: Any) -> Dict[str, Any]:
    """
    MCP handler for depmap_get_hub_domains.

    Returns top-N domains ranked by edge degree. Computes on every call (AC7, Story #889).

    Args:
        params: Tool arguments.
            top_n (int, default DEFAULT_HUB_DOMAINS_LIMIT): positive int; absent uses default,
                  present-but-invalid returns invalid_input.
            by (str, default "total_degree"): one of _VALID_BY_VALUES.
        user: Authenticated user (unused, kept for signature compatibility).

    Returns:
        MCP-compliant response dict.
        resolution values (AC5):
        - invalid_input: unknown by=, invalid/negative top_n, or dep_map_path not a valid Path
        - ok:            hubs computed (may be empty list)
    """
    is_dict = isinstance(params, dict)
    raw_by = params.get("by", "total_degree") if is_dict else "total_degree"

    if not isinstance(raw_by, str) or raw_by not in _VALID_BY_VALUES:
        return _hub_invalid_input_response(
            f"by must be one of {sorted(_VALID_BY_VALUES)}, got {raw_by!r}"
        )

    raw_top_n = params.get("top_n") if is_dict else None
    if raw_top_n is None:
        top_n = DEFAULT_HUB_DOMAINS_LIMIT
    elif not isinstance(raw_top_n, int) or isinstance(raw_top_n, bool) or raw_top_n < 1:
        return _hub_invalid_input_response(
            f"top_n must be a positive integer, got {raw_top_n!r}"
        )
    else:
        top_n = raw_top_n

    dep_map_path = (
        _utils.app_module.app.state.dependency_map_service.cidx_meta_read_path
    )
    if not isinstance(dep_map_path, Path) or not dep_map_path.exists():
        logger.warning(
            "depmap_get_hub_domains: dep_map_path missing or invalid: %s", dep_map_path
        )
        return _hub_invalid_input_response("dep_map_path not found")

    output_dir = dep_map_path / "dependency-map"
    hubs = _compute_hub_domains(output_dir, top_n=top_n, by=raw_by)
    success, resolution = True, "ok"
    assert_resolution_valid(resolution)
    assert_success_resolution_consistent(success, resolution)
    return _mcp_response({"success": success, "resolution": resolution, "hubs": hubs})


def _register(registry: Dict[str, Any]) -> None:
    """Register depmap handlers in the HANDLER_REGISTRY."""
    registry["depmap_find_consumers"] = depmap_find_consumers_handler
    registry["depmap_get_repo_domains"] = depmap_get_repo_domains_handler
    registry["depmap_get_domain_summary"] = depmap_get_domain_summary_handler
    registry["depmap_get_stale_domains"] = depmap_get_stale_domains_handler
    registry["depmap_get_cross_domain_graph"] = depmap_get_cross_domain_graph_handler
    registry["depmap_get_hub_domains"] = depmap_get_hub_domains_handler
