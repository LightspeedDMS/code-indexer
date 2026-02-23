"""
DependencyMapDomainService for Story #214 (Domain Explorer with Documentation Detail Panel).

Provides domain explorer data including:
- Domain list loading from _domains.json
- Cross-domain dependency parsing from _index.md
- Domain detail assembly (description, repos, deps, markdown)
- Access filtering (admin vs non-admin)
- Markdown rendering with YAML frontmatter stripping
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import markdown

logger = logging.getLogger(__name__)


class DependencyMapDomainService:
    """
    Service for domain explorer data in the Dependency Map dashboard (Story #214).

    Data sources (all may be absent - handled gracefully):
      {golden_repos_dir}/cidx-meta/dependency-map/_domains.json
      {golden_repos_dir}/cidx-meta/dependency-map/_index.md
      {golden_repos_dir}/cidx-meta/dependency-map/{domain_name}.md
    """

    def __init__(self, dependency_map_service, config_manager) -> None:
        """
        Initialize the domain service.

        Args:
            dependency_map_service: DependencyMapService instance with golden_repos_dir property
            config_manager: ServerConfigManager (reserved for future use)
        """
        self._dependency_map_service = dependency_map_service
        self._config_manager = config_manager

    @staticmethod
    def _validate_domain_name(domain_name: str) -> bool:
        """Validate domain name contains no path traversal sequences."""
        if not domain_name:
            return False
        if '/' in domain_name or '\\' in domain_name or '..' in domain_name:
            return False
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def get_domain_list(self, accessible_repos: Optional[Set[str]] = None) -> Dict[str, Any]:
        """
        Return domain list for the explorer left panel.

        Args:
            accessible_repos: None means admin (all visible).
                              Set[str] means non-admin (filtered to accessible repos).

        Returns:
            Dict with:
              - domains: List[Dict] sorted alphabetically, each with:
                  name, description, repo_count, participating_repos, last_analyzed
              - total_count: int
        """
        raw_domains = self._load_domains_json()

        domains: List[Dict[str, Any]] = []
        for domain in raw_domains:
            name = domain.get("name", "")
            description = domain.get("description", "")
            all_repos: List[str] = domain.get("participating_repos", [])

            # Apply access filtering
            if accessible_repos is not None:
                visible_repos = [r for r in all_repos if r in accessible_repos]
                # Domain only visible if at least one repo is accessible
                if not visible_repos:
                    continue
            else:
                visible_repos = list(all_repos)

            last_analyzed = self._get_domain_last_analyzed(name)

            domains.append({
                "name": name,
                "description": description,
                "repo_count": len(visible_repos),
                "participating_repos": visible_repos,
                "last_analyzed": last_analyzed,
            })

        # Sort alphabetically by name
        domains.sort(key=lambda d: d["name"])

        return {
            "domains": domains,
            "total_count": len(domains),
        }

    def get_domain_detail(
        self,
        domain_name: str,
        accessible_repos: Optional[Set[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Return detail dict for a single domain.

        Args:
            domain_name: Name of the domain to retrieve.
            accessible_repos: None means admin (all visible).
                              Set[str] means non-admin (filtered).

        Returns:
            Dict with name, description, last_analyzed, repos, outgoing_deps,
            incoming_deps, full_documentation_html
            OR None if domain not found.
        """
        if not self._validate_domain_name(domain_name):
            logger.warning("dependency_map_domain: invalid domain name: %s", domain_name)
            return None

        raw_domains = self._load_domains_json()
        domain_data = next((d for d in raw_domains if d.get("name") == domain_name), None)

        if domain_data is None:
            return None

        all_repos: List[str] = domain_data.get("participating_repos", [])

        # Apply repo filtering
        if accessible_repos is not None:
            visible_repos = sorted(r for r in all_repos if r in accessible_repos)
        else:
            visible_repos = sorted(all_repos)

        # Determine visible domains set for cross-dep filtering
        visible_domain_names = self._compute_visible_domain_names(accessible_repos, raw_domains)

        # Parse cross-domain dependencies
        all_deps = self._parse_cross_domain_deps()

        outgoing_deps = [
            dep for dep in all_deps
            if dep["source"] == domain_name and dep["target"] in visible_domain_names
        ]
        incoming_deps = [
            dep for dep in all_deps
            if dep["target"] == domain_name and dep["source"] in visible_domain_names
        ]

        last_analyzed = self._get_domain_last_analyzed(domain_name)
        full_documentation_html = self._render_domain_markdown(domain_name)

        return {
            "name": domain_name,
            "description": domain_data.get("description", ""),
            "last_analyzed": last_analyzed,
            "repos": visible_repos,
            "outgoing_deps": outgoing_deps,
            "incoming_deps": incoming_deps,
            "full_documentation_html": full_documentation_html,
        }

    def get_graph_data(self, accessible_repos: Optional[Set[str]] = None) -> Dict[str, Any]:
        """
        Return graph data (nodes + edges) for the D3.js visualization.

        Args:
            accessible_repos: None means admin (all visible).
                              Set[str] means non-admin (filtered).

        Returns:
            Dict with:
              - nodes: List[Dict] each with id, name, description, repo_count,
                       incoming_dep_count, outgoing_dep_count
              - edges: List[Dict] each with source, target, relationship
        """
        raw_domains = self._load_domains_json()
        visible_domain_names = self._compute_visible_domain_names(accessible_repos, raw_domains)

        # Build edges first: only between visible domains.
        # Edges must be built before nodes so dep counts can be pre-computed.
        all_deps = self._parse_cross_domain_deps()
        edges = []
        for dep in all_deps:
            if dep["source"] in visible_domain_names and dep["target"] in visible_domain_names:
                edges.append({
                    "source": dep["source"],
                    "target": dep["target"],
                    "relationship": dep.get("relationship", ""),
                    "dep_type": dep.get("dep_type", ""),
                })

        # Pre-compute dep counts from visible edges for each node
        outgoing_counts: Dict[str, int] = {}
        incoming_counts: Dict[str, int] = {}
        for edge in edges:
            src = edge["source"]
            tgt = edge["target"]
            outgoing_counts[src] = outgoing_counts.get(src, 0) + 1
            incoming_counts[tgt] = incoming_counts.get(tgt, 0) + 1

        # Build nodes from domain list (apply access filtering)
        domain_list_data = self.get_domain_list(accessible_repos)
        nodes = []
        for domain in domain_list_data["domains"]:
            node_id = domain["name"]
            nodes.append({
                "id": node_id,
                "name": node_id,
                "description": (domain.get("description") or "")[:100],
                "repo_count": domain["repo_count"],
                "incoming_dep_count": incoming_counts.get(node_id, 0),
                "outgoing_dep_count": outgoing_counts.get(node_id, 0),
            })

        return {"nodes": nodes, "edges": edges}

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _get_depmap_dir(self) -> Path:
        """Return the path to the dependency-map directory.

        Uses the versioned cidx-meta path for reads since Story #224 made
        cidx-meta a versioned golden repo. The actual content lives in
        .versioned/cidx-meta/v_*/ rather than the live golden-repos/cidx-meta/.
        """
        cidx_meta_read_path = self._dependency_map_service.cidx_meta_read_path
        return cidx_meta_read_path / "dependency-map"

    def _load_domains_json(self) -> List[Dict[str, Any]]:
        """
        Load _domains.json from the dependency-map directory.

        Returns empty list if file is missing or unreadable.
        """
        try:
            domains_path = self._get_depmap_dir() / "_domains.json"
        except Exception as e:
            logger.warning("dependency_map_domain: failed to get depmap dir: %s", e)
            return []

        if not domains_path.exists():
            return []

        try:
            data = json.loads(domains_path.read_text())
            if not isinstance(data, list):
                logger.warning("dependency_map_domain: _domains.json is not a list")
                return []
            return data
        except Exception as e:
            logger.warning("dependency_map_domain: failed to read _domains.json: %s", e)
            return []

    def _parse_cross_domain_deps(self) -> List[Dict[str, Any]]:
        """
        Parse cross-domain dependency table from _index.md.

        Supports three table formats:
          3-column: Source Domain | Target Domain | Via Repos
          4-column: Source Domain | Target Domain | Via Repos | Relationship
          5-column: Source Domain | Target Domain | Via Repos | Type | Why

        Uses line-by-line pipe splitting instead of regex to avoid the ambiguity
        where a 3-column pattern also matches subsets of 4-column rows.

        Returns:
            List of dicts: {source, target, via_repos, relationship, dep_type, why}
            Empty list if file missing or no table found.
        """
        try:
            index_path = self._get_depmap_dir() / "_index.md"
        except Exception as e:
            logger.warning("dependency_map_domain: failed to get depmap dir: %s", e)
            return []

        if not index_path.exists():
            return []

        try:
            content = index_path.read_text()
        except Exception as e:
            logger.warning("dependency_map_domain: failed to read _index.md: %s", e)
            return []

        deps: List[Dict[str, Any]] = []

        for line in content.splitlines():
            line = line.strip()
            # Must start and end with a pipe to be a table row
            if not line.startswith("|") or not line.endswith("|"):
                continue

            # Split by pipe and strip whitespace from each cell
            cells = [c.strip() for c in line.split("|")]
            # Remove empty strings from leading/trailing pipe split
            cells = [c for c in cells if c != ""]

            # Need 3, 4, or 5 cells
            if len(cells) not in (3, 4, 5):
                continue

            source = cells[0]
            target = cells[1]
            via = cells[2]
            relationship = cells[3] if len(cells) >= 4 else ""
            dep_type = ""
            why = ""

            # 5-column format: Source | Target | Via | Type | Why
            if len(cells) == 5:
                dep_type = cells[3]
                why = cells[4]
                relationship = ""

            # Skip header rows
            if source in ("Source Domain", ""):
                continue

            # Skip separator rows (contain only dashes and spaces)
            if set(source) <= {"-", " "}:
                continue

            deps.append({
                "source": source,
                "target": target,
                "via_repos": [r.strip() for r in via.split(",") if r.strip()],
                "relationship": relationship,
                "dep_type": dep_type,
                "why": why,
            })

        return deps

    def _render_domain_markdown(self, domain_name: str) -> Optional[str]:
        """
        Load domain .md file, strip YAML frontmatter, and render to HTML.

        Args:
            domain_name: Name of the domain (filename without .md extension).

        Returns:
            Rendered HTML string, or None if file missing.
        """
        if not self._validate_domain_name(domain_name):
            return None

        try:
            file_path = self._get_depmap_dir() / f"{domain_name}.md"
        except Exception as e:
            logger.warning("dependency_map_domain: failed to get depmap dir: %s", e)
            return None

        if not file_path.exists():
            return None

        try:
            content = file_path.read_text()
        except Exception as e:
            logger.warning("dependency_map_domain: failed to read %s.md: %s", domain_name, e)
            return None

        # Strip YAML frontmatter (---...---)
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                content = content[end + 3:].strip()

        try:
            html = markdown.markdown(content, extensions=["tables", "fenced_code"])
            # Sanitize: strip script/style/iframe/object tags (defense-in-depth)
            html = re.sub(
                r'<(script|style|iframe|object|embed|form|input)[^>]*>.*?</\1>',
                '',
                html,
                flags=re.IGNORECASE | re.DOTALL,
            )
            html = re.sub(
                r'<(script|style|iframe|object|embed|form|input)[^>]*/?\s*>',
                '',
                html,
                flags=re.IGNORECASE,
            )
            # Strip on* event handlers from remaining tags
            html = re.sub(r'\s+on\w+\s*=\s*"[^"]*"', '', html, flags=re.IGNORECASE)
            html = re.sub(r"\s+on\w+\s*=\s*'[^']*'", '', html, flags=re.IGNORECASE)
            return html
        except Exception as e:
            logger.warning("dependency_map_domain: failed to render markdown for %s: %s", domain_name, e)
            return None

    def _get_domain_last_analyzed(self, domain_name: str) -> Optional[str]:
        """
        Extract last_analyzed from domain .md YAML frontmatter.

        Args:
            domain_name: Name of the domain.

        Returns:
            ISO timestamp string or None if not found.
        """
        if not self._validate_domain_name(domain_name):
            return None

        try:
            file_path = self._get_depmap_dir() / f"{domain_name}.md"
        except Exception as e:
            logger.warning("dependency_map_domain: failed to get depmap dir: %s", e)
            return None

        if not file_path.exists():
            return None

        try:
            content = file_path.read_text()
        except Exception as e:
            logger.warning("dependency_map_domain: failed to read %s.md: %s", domain_name, e)
            return None

        # Extract YAML frontmatter
        if not content.startswith("---"):
            return None

        end = content.find("---", 3)
        if end == -1:
            return None

        frontmatter = content[3:end]

        # Parse last_analyzed field
        match = re.search(r"last_analyzed:\s*(.+?)(?:\n|$)", frontmatter)
        if not match:
            return None

        return match.group(1).strip()

    def _compute_visible_domain_names(
        self,
        accessible_repos: Optional[Set[str]],
        raw_domains: List[Dict[str, Any]],
    ) -> Set[str]:
        """
        Compute the set of domain names visible to the user.

        Admin (accessible_repos=None) sees all domains.
        Non-admin sees only domains with at least one accessible repo.
        """
        if accessible_repos is None:
            return {d.get("name", "") for d in raw_domains if d.get("name")}

        visible = set()
        for domain in raw_domains:
            name = domain.get("name", "")
            if not name:
                continue
            repos = domain.get("participating_repos", [])
            if any(r in accessible_repos for r in repos):
                visible.add(name)
        return visible
