"""
dep_map_parser_graph -- cross-domain graph building logic (Story #887 AC8).

Extracted from dep_map_mcp_parser.py. Handles outgoing/incoming edge collection,
bidirectional consistency checking (frozenset-keyed dedup fix for AC6), and
final graph edge assembly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple, Union

from code_indexer.server.services.dep_map_parser_hygiene import (
    AnomalyAggregate,
    AnomalyEntry,
    AnomalyType,
    aggregate_anomalies,
    deduplicate_anomalies,
    is_prose_fragment,
    normalize_identifier,
)
from code_indexer.server.services.dep_map_parser_tables import (
    iter_table_rows,
    parse_frontmatter_strict,
)

logger = logging.getLogger(__name__)

# Column indices (mirrors dep_map_parser_tables constants; re-declared locally
# to avoid cross-module constant coupling for these graph-specific usages).
_COL_OUTGOING_TARGET_DOMAIN = 2
_COL_DEP_TYPE = 3
_OUTGOING_MIN_COLS = 4
_OUTGOING_HEADER_SENTINEL = "This Repo"

_COL_SOURCE_DOMAIN = 2
_INCOMING_MIN_COLS = 6
_INCOMING_HEADER_SENTINEL = "External Repo"


def collect_outgoing_edges(
    content: str,
    domain_name: str,
    md_file: Path,
    edge_data: Dict[Tuple[str, str], Dict[str, Any]],
    anomalies: List[AnomalyEntry],
) -> None:
    """Aggregate outgoing dependency rows from *content* into *edge_data*.

    AC2: prose-fragment target_domain values are rejected with a
    GARBAGE_DOMAIN_REJECTED data-channel anomaly and not added to edge_data.

    Each row in the '### Outgoing Dependencies' table contributes to the
    (domain_name, target_domain) edge bucket. Anomalies are appended to
    *anomalies* on parse failure.
    """
    try:
        for cells in iter_table_rows(
            content,
            "### Outgoing Dependencies",
            _OUTGOING_MIN_COLS,
            _OUTGOING_HEADER_SENTINEL,
        ):
            target_domain = cells[_COL_OUTGOING_TARGET_DOMAIN]
            if not target_domain:
                continue
            if is_prose_fragment(target_domain):
                anomalies.append(
                    AnomalyEntry(
                        type=AnomalyType.GARBAGE_DOMAIN_REJECTED,
                        file=str(md_file),
                        message=f"prose-fragment target domain rejected: {target_domain!r}",
                        channel="data",
                    )
                )
                continue
            dep_type = (
                cells[_COL_DEP_TYPE].strip() if len(cells) > _COL_DEP_TYPE else ""
            )
            key = (domain_name, target_domain)
            if key not in edge_data:
                edge_data[key] = {"count": 0, "types": set()}
            edge_data[key]["count"] += 1
            if dep_type:
                edge_data[key]["types"].add(dep_type)
    except Exception as exc:
        logger.warning(
            "collect_outgoing_edges: failed to parse outgoing section in %s",
            md_file,
            exc_info=True,
        )
        anomalies.append(
            AnomalyEntry(
                type=AnomalyType.MALFORMED_YAML,
                file=str(md_file),
                message=f"outgoing section: {exc}",
                channel="parser",
            )
        )


def collect_incoming_claims(
    content: str,
    domain_name: str,
    md_file: Path,
    incoming_claims: Set[frozenset],
    anomalies: List[AnomalyEntry],
) -> None:
    """Extract incoming dependency claims from *content* for *domain_name*.

    Each row contributes a frozenset({source_domain, domain_name}) claim used
    by the bidirectional consistency check. The frozenset key ensures that
    A->B and B->A are treated as the same pair (AC6 dedup fix).
    """
    try:
        for cells in iter_table_rows(
            content,
            "### Incoming Dependencies",
            _INCOMING_MIN_COLS,
            _INCOMING_HEADER_SENTINEL,
        ):
            source_domain = cells[_COL_SOURCE_DOMAIN]
            if source_domain:
                # AC1/AC3: normalize BOTH source_domain and domain_name before frozenset
                # insertion so that backtick-wrapped or mixed-case values on either side
                # match the normalized edge keys produced by apply_edge_hygiene.
                norm_source, _ = normalize_identifier(source_domain)
                norm_target, _ = normalize_identifier(domain_name)
                incoming_claims.add(frozenset({norm_source, norm_target}))
    except Exception as exc:
        logger.warning(
            "collect_incoming_claims: failed to parse incoming section in %s",
            md_file,
            exc_info=True,
        )
        anomalies.append(
            AnomalyEntry(
                type=AnomalyType.MALFORMED_YAML,
                file=str(md_file),
                message=f"incoming section: {exc}",
                channel="parser",
            )
        )


def check_bidirectional_consistency(
    output_dir: Path,
    edge_data: Dict[Tuple[str, str], Dict[str, Any]],
    incoming_claims: Set[frozenset],
    anomalies: List[AnomalyEntry],
) -> None:
    """Emit data-channel anomalies for mismatched outgoing/incoming claims.

    Uses frozenset-keyed matching so that the symmetric pair A->B / B->A
    collapses to a single BIDIRECTIONAL_MISMATCH anomaly (AC6 fix).

    Direction checked: outgoing claim A->B not confirmed by any incoming claim
    containing both A and B.
    """
    seen_pairs: Set[frozenset] = set()
    for src, tgt in edge_data:
        pair = frozenset({src, tgt})
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        if pair not in incoming_claims:
            anomalies.append(
                AnomalyEntry(
                    type=AnomalyType.BIDIRECTIONAL_MISMATCH,
                    file=str(output_dir / f"{tgt}.md"),
                    message=(
                        f"bidirectional mismatch: {src}→{tgt} declared "
                        f"outgoing by {src} but not confirmed by incoming table"
                    ),
                    channel="data",
                )
            )


def finalize_graph_edges(
    output_dir: Path,
    edge_data: Dict[Tuple[str, str], Dict[str, Any]],
    anomalies: List[AnomalyEntry],
) -> List[Dict[str, Any]]:
    """Build the final edges list, enforcing the no-empty-types rule.

    Edges with an empty types set are omitted and produce a data-channel anomaly.
    Remaining edges have their types converted to a sorted list for deterministic
    JSON output. Result is sorted by (source_domain, target_domain).
    """
    edges: List[Dict[str, Any]] = []
    for (src, tgt), data in edge_data.items():
        types_set = data["types"]
        if not types_set:
            anomalies.append(
                AnomalyEntry(
                    type=AnomalyType.GARBAGE_DOMAIN_REJECTED,
                    file=str(output_dir / f"{src}.md"),
                    message=f"edge {src}→{tgt} has no derivable types",
                    channel="data",
                )
            )
            if src != tgt:
                # Non-self-loop with empty types is garbage — drop it.
                continue
            # Self-loop: AC4 mandates unconditional preservation even when types
            # are empty. Fall through to emit the edge with an empty types list.
        edges.append(
            {
                "source_domain": src,
                "target_domain": tgt,
                "dependency_count": data["count"],
                "types": sorted(types_set),
            }
        )
    edges.sort(key=lambda e: (e["source_domain"], e["target_domain"]))
    return edges


def parse_domain_file_for_graph(
    output_dir: Path,
    base_dir: Path,
    domain_name: str,
    edge_data: Dict[Tuple[str, str], Dict[str, Any]],
    incoming_claims: Set[frozenset],
    anomalies: List[AnomalyEntry],
) -> None:
    """Read one domain file and populate edge_data and incoming_claims.

    Performs path-traversal guard, then reads and validates frontmatter.
    Outgoing and incoming sections are each wrapped in their own try/except
    so that one section's failure does not abort the other.
    """
    md_file = (output_dir / f"{domain_name}.md").resolve()
    try:
        md_file.relative_to(base_dir)
    except ValueError:
        anomalies.append(
            AnomalyEntry(
                type=AnomalyType.PATH_TRAVERSAL_REJECTED,
                file=str(md_file),
                message="domain_name path traversal rejected",
                channel="parser",
            )
        )
        return

    try:
        content = md_file.read_text(encoding="utf-8")
        parse_frontmatter_strict(content)
    except Exception as exc:
        logger.warning(
            "parse_domain_file_for_graph: failed to read/parse %s",
            md_file,
            exc_info=True,
        )
        anomalies.append(
            AnomalyEntry(
                type=AnomalyType.MALFORMED_YAML,
                file=str(md_file),
                message=str(exc),
                channel="parser",
            )
        )
        return

    collect_outgoing_edges(content, domain_name, md_file, edge_data, anomalies)
    collect_incoming_claims(content, domain_name, md_file, incoming_claims, anomalies)


def apply_edge_hygiene(
    edge_data: Dict[Tuple[str, str], Dict[str, Any]],
    anomalies: List[AnomalyEntry],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Apply AC1/AC3 hygiene to edge_data keys: normalize identifiers, merge buckets.

    Each (src, tgt) key is normalized to lowercase (after backtick stripping).
    Buckets for keys that normalize to the same pair are merged.
    A CASE_NORMALIZATION_APPLIED anomaly is appended for each key that changed.

    Returns a new edge_data dict with normalized keys.
    """
    normalized: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for (src, tgt), data in edge_data.items():
        norm_src, src_mod = normalize_identifier(src)
        norm_tgt, tgt_mod = normalize_identifier(tgt)
        if src_mod or tgt_mod:
            anomalies.append(
                AnomalyEntry(
                    type=AnomalyType.CASE_NORMALIZATION_APPLIED,
                    file="",
                    message=(
                        f"normalized edge: {src!r}→{tgt!r} → {norm_src!r}→{norm_tgt!r}"
                    ),
                    channel="data",
                )
            )
        key = (norm_src, norm_tgt)
        if key not in normalized:
            normalized[key] = {"count": 0, "types": set()}
        normalized[key]["count"] += data["count"]
        normalized[key]["types"].update(data["types"])
    return normalized


