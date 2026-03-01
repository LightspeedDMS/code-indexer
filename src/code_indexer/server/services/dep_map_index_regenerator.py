"""
IndexRegenerator service for Story #342 AC14.

Regenerates _index.md deterministically from existing domain .md files and
_domains.json. No Claude CLI required -- this is a pure structural rebuild
from already-analyzed data.

Algorithm:
  1. Load _domains.json to get the domain list
  2. For each domain, read its .md file and parse YAML frontmatter
  3. Build Domain Catalog table from domain names, descriptions, repo counts
  4. Build Repo-to-Domain Matrix from participating_repos in each domain
  5. Scan domain files for cross-domain connection references (best-effort)
  6. Write fresh _index.md with frontmatter + all three sections
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from code_indexer.server.services.dep_map_file_utils import (
    load_domains_json as _load_domains_json_util,
    parse_yaml_frontmatter as _parse_yaml_frontmatter_util,
    parse_simple_yaml as _parse_simple_yaml_util,
)

logger = logging.getLogger(__name__)

# Section headers that may contain cross-domain dependency information
CROSS_DOMAIN_SECTION_HEADERS = [
    "## Cross-Domain Connections",
    "## Cross-Domain Dependencies",
]

# Phrases that indicate no cross-domain deps in a section
NO_DEPS_PHRASES = [
    "no verified cross-domain",
    "no cross-domain dependencies",
    "none",
]


class IndexRegenerator:
    """
    Regenerates _index.md from existing domain .md files and _domains.json.

    Pure write service -- reads domain files, writes _index.md.
    Call regenerate(output_dir) to rebuild the index file.
    """

    def regenerate(self, output_dir: Path) -> Path:
        """
        Regenerate _index.md in output_dir from existing domain files.

        Args:
            output_dir: Path to the dependency map output directory
                        (e.g. cidx-meta/dependency-map/)

        Returns:
            Path to the written _index.md file.
        """
        output_dir = Path(output_dir)

        domain_list = self._load_domains_json(output_dir)

        catalog_rows = self._build_catalog_rows(output_dir, domain_list)
        matrix_rows = self._build_matrix_rows(output_dir, domain_list)
        cross_domain_edges = self._collect_cross_domain_edges(output_dir, domain_list)

        all_repos = sorted({row[0] for row in matrix_rows})

        content = self._format_index_md(
            catalog_rows=catalog_rows,
            matrix_rows=matrix_rows,
            cross_domain_edges=cross_domain_edges,
            repos=all_repos,
            domain_count=len(catalog_rows),
        )

        index_path = output_dir / "_index.md"
        index_path.write_text(content, encoding="utf-8")
        logger.info("Wrote regenerated _index.md to %s", index_path)
        return index_path

    # ─────────────────────────────────────────────────────────────────────────
    # Data loading
    # ─────────────────────────────────────────────────────────────────────────

    def _load_domains_json(self, output_dir: Path) -> List[Dict[str, Any]]:
        """Load _domains.json, returning empty list if missing or invalid."""
        return _load_domains_json_util(output_dir)

    # ─────────────────────────────────────────────────────────────────────────
    # Catalog building
    # ─────────────────────────────────────────────────────────────────────────

    def _build_catalog_rows(
        self, output_dir: Path, domain_list: List[Dict[str, Any]]
    ) -> List[Tuple[str, str, int]]:
        """
        Build rows for the Domain Catalog table.

        Returns list of (domain_name, description, repo_count) tuples.
        Only includes domains whose .md file exists.
        """
        rows = []
        for domain in domain_list:
            name = domain.get("name", "")
            if not name:
                continue
            md_file = output_dir / f"{name}.md"
            if not md_file.exists():
                logger.debug("Skipping missing domain file: %s", md_file)
                continue

            frontmatter = self._parse_yaml_frontmatter_from_file(md_file)
            description = (
                domain.get("description", "")
                or (frontmatter.get("description", "") if frontmatter else "")
            )
            repos = self._get_repos_for_domain(domain, frontmatter)
            rows.append((name, description, len(repos)))

        return rows

    # ─────────────────────────────────────────────────────────────────────────
    # Matrix building
    # ─────────────────────────────────────────────────────────────────────────

    def _build_matrix_rows(
        self, output_dir: Path, domain_list: List[Dict[str, Any]]
    ) -> List[Tuple[str, str]]:
        """
        Build rows for the Repo-to-Domain Matrix table.

        Returns list of (repo_name, domain_name) tuples.
        Only includes domains whose .md file exists.
        """
        rows = []
        for domain in domain_list:
            name = domain.get("name", "")
            if not name:
                continue
            md_file = output_dir / f"{name}.md"
            if not md_file.exists():
                continue

            frontmatter = self._parse_yaml_frontmatter_from_file(md_file)
            repos = self._get_repos_for_domain(domain, frontmatter)
            for repo in repos:
                rows.append((repo, name))

        return rows

    # ─────────────────────────────────────────────────────────────────────────
    # Cross-domain dependency collection
    # ─────────────────────────────────────────────────────────────────────────

    def _collect_cross_domain_edges(
        self, output_dir: Path, domain_list: List[Dict[str, Any]]
    ) -> List[Tuple[str, str, str]]:
        """
        Collect cross-domain dependency edges from domain .md file contents.

        Scans each domain file for a Cross-Domain section. If the section
        explicitly states there are no dependencies, it is skipped.

        Returns list of (source_domain, target_domain, evidence) tuples.
        This is best-effort: if a file has complex or unparseable references,
        the section is skipped rather than producing garbage output.
        """
        edges = []
        domain_names = {d.get("name", "") for d in domain_list if d.get("name")}

        for domain in domain_list:
            name = domain.get("name", "")
            if not name:
                continue
            md_file = output_dir / f"{name}.md"
            if not md_file.exists():
                continue

            try:
                content = md_file.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("Failed to read %s: %s", md_file, e)
                continue

            section_edges = self._parse_cross_domain_section(
                content, name, domain_names
            )
            edges.extend(section_edges)

        return edges

    def _parse_cross_domain_section(
        self, content: str, source_domain: str, known_domains: set
    ) -> List[Tuple[str, str, str]]:
        """
        Parse cross-domain dependency references from a domain file's content.

        Looks for a Cross-Domain section header and extracts references to
        other known domain names. Returns empty list if section indicates
        no dependencies or if parsing yields no usable references.
        """
        # Find the cross-domain section
        section_start = -1
        section_header = ""
        for header in CROSS_DOMAIN_SECTION_HEADERS:
            idx = content.find(header)
            if idx != -1:
                section_start = idx
                section_header = header
                break

        if section_start == -1:
            return []

        # Extract section body (up to next ## header or end of file)
        body_start = section_start + len(section_header)
        next_header = content.find("\n## ", body_start)
        if next_header != -1:
            section_body = content[body_start:next_header]
        else:
            section_body = content[body_start:]

        section_lower = section_body.lower().strip()

        # Check for explicit "no dependencies" phrases
        for phrase in NO_DEPS_PHRASES:
            if phrase in section_lower:
                return []

        # Try to find references to known domain names
        edges = []
        for line in section_body.splitlines():
            line_lower = line.lower()
            for target in known_domains:
                if target == source_domain:
                    continue
                if target in line_lower:
                    evidence = line.strip().lstrip("- ").strip()
                    if evidence:
                        edges.append((source_domain, target, evidence))

        return edges

    # ─────────────────────────────────────────────────────────────────────────
    # Frontmatter parsing
    # ─────────────────────────────────────────────────────────────────────────

    def _parse_yaml_frontmatter_from_file(
        self, md_file: Path
    ) -> Optional[Dict[str, Any]]:
        """Read a .md file and parse its YAML frontmatter block."""
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to read %s: %s", md_file, e)
            return None
        return self._parse_yaml_frontmatter(content)

    def _parse_yaml_frontmatter(self, content: str) -> Optional[Dict[str, Any]]:
        """Parse YAML frontmatter block from markdown content."""
        return _parse_yaml_frontmatter_util(content)

    def _parse_simple_yaml(self, lines: List[str]) -> Dict[str, Any]:
        """Parse a simplified YAML structure from frontmatter lines."""
        return _parse_simple_yaml_util(lines)

    # ─────────────────────────────────────────────────────────────────────────
    # Repo resolution helper
    # ─────────────────────────────────────────────────────────────────────────

    def _get_repos_for_domain(
        self,
        domain: Dict[str, Any],
        frontmatter: Optional[Dict[str, Any]],
    ) -> List[str]:
        """
        Resolve the list of participating repos for a domain.

        Priority: frontmatter participating_repos > domain JSON participating_repos
        """
        if frontmatter:
            fm_repos = frontmatter.get("participating_repos", [])
            if isinstance(fm_repos, list) and fm_repos:
                return fm_repos

        json_repos = domain.get("participating_repos", [])
        if isinstance(json_repos, list):
            return json_repos
        return []

    # ─────────────────────────────────────────────────────────────────────────
    # _index.md formatting
    # ─────────────────────────────────────────────────────────────────────────

    def _format_index_md(
        self,
        catalog_rows: List[Tuple[str, str, int]],
        matrix_rows: List[Tuple[str, str]],
        cross_domain_edges: List[Tuple[str, str, str]],
        repos: List[str],
        domain_count: int,
    ) -> str:
        """
        Format the complete _index.md content.

        Args:
            catalog_rows: List of (domain_name, description, repo_count)
            matrix_rows: List of (repo_name, domain_name)
            cross_domain_edges: List of (source_domain, target_domain, evidence)
            repos: Sorted list of all repos across all domains
            domain_count: Total number of domains included

        Returns:
            Complete _index.md content as a string.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Build repos_analyzed YAML list
        repos_yaml = "\n".join(f"  - {r}" for r in repos) if repos else ""
        repos_analyzed_block = f"repos_analyzed:\n{repos_yaml}" if repos else "repos_analyzed: []"

        frontmatter = (
            f"---\n"
            f"schema_version: 1.0\n"
            f"last_analyzed: \"{now}\"\n"
            f"repos_analyzed_count: {len(repos)}\n"
            f"domains_count: {domain_count}\n"
            f"{repos_analyzed_block}\n"
            f"---\n"
        )

        # Domain Catalog table
        catalog_header = (
            "## Domain Catalog\n\n"
            "| Domain | Description | Repo Count |\n"
            "|---|---|---|\n"
        )
        catalog_body = "\n".join(
            f"| {name} | {desc} | {count} |"
            for name, desc, count in catalog_rows
        )
        if not catalog_rows:
            catalog_body = "_No domains._"
        catalog_section = catalog_header + catalog_body + "\n"

        # Repo-to-Domain Matrix table
        matrix_header = (
            "## Repo-to-Domain Matrix\n\n"
            "| Repository | Domain |\n"
            "|---|---|\n"
        )
        matrix_body = "\n".join(
            f"| {repo} | {domain} |"
            for repo, domain in sorted(matrix_rows)
        )
        if not matrix_rows:
            matrix_body = "_No repositories._"
        matrix_section = matrix_header + matrix_body + "\n"

        # Cross-Domain Dependencies table
        if cross_domain_edges:
            cross_header = (
                "## Cross-Domain Dependencies\n\n"
                "| Source Domain | Target Domain | Evidence |\n"
                "|---|---|---|\n"
            )
            cross_body = "\n".join(
                f"| {src} | {tgt} | {evidence} |"
                for src, tgt, evidence in cross_domain_edges
            )
            cross_section = cross_header + cross_body + "\n"
        else:
            cross_section = (
                "## Cross-Domain Dependencies\n\n"
                "_No cross-domain dependencies detected._\n"
            )

        return (
            frontmatter
            + "\n"
            + "# Dependency Map Index\n"
            + "\n"
            + catalog_section
            + "\n"
            + matrix_section
            + "\n"
            + cross_section
        )
