"""
DepMapMCPParser -- shared parser for dependency-map MCP tools (Story #855).

Reads the dependency-map directory from cidx-meta and exposes query methods
used by the depmap MCP handlers. No I/O at construction; all I/O deferred
to method calls.

Story #887: parser hygiene and anomaly channel hardening.
  AC1: strip backticks from all identifier fields
  AC2-AC7: additional hygiene and channel split (see get_cross_domain_graph)
  AC8: module split into 4 files each <=500 lines
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import yaml

# Any is justified: domain summary dicts contain heterogeneous YAML/JSON values
# (str, int, list, dict) with no fixed schema across all fields.

from code_indexer.server.services.dep_map_file_utils import (
    get_domain_md_files,
    load_domains_json,
)
from code_indexer.server.services.dep_map_parser_graph import (
    apply_edge_hygiene,
    build_graph_anomalies,
    finalize_graph_edges,
    parse_domain_file_for_graph,
)
from code_indexer.server.services.dep_map_parser_hygiene import (
    AnomalyAggregate,
    AnomalyEntry,
    strip_backticks,
)
from code_indexer.server.services.dep_map_parser_tables import (
    build_cross_domain_connections,
    build_name_description,
    build_participating_repos,
    parse_frontmatter_strict,
    parse_incoming_table,
    parse_last_analyzed,
    parse_roles_table,
)

logger = logging.getLogger(__name__)


def _parse_file_for_consumers(
    md_file: Path,
    repo_name: str,
    domain_repos: Dict[str, List[str]],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Parse one domain markdown file and extract consumer rows for repo_name.

    AC1: strips backticks from domain_name, depends_on, and consuming_repo fields.

    Raises:
        OSError: on file read failure.
        ValueError: on malformed frontmatter.
    """
    content = md_file.read_text(encoding="utf-8")
    fm = parse_frontmatter_strict(content)
    domain_name = fm.get("name", md_file.stem) if fm else md_file.stem
    domain_name = strip_backticks(domain_name)

    incoming = parse_incoming_table(content)
    rows: List[Dict[str, str]] = []
    anomalies: List[Dict[str, str]] = []

    for row in incoming:
        depends_on = strip_backticks(row["depends_on"])
        if depends_on != repo_name:
            continue
        consuming_repo = strip_backticks(row["external_repo"])

        if domain_name in domain_repos:
            json_repos = domain_repos[domain_name]
            if repo_name not in json_repos:
                anomalies.append(
                    {
                        "file": str(md_file),
                        "error": (
                            f"Inconsistency: markdown table references '{repo_name}' "
                            f"as dependency in domain '{domain_name}' but "
                            f"_domains.json does not list it in participating_repos"
                        ),
                    }
                )

        rows.append(
            {
                "domain": domain_name,
                "consuming_repo": consuming_repo,
                "dependency_type": row["dep_type"],
                "evidence": row["evidence"],
            }
        )

    return rows, anomalies


def _lookup_domain_entry(
    domains: List[Dict[str, Any]], domain_name: str
) -> Optional[Dict[str, Any]]:
    """Return the first domain dict whose 'name' equals domain_name, or None."""
    for d in domains:
        if isinstance(d, dict) and d.get("name") == domain_name:
            return d
    return None


def _read_domain_md_content(
    md_file: Path,
) -> Tuple[str, Optional[Dict[str, str]]]:
    """Read a domain .md file. Returns (content, None) on success or ("", anomaly) on error."""
    if not md_file.exists():
        logger.warning("get_domain_summary: .md file not found: %s", md_file)
        return "", {"file": str(md_file), "error": "file not found"}
    try:
        return md_file.read_text(encoding="utf-8"), None
    except OSError as exc:
        logger.warning("get_domain_summary: failed to read %s: %s", md_file, exc)
        return "", {"file": str(md_file), "error": str(exc)}


def _current_utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(tz=timezone.utc)