def emit_self_loop_anomalies(
    edge_data: Dict[Tuple[str, str], Dict[str, Any]],
    output_dir: Path,
    anomalies: List[AnomalyEntry],
) -> None:
    """Emit AC4 SELF_LOOP data-channel anomaly for each self-loop edge.

    Self-loop edges (src == tgt) are preserved in edge_data; this function
    only appends the required anomaly entry per self-loop.
    """
    for src, tgt in edge_data:
        if src == tgt:
            anomalies.append(
                AnomalyEntry(
                    type=AnomalyType.SELF_LOOP,
                    file=str(output_dir / f"{src}.md"),
                    message=f"self-loop edge: {src}→{tgt}",
                    channel="data",
                )
            )


def _aggregate_graph(
    output_dir: Path,
) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], List[AnomalyEntry]]:
    """Parse all domain files in output_dir and return (edge_data, raw_anomalies).

    Shared helper consumed by both DepMapMCPParser.get_cross_domain_graph_with_channels
    and depmap_get_hub_domains_handler. Single source of truth for the parsing loop
    (AC4, Story #889).

    Returns:
        edge_data    — dict keyed by (src, tgt) tuples, values have 'count' and 'types'.
        raw_anomalies — list of AnomalyEntry from parsing; not yet deduped or aggregated.

    If output_dir does not exist or _domains.json is empty, returns ({}, []).
    """
    from code_indexer.server.services.dep_map_file_utils import load_domains_json

    if not output_dir.exists():
        return {}, []

    domains = load_domains_json(output_dir)
    if not domains:
        return {}, []

    base_dir = output_dir.resolve()
    edge_data: Dict[Tuple[str, str], Dict[str, Any]] = {}
    incoming_claims: Set[frozenset] = set()
    raw_anomalies: List[AnomalyEntry] = []

    for domain in domains:
        if not isinstance(domain, dict):
            continue
        domain_name = domain.get("name", "")
        if not domain_name:
            continue
        parse_domain_file_for_graph(
            output_dir,
            base_dir,
            domain_name,
            edge_data,
            incoming_claims,
            raw_anomalies,
        )

    edge_data = apply_edge_hygiene(edge_data, raw_anomalies)
    return edge_data, raw_anomalies


