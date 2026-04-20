"""
DepMapMCPParser — shared parser for dependency-map MCP tools (Story #855).

Reads the dependency-map directory from cidx-meta and exposes query methods
used by the depmap MCP handlers. No I/O at construction; all I/O deferred
to method calls.

Only find_consumers is fully implemented in Story #855 (S1).
The remaining four methods are correct-signature stubs — Stories 2-4 are
strictly additive.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from code_indexer.server.services.dep_map_file_utils import (
    get_domain_md_files,
    load_domains_json,
)

logger = logging.getLogger(__name__)

# Column indices in the Incoming Dependencies table (0-based, after stripping outer pipes)
_COL_EXTERNAL_REPO = 0
_COL_DEPENDS_ON = 1
_COL_SOURCE_DOMAIN = 2
_COL_DEP_TYPE = 3
_COL_WHY = 4
_COL_EVIDENCE = 5
_INCOMING_MIN_COLS = 6

# Header sentinel to skip when parsing table rows
_INCOMING_HEADER_SENTINEL = "External Repo"


class DepMapMCPParser:
    """
    Parser for the dependency-map output directory in cidx-meta.

    Constructor stores the root path (parent of dependency-map/).
    No I/O is performed at construction time.

    All public methods return (results, anomalies) tuples:
      - results: list of dicts or None (for get_domain_summary)
      - anomalies: list of {"file": str, "error": str} dicts
    """

    def __init__(self, dep_map_path: Path) -> None:
        """
        Store dep_map_path.  No I/O performed here.

        Args:
            dep_map_path: Parent directory that contains the
                          ``dependency-map/`` subdirectory.
        """
        self._dep_map_path = dep_map_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_consumers(
        self, repo_name: str
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        """
        Return all repos that depend on repo_name, across every domain.

        Dual-source: _domains.json provides domain membership; the markdown
        Incoming Dependencies table provides dependency_type and evidence.
        Inconsistency between the two sources emits an anomaly entry.

        Resilience: every per-file parse is wrapped in try/except; failures
        append {"file": path, "error": message} to anomalies and continue.

        Empty repo_name is a valid no-op: returns ([], []) immediately.
        Missing dependency-map directory is a valid no-op per spec AC3.

        Args:
            repo_name: Repository alias to search for (the "Depends On" column).

        Returns:
            (consumers, anomalies)
            consumers — list of dicts with keys:
                domain, consuming_repo, dependency_type, evidence
            anomalies — list of dicts with keys:
                file, error
        """
        if not repo_name:
            logger.debug("find_consumers called with empty repo_name — returning empty")
            return [], []

        output_dir = self._dep_map_path / "dependency-map"
        if not output_dir.exists():
            logger.debug(
                "find_consumers: dependency-map dir not found at %s — returning empty",
                output_dir,
            )
            return [], []

        # Load _domains.json once — not inside the per-file loop
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
                rows, file_anomalies = self._parse_file_for_consumers(
                    md_file, repo_name, domain_repos
                )
                consumers.extend(rows)
                anomalies.extend(file_anomalies)
            except Exception as exc:
                anomalies.append({"file": str(md_file), "error": str(exc)})

        return consumers, anomalies

    def get_repo_domains(
        self, repo_name: str
    ) -> Tuple[List[str], List[Dict[str, str]]]:
        """Stub — Story 2 will implement. Returns ([], [])."""
        return [], []

    def get_domain_summary(
        self, domain_name: str
    ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, str]]]:
        """Stub — Story 3 will implement. Returns (None, [])."""
        return None, []

    def get_stale_domains(
        self,
    ) -> Tuple[List[str], List[Dict[str, str]]]:
        """Stub — Story 3 will implement. Returns ([], [])."""
        return [], []

    def get_cross_domain_graph(
        self,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        """Stub — Story 4 will implement. Returns ([], [])."""
        return [], []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_file_for_consumers(
        self,
        md_file: Path,
        repo_name: str,
        domain_repos: Dict[str, List[str]],
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        """
        Parse one domain markdown file and extract consumer rows for repo_name.

        Raises yaml.YAMLError on malformed YAML frontmatter so the caller
        can record an anomaly with the file path.

        Returns:
            (rows, anomalies) — rows are consumer dicts; anomalies contain
            inconsistency warnings about dual-source mismatches.
        """
        content = md_file.read_text(encoding="utf-8")

        fm = self._parse_frontmatter_strict(content)
        domain_name = fm.get("name", md_file.stem) if fm else md_file.stem

        incoming = self._parse_incoming_table(content)

        rows: List[Dict[str, str]] = []
        anomalies: List[Dict[str, str]] = []

        for row in incoming:
            if row["depends_on"] != repo_name:
                continue

            consuming_repo = row["external_repo"]

            # Dual-source consistency check: _domains.json vs markdown table
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

    def _parse_frontmatter_strict(self, content: str) -> Optional[Dict[str, Any]]:
        """
        Parse YAML frontmatter, raising yaml.YAMLError on malformed YAML.

        Unlike dep_map_file_utils.parse_yaml_frontmatter (which silently
        returns None on errors), this raises so that the caller can record
        an anomaly with the file path.
        """
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        # Raises yaml.YAMLError if the frontmatter block is malformed
        return yaml.safe_load(parts[1]) or {}

    @staticmethod
    def _parse_incoming_table(
        content: str,
    ) -> List[Dict[str, str]]:
        """
        Extract rows from the '### Incoming Dependencies' table in a domain file.

        Expected columns (6):
          External Repo | Depends On | Source Domain | Type | Why | Evidence

        Cell splitting uses split("|")[1:-1] to preserve empty cells and
        maintain correct column positions even when cells are blank.

        Returns:
            List of dicts with keys:
            external_repo, depends_on, source_domain, dep_type, why, evidence
        """
        rows: List[Dict[str, str]] = []
        in_incoming = False

        for line in content.splitlines():
            stripped = line.strip()

            if stripped == "### Incoming Dependencies":
                in_incoming = True
                continue

            if in_incoming and stripped.startswith("##"):
                break

            if not in_incoming:
                continue

            if not (stripped.startswith("|") and stripped.endswith("|")):
                continue

            # Preserve all cells including empty ones; discard outer delimiters
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if len(cells) < _INCOMING_MIN_COLS:
                continue

            # Skip header row
            if cells[_COL_EXTERNAL_REPO] == _INCOMING_HEADER_SENTINEL:
                continue

            # Skip separator row (dashes only in first cell)
            if set(cells[_COL_EXTERNAL_REPO]) <= frozenset("-"):
                continue

            rows.append(
                {
                    "external_repo": cells[_COL_EXTERNAL_REPO],
                    "depends_on": cells[_COL_DEPENDS_ON],
                    "source_domain": cells[_COL_SOURCE_DOMAIN],
                    "dep_type": cells[_COL_DEP_TYPE],
                    "why": cells[_COL_WHY],
                    "evidence": cells[_COL_EVIDENCE],
                }
            )

        return rows