class DepMapMCPParser:
    """
    Parser for the dependency-map output directory in cidx-meta.

    Constructor stores the root path (parent of dependency-map/).
    No I/O is performed at construction time.

    Public methods return (results, anomalies) 2-tuples EXCEPT
    get_cross_domain_graph which returns a 4-tuple
    (edges, anomalies, parser_anomalies, data_anomalies).
    """

    def __init__(self, dep_map_path: Path) -> None:
        self._dep_map_path = dep_map_path

    def find_consumers(
        self, repo_name: str
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        """Return all repos that depend on repo_name, across every domain."""
        if not repo_name:
            return [], []

        output_dir = self._dep_map_path / "dependency-map"
        if not output_dir.exists():
            return [], []

        domains = load_domains_json(output_dir)
        domain_repos: Dict[str, List[str]] = {
            d["name"]: list(d.get("participating_repos") or [])
            for d in domains
            if isinstance(d, dict) and d.get("name")
        }

        consumers: List[Dict[str, str]] = []
        anomalies: List[Dict[str, str]] = []

        for md_file in get_domain_md_files(output_dir):
            try:
                rows, file_anomalies = _parse_file_for_consumers(
                    md_file, repo_name, domain_repos
                )
                consumers.extend(rows)
                anomalies.extend(file_anomalies)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                anomalies.append({"file": str(md_file), "error": str(exc)})

        return consumers, anomalies

    def get_repo_domains(
        self, repo_name: str
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        """Return all domains that repo_name participates in, with its role."""
        if not repo_name:
            return [], []

        output_dir = self._dep_map_path / "dependency-map"
        if not output_dir.exists():
            return [], []

        domains = load_domains_json(output_dir)
        memberships: List[Dict[str, str]] = []
        anomalies: List[Dict[str, str]] = []

        for domain in domains:
            if not isinstance(domain, dict):
                continue
            domain_name = domain.get("name", "")
            if not domain_name:
                continue
            if repo_name not in (domain.get("participating_repos") or []):
                continue

            md_file = output_dir / f"{domain_name}.md"
            role = ""
            if md_file.exists():
                try:
                    content = md_file.read_text(encoding="utf-8")
                    parse_frontmatter_strict(content)
                    role = parse_roles_table(content).get(repo_name, "")
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    logger.warning(
                        "get_repo_domains: failed to parse %s: %s", md_file, exc
                    )
                    anomalies.append({"file": str(md_file), "error": str(exc)})
                    memberships.append({"domain_name": domain_name, "role": ""})
                    continue

            memberships.append({"domain_name": domain_name, "role": role})

        return memberships, anomalies

    def get_domain_summary(
        self, domain_name: str
    ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, str]]]:
        """Return structured summary for a named domain.

        Includes path-traversal guard to prevent directory escape via domain_name.
        Unknown domains return (None, []). Missing dep-map path returns (None, []).
        """
        if not domain_name:
            return None, []

        output_dir = self._dep_map_path / "dependency-map"
        if not output_dir.exists():
            return None, []

        domains = load_domains_json(output_dir)
        domain_entry = _lookup_domain_entry(domains, domain_name)
        if domain_entry is None:
            return None, []

        # Path-traversal guard: mirrors get_stale_domains pattern
        base_dir = output_dir.resolve()
        md_file = (output_dir / f"{domain_name}.md").resolve()
        try:
            md_file.relative_to(base_dir)
        except ValueError:
            return None, [
                {"file": str(md_file), "error": "domain_name path traversal rejected"}
            ]

        anomalies: List[Dict[str, str]] = []
        content, read_anomaly = _read_domain_md_content(md_file)
        if read_anomaly:
            anomalies.append(read_anomaly)

        name, description, fm_anomaly = build_name_description(
            content,
            md_file,
            fallback_name=domain_name,
            fallback_description=domain_entry.get("description", ""),
        )
        if fm_anomaly:
            anomalies.append(fm_anomaly)

        participating_repos, pr_anomaly = build_participating_repos(content, md_file)
        if pr_anomaly:
            anomalies.append(pr_anomaly)

        cross_domain_connections, cdc_anomaly = build_cross_domain_connections(
            content, md_file
        )
        if cdc_anomaly:
            anomalies.append(cdc_anomaly)

        return {
            "name": name,
            "description": description,
            "participating_repos": participating_repos,
            "cross_domain_connections": cross_domain_connections,
        }, anomalies

    def get_stale_domains(
        self,
        days_threshold: int,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        """Return domains whose last_analyzed is older than days_threshold days.

        Args:
            days_threshold: Minimum days_stale for inclusion. Must be >= 0.

        Returns:
            (stale_domains, anomalies) sorted descending by days_stale.
            stale_domains entries: {domain_name, last_analyzed, days_stale}.
            anomalies entries: {file, error} for missing/unparseable last_analyzed.

        Raises:
            ValueError: when days_threshold < 0.
        """
        if days_threshold < 0:
            raise ValueError("days_threshold must be non-negative")

        output_dir = self._dep_map_path / "dependency-map"
        if not output_dir.exists():
            return [], []

        base_dir = output_dir.resolve()
        domains = load_domains_json(output_dir)
        now_utc = _current_utc_now()
        stale_domains: List[Dict[str, Any]] = []
        anomalies: List[Dict[str, str]] = []

        for domain in domains:
            if not isinstance(domain, dict):
                continue
            domain_name = domain.get("name", "")
            if not domain_name:
                continue
            md_file = (output_dir / f"{domain_name}.md").resolve()
            try:
                md_file.relative_to(base_dir)
            except ValueError:
                anomalies.append(
                    {
                        "file": str(md_file),
                        "error": "domain_name path traversal rejected",
                    }
                )
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                fm = parse_frontmatter_strict(content) or {}
                if "last_analyzed" not in fm:
                    raise ValueError("last_analyzed field missing from frontmatter")
                last_analyzed_dt = parse_last_analyzed(str(fm["last_analyzed"]))
                days_stale = (now_utc - last_analyzed_dt).days
                if days_stale >= days_threshold:
                    stale_domains.append(
                        {
                            "domain_name": domain_name,
                            "last_analyzed": last_analyzed_dt.isoformat(),
                            "days_stale": days_stale,
                        }
                    )
            except (OSError, ValueError, yaml.YAMLError) as exc:
                anomalies.append({"file": str(md_file), "error": str(exc)})

        stale_domains.sort(key=lambda d: d["days_stale"], reverse=True)
        return stale_domains, anomalies

    def get_cross_domain_graph_with_channels(
        self,
    ) -> Tuple[
        List[Dict[str, Any]],
        List[Union[AnomalyEntry, AnomalyAggregate]],
        List[Union[AnomalyEntry, AnomalyAggregate]],  # parser_anomalies
        List[Union[AnomalyEntry, AnomalyAggregate]],  # data_anomalies
    ]:
        """Return the full directed domain-to-domain edge graph with AC1-AC7 hygiene.

        Returns:
            (edges, anomalies, parser_anomalies, data_anomalies)
            edges — sorted list of {source_domain, target_domain,
                dependency_count, types} dicts (self-loops preserved per AC4).
            anomalies — deduplicated, aggregated union of parser + data anomalies.
            parser_anomalies — structural/format-level anomalies (channel='parser').
            data_anomalies — semantic/consistency anomalies (channel='data').
        """
        output_dir = self._dep_map_path / "dependency-map"
        if not output_dir.exists():
            return [], [], [], []

        domains = load_domains_json(output_dir)
        if not domains:
            return [], [], [], []

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
        # finalize_graph_edges runs BEFORE build_graph_anomalies so that any
        # anomalies it emits (e.g. GARBAGE_DOMAIN_REJECTED for empty-types edges)
        # are included in dedup, aggregation, and channel split (Blocker 2 fix).
        edges = finalize_graph_edges(output_dir, edge_data, raw_anomalies)
        all_anomalies, parser_anomalies, data_anomalies = build_graph_anomalies(
            output_dir, edge_data, incoming_claims, raw_anomalies
        )
        return edges, all_anomalies, parser_anomalies, data_anomalies

    def get_cross_domain_graph(
        self,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        """Return (edges, anomalies) — legacy 2-tuple public API.

        Delegates to get_cross_domain_graph_with_channels() and converts each
        anomaly to a plain {file, error} dict matching the original public
        contract so callers that unpack exactly 2 values continue to work
        and can directly serialize anomalies to JSON without type inspection.

        AnomalyEntry  → {"file": entry.file, "error": entry.message}
        AnomalyAggregate → {"file": "<aggregated>",
                            "error": "<N> occurrences: <type>"}

        Use get_cross_domain_graph_with_channels() when typed anomaly objects
        and channel separation are needed.
        """
        edges, all_anomalies, _, _ = self.get_cross_domain_graph_with_channels()
        dicts: List[Dict[str, str]] = []
        for anomaly in all_anomalies:
            if isinstance(anomaly, AnomalyAggregate):
                dicts.append(
                    {
                        "file": "<aggregated>",
                        "error": f"{anomaly.count} occurrences: {anomaly.type.value}",
                    }
                )
            else:
                dicts.append({"file": anomaly.file, "error": anomaly.message})
        return edges, dicts
