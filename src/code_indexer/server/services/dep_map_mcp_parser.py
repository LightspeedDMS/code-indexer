"""
DepMapMCPParser — shared parser for dependency-map MCP tools (Story #855).

Reads the dependency-map directory from cidx-meta and exposes query methods
used by the depmap MCP handlers. No I/O at construction; all I/O deferred
to method calls.

find_consumers is fully implemented in Story #855 (S1).
get_repo_domains and get_domain_summary are fully implemented in Story #856 (S2).
get_stale_domains and get_cross_domain_graph remain stubs for Stories S3-S4.
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from code_indexer.server.services.dep_map_file_utils import (
    get_domain_md_files,
    load_domains_json,
)

logger = logging.getLogger(__name__)


def _current_utc_now() -> datetime:
    """Return the current UTC time.

    Defined at module level so tests can monkeypatch it for clock-controlled
    assertions without importing or patching datetime itself.
    """
    return datetime.now(timezone.utc)


# Column indices in the Incoming Dependencies table (0-based, after stripping outer pipes)
_COL_EXTERNAL_REPO = 0
_COL_DEPENDS_ON = 1
_COL_SOURCE_DOMAIN = 2
_COL_DEP_TYPE = 3
_COL_WHY = 4
_COL_EVIDENCE = 5
_INCOMING_MIN_COLS = 6

# Column indices in the Repository Roles table (0-based)
_COL_ROLES_REPO = 0
_COL_ROLES_ROLE = 2
_ROLES_MIN_COLS = 3
_ROLES_HEADER_SENTINEL = "Repository"

# Column indices in the Outgoing Dependencies table (0-based)
_COL_OUTGOING_SOURCE_REPO = 0
_COL_OUTGOING_TARGET_DOMAIN = 2
_OUTGOING_MIN_COLS = 4
_OUTGOING_HEADER_SENTINEL = "This Repo"

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
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        """Return all domains that repo_name participates in, with its role in each.

        Reads _domains.json for domain membership. For each matching domain,
        reads the domain .md file and extracts the repo's role from the
        Repository Roles table. Malformed .md files are captured as anomalies;
        other domains continue to be processed.

        Missing dependency-map directory returns ([], []) with no exception.

        Returns:
            (memberships, anomalies)
            memberships — list of {domain_name, role}
            anomalies   — list of {file, error}
        """
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
                    self._parse_frontmatter_strict(content)  # raises on malformed YAML
                    role = self._parse_roles_table(content).get(repo_name, "")
                except Exception as exc:
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

        Looks up the domain in _domains.json, then reads its .md file.
        Each parse section (frontmatter, roles table, outgoing table) is
        independently try/except wrapped so partial failures produce anomalies
        with the section name in the error string rather than aborting the
        whole response.

        Unknown domain returns (None, []). Missing dep-map path returns (None, []).

        Returns:
            (summary, anomalies)
            summary — dict or None:
                {name, description, participating_repos, cross_domain_connections}
            anomalies — list of {file, error}
        """
        output_dir = self._dep_map_path / "dependency-map"
        if not output_dir.exists():
            return None, []

        domains = load_domains_json(output_dir)
        domain_entry = self._lookup_domain_entry(domains, domain_name)
        if domain_entry is None:
            return None, []

        anomalies: List[Dict[str, str]] = []
        md_file = output_dir / f"{domain_name}.md"

        content, read_anomaly = self._read_domain_md_content(md_file)
        if read_anomaly:
            anomalies.append(read_anomaly)

        # Section 1: frontmatter — name and description from the .md file itself,
        # with _domains.json values as fallbacks when the file omits those keys.
        name, description, fm_anomaly = self._build_name_description(
            content,
            md_file,
            fallback_name=domain_name,
            fallback_description=domain_entry.get("description", ""),
        )
        if fm_anomaly:
            anomalies.append(fm_anomaly)

        # Section 2: participating_repos from Repository Roles table
        participating_repos, pr_anomaly = self._build_participating_repos(
            content, md_file
        )
        if pr_anomaly:
            anomalies.append(pr_anomaly)

        # Section 3: cross_domain_connections from Outgoing Dependencies table
        cross_domain_connections, cdc_anomaly = self._build_cross_domain_connections(
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
                fm = self._parse_frontmatter_strict(content) or {}
                if "last_analyzed" not in fm:
                    raise ValueError("last_analyzed field missing from frontmatter")
                last_analyzed_dt = self._parse_last_analyzed(str(fm["last_analyzed"]))
                days_stale = (now_utc - last_analyzed_dt).days
                if days_stale >= days_threshold:
                    stale_domains.append(
                        {
                            "domain_name": domain_name,
                            "last_analyzed": fm["last_analyzed"],
                            "days_stale": days_stale,
                        }
                    )
            except Exception as exc:
                anomalies.append({"file": str(md_file), "error": str(exc)})

        stale_domains.sort(key=lambda d: d["days_stale"], reverse=True)
        return stale_domains, anomalies

    def get_cross_domain_graph(
        self,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        """Stub — Story 4 will implement. Returns ([], [])."""
        return [], []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Low-level table parsers (S2)
    # ------------------------------------------------------------------

    @staticmethod
    def _iter_table_rows(
        content: str,
        section_heading: str,
        min_cols: int,
        header_sentinel: str,
    ):
        """Yield cell lists for each data row in a named markdown table section.

        Activates on `section_heading`. Exits when a subsequent heading whose
        level (number of leading `#` chars) is <= the section heading's level
        is encountered.

        Args:
            content: Full markdown file content.
            section_heading: Exact heading string (e.g. "## Repository Roles").
            min_cols: Minimum number of pipe-separated cells required.
            header_sentinel: First-cell value that identifies the header row.

        Yields:
            List[str] of stripped cell values for each qualifying data row.
        """
        # Compute target section level from leading '#' characters
        section_level = len(section_heading) - len(section_heading.lstrip("#"))
        in_section = False

        for line in content.splitlines():
            stripped = line.strip()

            if stripped == section_heading:
                in_section = True
                continue

            if in_section and stripped.startswith("#"):
                # Compute level of the new heading inline
                lvl = len(stripped) - len(stripped.lstrip("#"))
                if lvl > 0 and stripped[lvl : lvl + 1] == " " and lvl <= section_level:
                    break

            if not in_section:
                continue

            if not (stripped.startswith("|") and stripped.endswith("|")):
                continue

            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if len(cells) < min_cols:
                continue
            if cells[0] == header_sentinel:
                continue
            if set(cells[0]) <= frozenset("-"):
                continue

            yield cells

    @staticmethod
    def _parse_roles_table(content: str) -> Dict[str, str]:
        """Extract repo→role mapping from the '## Repository Roles' table.

        Expected columns (at least 3): Repository | Language | Role

        Returns dict of {repo_name: role_str}.
        """
        result: Dict[str, str] = {}
        for cells in DepMapMCPParser._iter_table_rows(
            content, "## Repository Roles", _ROLES_MIN_COLS, _ROLES_HEADER_SENTINEL
        ):
            repo = cells[_COL_ROLES_REPO].strip("*")  # strip bold markers
            role = cells[_COL_ROLES_ROLE]
            if repo:
                result[repo] = role
        return result

    @staticmethod
    def _parse_outgoing_table(content: str) -> Dict[str, int]:
        """Count rows per target_domain in the '### Outgoing Dependencies' table.

        Expected columns (at least 4):
          This Repo | Depends On | Target Domain | Type | ...

        Returns dict of {target_domain: row_count}.
        """
        counts: Dict[str, int] = defaultdict(int)
        for cells in DepMapMCPParser._iter_table_rows(
            content,
            "### Outgoing Dependencies",
            _OUTGOING_MIN_COLS,
            _OUTGOING_HEADER_SENTINEL,
        ):
            target = cells[_COL_OUTGOING_TARGET_DOMAIN]
            if target:
                counts[target] += 1
        return dict(counts)

    # ------------------------------------------------------------------
    # Domain lookup and file I/O helpers (S2)
    # ------------------------------------------------------------------

    @staticmethod
    def _lookup_domain_entry(
        domains: List[Dict[str, Any]], domain_name: str
    ) -> Optional[Dict[str, Any]]:
        """Return the first domain dict whose 'name' equals domain_name, or None."""
        for d in domains:
            if isinstance(d, dict) and d.get("name") == domain_name:
                return d
        return None

    @staticmethod
    def _read_domain_md_content(
        md_file: Path,
    ) -> Tuple[str, Optional[Dict[str, str]]]:
        """Read a domain .md file and return its content.

        Returns:
            (content, None) on success.
            ("", anomaly_dict) on any failure (missing file or read error),
            after logging a warning for every failure path.
        """
        if not md_file.exists():
            logger.warning("get_domain_summary: .md file not found: %s", md_file)
            return "", {"file": str(md_file), "error": "file not found"}
        try:
            return md_file.read_text(encoding="utf-8"), None
        except Exception as exc:
            logger.warning("get_domain_summary: failed to read %s: %s", md_file, exc)
            return "", {"file": str(md_file), "error": str(exc)}

    # ------------------------------------------------------------------
    # Summary builder helpers (S2)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_section_has_table(content: str, section_heading: str) -> None:
        """Raise ValueError when a section heading is present but has no table rows.

        A section is considered "present" when the exact heading string appears
        in the content.  A table row is any line that starts and ends with '|'.
        If the section is absent entirely this is a no-op (the section is
        optional).  If it is present but contains only non-table text the
        caller should record an anomaly — so we raise to make that explicit.

        Args:
            content: Full markdown file content.
            section_heading: Exact heading line to search for.

        Raises:
            ValueError: section heading found but no pipe-delimited table rows
                        follow before the next heading of the same or higher level.
        """
        section_level = len(section_heading) - len(section_heading.lstrip("#"))
        in_section = False
        found_table_row = False

        for line in content.splitlines():
            stripped = line.strip()
            if stripped == section_heading:
                in_section = True
                continue
            if in_section and stripped.startswith("#"):
                lvl = len(stripped) - len(stripped.lstrip("#"))
                if lvl > 0 and stripped[lvl : lvl + 1] == " " and lvl <= section_level:
                    break
            if not in_section:
                continue
            if stripped.startswith("|") and stripped.endswith("|"):
                found_table_row = True
                break

        if in_section and not found_table_row:
            raise ValueError(
                f"section '{section_heading}' is present but contains no table rows"
            )

    @staticmethod
    def _build_name_description(
        content: str,
        md_file: Path,
        fallback_name: str,
        fallback_description: str = "",
    ) -> Tuple[str, str, Optional[Dict[str, str]]]:
        """Parse name and description from YAML frontmatter in the .md file.

        A file that opens with '---' but has no closing '---' delimiter is
        treated as corrupt frontmatter (returns anomaly), not as "no frontmatter".

        When the frontmatter parses successfully but lacks a 'name' or
        'description' key, the caller-supplied fallback values are used.  Key
        presence (not truthiness) determines whether the file's own value is
        used, so an explicit empty string in frontmatter is preserved.

        Returns:
            (name, description, None) on successful parse.
            ("", "", anomaly_dict) on frontmatter parse error, after logging.
            (fallback_name, fallback_description, None) when content has no
            '---' opener at all.
        """
        if not content:
            return fallback_name, fallback_description, None
        if not content.startswith("---"):
            return fallback_name, fallback_description, None
        try:
            parts = content.split("---", 2)
            if len(parts) < 3:
                raise ValueError("frontmatter block opened with '---' but never closed")
            # yaml.safe_load: PyYAML stdlib-adjacent primitive; returns None for empty YAML, dict for mappings (verified: python-yaml.org/wiki/PyYAMLDocumentation).
            fm = yaml.safe_load(parts[1]) or {}
            name = fm["name"] if "name" in fm else fallback_name
            description = (
                fm["description"] if "description" in fm else fallback_description
            )
            return name, description, None
        except Exception as exc:
            logger.warning(
                "get_domain_summary: failed to parse frontmatter in %s: %s",
                md_file,
                exc,
            )
            return (
                "",
                "",
                {
                    "file": str(md_file),
                    "error": f"frontmatter: {exc}",
                },
            )

    @staticmethod
    def _build_participating_repos(
        content: str,
        md_file: Path,
    ) -> Tuple[List[Dict[str, str]], Optional[Dict[str, str]]]:
        """Extract participating_repos from the Repository Roles table.

        Calls _validate_section_has_table first so that a present-but-empty
        section raises ValueError and the anomaly error string carries the
        "participating_repos:" prefix for caller identification.

        Returns:
            ([{repo, role}, ...], None) on success.
            ([], anomaly_dict) on parse error, after logging a warning.
            ([], None) when content is empty (file was not readable).
        """
        if not content:
            return [], None
        try:
            DepMapMCPParser._validate_section_has_table(content, "## Repository Roles")
            roles_map = DepMapMCPParser._parse_roles_table(content)
            return [{"repo": r, "role": role} for r, role in roles_map.items()], None
        except Exception as exc:
            logger.warning(
                "get_domain_summary: failed to parse roles table in %s: %s",
                md_file,
                exc,
            )
            return [], {
                "file": str(md_file),
                "error": f"participating_repos: {exc}",
            }

    @staticmethod
    def _build_cross_domain_connections(
        content: str,
        md_file: Path,
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, str]]]:
        """Extract cross_domain_connections from the Outgoing Dependencies table.

        Calls _validate_section_has_table first so that a present-but-empty
        section raises ValueError and the anomaly error string carries the
        "cross_domain_connections:" prefix for caller identification.

        Returns:
            ([{target_domain, dependency_count}, ...], None) on success.
            ([], anomaly_dict) on parse error, after logging a warning.
            ([], None) when content is empty (file was not readable).
        """
        if not content:
            return [], None
        try:
            DepMapMCPParser._validate_section_has_table(
                content, "### Outgoing Dependencies"
            )
            counts = DepMapMCPParser._parse_outgoing_table(content)
            return [
                {"target_domain": t, "dependency_count": c} for t, c in counts.items()
            ], None
        except Exception as exc:
            logger.warning(
                "get_domain_summary: failed to parse outgoing table in %s: %s",
                md_file,
                exc,
            )
            return [], {
                "file": str(md_file),
                "error": f"cross_domain_connections: {exc}",
            }

    @staticmethod
    def _parse_last_analyzed(raw: str) -> datetime:
        """Parse a last_analyzed ISO-8601 string to a UTC-normalized datetime.

        Accepts timezone-aware ISO-8601 strings (``2026-04-18T12:00:00+00:00``)
        and ``Z``-suffixed forms (converted to ``+00:00`` before parsing).
        Result is always UTC via ``astimezone(timezone.utc)``.

        Raises:
            ValueError: when ``fromisoformat`` cannot parse the string.
        """
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(normalized)
        return dt.astimezone(timezone.utc)

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
        # yaml.safe_load: PyYAML stdlib-adjacent primitive; returns None for empty YAML, dict for mappings (verified: python-yaml.org/wiki/PyYAMLDocumentation).
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