def build_graph_anomalies(
    output_dir: Path,
    edge_data: Dict[Tuple[str, str], Dict[str, Any]],
    incoming_claims: Set[frozenset],
    raw_anomalies: List[AnomalyEntry],
) -> Tuple[
    List[Union[AnomalyEntry, AnomalyAggregate]],
    List[Union[AnomalyEntry, AnomalyAggregate]],
    List[Union[AnomalyEntry, AnomalyAggregate]],
]:
    """AC4-AC7 post-processing: self-loops, bidi-check, dedup, aggregate, channel split.

    Returns (all_anomalies, parser_anomalies, data_anomalies).
    all_anomalies may contain AnomalyAggregate entries when per-type count > threshold.

    Channel split (Approach B): each item in aggregated is routed to its channel list
    via item.type.channel. AnomalyAggregate carries .type which has the bound .channel
    attribute, so aggregates are correctly routed without being silently dropped.
    """
    emit_self_loop_anomalies(edge_data, output_dir, raw_anomalies)
    check_bidirectional_consistency(
        output_dir, edge_data, incoming_claims, raw_anomalies
    )
    deduped = deduplicate_anomalies(raw_anomalies)
    aggregated = aggregate_anomalies(deduped)
    # Approach B: route each item (AnomalyEntry or AnomalyAggregate) to its channel
    # using the self-classifying AnomalyType.channel attribute. This ensures aggregates
    # are not silently dropped from parser_anomalies / data_anomalies (NEW-2 fix).
    parser_anomalies: List[Union[AnomalyEntry, AnomalyAggregate]] = []
    data_anomalies: List[Union[AnomalyEntry, AnomalyAggregate]] = []
    for item in aggregated:
        if item.type.channel == "parser":
            parser_anomalies.append(item)
        else:
            data_anomalies.append(item)
    return aggregated, parser_anomalies, data_anomalies
