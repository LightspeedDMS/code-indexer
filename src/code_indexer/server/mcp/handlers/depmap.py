"""
Dependency-map MCP handlers — Story #855.

Provides depmap_find_consumers_handler and _register() for wiring into
the HANDLER_REGISTRY via _legacy.py.

dep_map_path is resolved fresh on every call via
app.state.dependency_map_service.cidx_meta_read_path — never cached.
"""

import logging
from pathlib import Path
from typing import Any, Dict

from code_indexer.server.services.dep_map_parser_hygiene import (
    AnomalyAggregate,
    AnomalyEntry,
)

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
        "repo_has_no_consumers" if _is_repo_indexed(output_dir, repo_name)
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
        {"success": success, "resolution": resolution, "domains": enriched, "anomalies": anomalies}
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
        {"success": success, "resolution": resolution, "summary": summary, "anomalies": anomalies}
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

    edges, all_anomalies, parser_anomalies, data_anomalies = (
        parser.get_cross_domain_graph_with_channels()
    )
    success, resolution = True, "ok"
    assert_resolution_valid(resolution)
    assert_success_resolution_consistent(success, resolution)
    return _mcp_response(
        {
            "success": success,
            "resolution": resolution,
            "edges": edges,
            "anomalies": [_anomaly_to_dict(a) for a in all_anomalies],
            "parser_anomalies": [_anomaly_to_dict(a) for a in parser_anomalies],
            "data_anomalies": [_anomaly_to_dict(a) for a in data_anomalies],
        }
    )


def _register(registry: Dict[str, Any]) -> None:
    """Register depmap handlers in the HANDLER_REGISTRY."""
    registry["depmap_find_consumers"] = depmap_find_consumers_handler
    registry["depmap_get_repo_domains"] = depmap_get_repo_domains_handler
    registry["depmap_get_domain_summary"] = depmap_get_domain_summary_handler
    registry["depmap_get_stale_domains"] = depmap_get_stale_domains_handler
    registry["depmap_get_cross_domain_graph"] = depmap_get_cross_domain_graph_handler
