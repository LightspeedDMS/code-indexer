"""
DepMapHealthDetector service for Story #342.

Inspects a dependency map output directory and produces a structured HealthReport
with all detected anomalies. Pure inspection -- no side effects, no writes.

Health Detection Algorithm (6 checks):
  Check 1: Missing or 0-char domain files (from _domains.json)
  Check 2: Orphan .md files not in _domains.json
  Check 3: _domains.json count vs .md file count mismatch
  Check 4: _index.md missing or stale (repo matrix comparison)
  Check 5: Domain .md files missing required sections
  Check 6: Golden repos not assigned to any domain (requires known_repos parameter)

Status escalation:
  critical     -- if any anomaly with type in (missing_domain_file, zero_char_domain)
  needs_repair -- if any other anomaly present
  healthy      -- no anomalies
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from code_indexer.server.services.dep_map_file_utils import (
    get_domain_md_files as _get_domain_md_files_util,
    has_yaml_frontmatter as _has_yaml_frontmatter_util,
    load_domains_json as _load_domains_json_util,
    parse_simple_yaml as _parse_simple_yaml_util,
    parse_yaml_frontmatter as _parse_yaml_frontmatter_util,
)

logger = logging.getLogger(__name__)

# Threshold below which a domain file is considered undersized
DOMAIN_SIZE_THRESHOLD = 1000

# Required markdown section headers in domain .md files
REQUIRED_SECTIONS = [
    "## Overview",
    "## Repository Roles",
]

# Anomaly types that escalate to critical status
CRITICAL_ANOMALY_TYPES = {"missing_domain_file", "zero_char_domain"}

# Anomaly types that require Claude CLI to fix (Phase 1 repair)
REPAIRABLE_ANOMALY_TYPES = {
    "missing_domain_file",
    "zero_char_domain",
    "undersized_domain",
    "incomplete_domain",
    "malformed_domain",
}


@dataclass
class Anomaly:
    """
    Represents a single detected anomaly in the dependency map output.

    Attributes:
        type: Anomaly type identifier string
        domain: Domain name (for domain-level anomalies)
        file: File name (for file-level anomalies like orphans)
        size: File size in chars (for undersized_domain anomaly)
        missing_repos: List of repos missing from index matrix (for stale_index)
        severity: Optional severity override (e.g. "high" for missing_index)
        detail: Optional additional detail string
    """

    type: str
    domain: Optional[str] = None
    file: Optional[str] = None
    size: Optional[int] = None
    missing_repos: Optional[List[str]] = None
    severity: Optional[str] = None
    detail: Optional[str] = None
    json_count: Optional[int] = None
    file_count: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        result: Dict[str, Any] = {"type": self.type}
        if self.domain is not None:
            result["domain"] = self.domain
        if self.file is not None:
            result["file"] = self.file
        if self.size is not None:
            result["size"] = self.size
        if self.missing_repos is not None:
            result["missing_repos"] = self.missing_repos
        if self.severity is not None:
            result["severity"] = self.severity
        if self.detail is not None:
            result["detail"] = self.detail
        if self.json_count is not None:
            result["json_count"] = self.json_count
        if self.file_count is not None:
            result["file_count"] = self.file_count
        return result


@dataclass
class HealthReport:
    """
    Structured result of a health detection run.

    Attributes:
        status: "healthy", "needs_repair", or "critical"
        anomalies: List of detected Anomaly objects
        repairable_count: Number of anomalies that can be auto-repaired
        output_dir: The directory that was inspected
    """

    status: str
    anomalies: List[Anomaly] = field(default_factory=list)
    repairable_count: int = 0
    output_dir: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "status": self.status,
            "anomalies": [a.to_dict() for a in self.anomalies],
            "repairable_count": self.repairable_count,
            "output_dir": str(self.output_dir) if self.output_dir else None,
        }

    @property
    def is_healthy(self) -> bool:
        """True when status is 'healthy'."""
        return self.status == "healthy"

    @property
    def needs_repair(self) -> bool:
        """True when anomalies exist (status is not 'healthy')."""
        return self.status != "healthy"


class DepMapHealthDetector:
    """
    Inspects a dependency map output directory and produces a HealthReport.

    Pure inspection service -- no side effects, no writes to disk.
    Call detect(output_dir) to perform all 5 health checks.
    """

    def detect(
        self,
        output_dir: Path,
        known_repos: Optional[Set[str]] = None,
    ) -> HealthReport:
        """
        Inspect output_dir and return a HealthReport with all detected anomalies.

        Args:
            output_dir: Path to the dependency map output directory
                        (e.g. cidx-meta/dependency-map/)
            known_repos: Optional set of golden repo names from the database.
                         When provided, Check 6 runs to flag repos not covered
                         by any domain in _domains.json. Pass None to skip Check 6
                         (backward-compatible default).

        Returns:
            HealthReport with status and list of Anomaly objects.
        """
        output_dir = Path(output_dir)

        # Guard: directory must exist and be non-empty
        if not output_dir.exists() or not output_dir.is_dir():
            return HealthReport(
                status="critical",
                anomalies=[Anomaly(type="missing_output_dir", detail=str(output_dir))],
                repairable_count=0,
                output_dir=output_dir,
            )

        # Check if directory is functionally empty (no _domains.json or .md files)
        domains_file = output_dir / "_domains.json"
        md_files = self._get_domain_md_files(output_dir)
        if not domains_file.exists() and not md_files:
            return HealthReport(
                status="critical",
                anomalies=[Anomaly(type="empty_output_dir", detail=str(output_dir))],
                repairable_count=0,
                output_dir=output_dir,
            )

        anomalies: List[Anomaly] = []

        # Load domain list from _domains.json (or empty list if missing)
        domain_list = self._load_domains_json(output_dir)

        # Check 1: Missing or 0-char or undersized domain files
        anomalies.extend(self._check_domain_files(output_dir, domain_list))

        # Check 2: Orphan .md files not in _domains.json
        anomalies.extend(self._check_orphan_files(output_dir, domain_list, md_files))

        # Check 3: _domains.json count vs .md file count mismatch
        anomalies.extend(self._check_domain_count_mismatch(domain_list, md_files))

        # Check 4: _index.md missing or stale
        anomalies.extend(self._check_index_md(output_dir, md_files))

        # Check 5: Domain .md files missing required sections
        anomalies.extend(self._check_domain_structure(output_dir, domain_list))

        # Check 6: Repos not covered by any domain (requires known_repos)
        if known_repos is not None:
            anomalies.extend(self._check_uncovered_repos(domain_list, known_repos))

        # Determine overall status
        status = self._compute_status(anomalies)

        # Count repairable anomalies
        repairable_count = sum(
            1 for a in anomalies if a.type in REPAIRABLE_ANOMALY_TYPES
        )

        return HealthReport(
            status=status,
            anomalies=anomalies,
            repairable_count=repairable_count,
            output_dir=output_dir,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Check 1: Domain file existence, size, content
    # ─────────────────────────────────────────────────────────────────────────

    def _check_domain_files(
        self, output_dir: Path, domain_list: List[Dict[str, Any]]
    ) -> List[Anomaly]:
        """Check each domain in _domains.json for missing, zero-char, or undersized files."""
        anomalies = []
        for domain in domain_list:
            name = domain.get("name", "")
            if not name:
                continue
            domain_file = output_dir / f"{name}.md"
            if not domain_file.exists():
                anomalies.append(Anomaly(type="missing_domain_file", domain=name))
            else:
                size = domain_file.stat().st_size
                if size == 0:
                    anomalies.append(Anomaly(type="zero_char_domain", domain=name))
                elif size < DOMAIN_SIZE_THRESHOLD:
                    anomalies.append(
                        Anomaly(type="undersized_domain", domain=name, size=size)
                    )
        return anomalies

    # ─────────────────────────────────────────────────────────────────────────
    # Check 2: Orphan .md files
    # ─────────────────────────────────────────────────────────────────────────

    def _check_orphan_files(
        self,
        output_dir: Path,
        domain_list: List[Dict[str, Any]],
        md_files: List[Path],
    ) -> List[Anomaly]:
        """Check for .md files on disk that are not tracked in _domains.json."""
        domain_names: Set[str] = {d.get("name", "") for d in domain_list}
        anomalies = []
        for md_file in md_files:
            if md_file.stem not in domain_names:
                anomalies.append(
                    Anomaly(type="orphan_domain_file", file=md_file.name)
                )
        return anomalies

    # ─────────────────────────────────────────────────────────────────────────
    # Check 3: Count mismatch
    # ─────────────────────────────────────────────────────────────────────────

    def _check_domain_count_mismatch(
        self,
        domain_list: List[Dict[str, Any]],
        md_files: List[Path],
    ) -> List[Anomaly]:
        """Check if _domains.json count matches on-disk .md file count."""
        json_count = len(domain_list)
        file_count = len(md_files)
        if json_count != file_count:
            return [
                Anomaly(
                    type="domain_count_mismatch",
                    domain=None,
                    size=None,
                    detail=f"json={json_count} files={file_count}",
                    json_count=json_count,
                    file_count=file_count,
                )
            ]
        return []

    # ─────────────────────────────────────────────────────────────────────────
    # Check 4: _index.md presence and staleness
    # ─────────────────────────────────────────────────────────────────────────

    def _check_index_md(
        self, output_dir: Path, md_files: List[Path]
    ) -> List[Anomaly]:
        """Check for missing or stale _index.md."""
        index_file = output_dir / "_index.md"
        if not index_file.exists():
            return [Anomaly(type="missing_index", severity="high")]

        # Check staleness: compare repos in matrix vs repos in domain frontmatter
        try:
            index_content = index_file.read_text(encoding="utf-8")
            matrix_repos = self._parse_matrix_repos(index_content)
            actual_repos = self._collect_repos_from_frontmatter(md_files)

            missing_repos = sorted(actual_repos - matrix_repos)
            if missing_repos:
                return [Anomaly(type="stale_index", missing_repos=missing_repos)]
        except Exception as e:
            logger.warning("Failed to check _index.md staleness: %s", e)

        return []

    def _parse_matrix_repos(self, index_content: str) -> Set[str]:
        """
        Parse repo names from the Repo-to-Domain Matrix table in _index.md.

        Looks for rows in the matrix table of the form:
            | repo-name | domain-name |
        """
        repos: Set[str] = set()
        in_matrix = False
        for line in index_content.splitlines():
            stripped = line.strip()
            if "Repo-to-Domain Matrix" in stripped:
                in_matrix = True
                continue
            if in_matrix:
                if stripped.startswith("##") and "Repo-to-Domain" not in stripped:
                    # Next section header -- stop
                    break
                if stripped.startswith("| ") and not stripped.startswith("|---"):
                    parts = [p.strip() for p in stripped.split("|")]
                    parts = [p for p in parts if p]
                    if len(parts) >= 2:
                        header_words = {"Repository", "Domain"}
                        if parts[0] not in header_words:
                            repos.add(parts[0])
        return repos

    def _collect_repos_from_frontmatter(self, md_files: List[Path]) -> Set[str]:
        """
        Collect all repos referenced in participating_repos from domain .md frontmatter.
        """
        repos: Set[str] = set()
        for md_file in md_files:
            try:
                content = md_file.read_text(encoding="utf-8")
                frontmatter = self._parse_yaml_frontmatter(content)
                if frontmatter:
                    participating = frontmatter.get("participating_repos", [])
                    if isinstance(participating, list):
                        repos.update(str(r) for r in participating)
            except Exception as e:
                logger.warning("Failed to parse frontmatter from %s: %s", md_file, e)
        return repos

    # ─────────────────────────────────────────────────────────────────────────
    # Check 5: Domain structure (frontmatter, required sections)
    # ─────────────────────────────────────────────────────────────────────────

    def _check_domain_structure(
        self, output_dir: Path, domain_list: List[Dict[str, Any]]
    ) -> List[Anomaly]:
        """Check domain .md files for YAML frontmatter and required sections."""
        anomalies = []
        domain_names = {d.get("name", "") for d in domain_list}

        for name in domain_names:
            if not name:
                continue
            domain_file = output_dir / f"{name}.md"
            if not domain_file.exists():
                continue  # Already caught by Check 1

            try:
                content = domain_file.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("Failed to read domain file %s: %s", domain_file, e)
                continue

            # Skip zero-char and undersized files -- already flagged in Check 1
            if len(content) == 0 or len(content) < DOMAIN_SIZE_THRESHOLD:
                continue

            # Check for YAML frontmatter
            if not self._has_yaml_frontmatter(content):
                anomalies.append(
                    Anomaly(
                        type="malformed_domain",
                        domain=name,
                        detail="missing frontmatter",
                    )
                )
                continue  # No point checking sections if frontmatter is missing

            # Check for required sections
            if not self._has_required_sections(content):
                anomalies.append(Anomaly(type="incomplete_domain", domain=name))

        return anomalies

    # ─────────────────────────────────────────────────────────────────────────
    # Check 6: Repos not covered by any domain
    # ─────────────────────────────────────────────────────────────────────────

    def _check_uncovered_repos(
        self,
        domain_list: List[Dict[str, Any]],
        known_repos: Set[str],
    ) -> List[Anomaly]:
        """Check for golden repos not assigned to any domain in _domains.json.

        cidx-meta is always excluded — it is the meta repo, not a domain participant.
        """
        covered_repos: Set[str] = set()
        for domain in domain_list:
            participating = domain.get("participating_repos", [])
            if isinstance(participating, list):
                covered_repos.update(str(r) for r in participating)

        uncovered = sorted(known_repos - covered_repos - {"cidx-meta"})
        if uncovered:
            return [
                Anomaly(
                    type="uncovered_repo",
                    missing_repos=uncovered,
                    detail=f"{len(uncovered)} repo(s) not in any domain",
                )
            ]
        return []

    # ─────────────────────────────────────────────────────────────────────────
    # Status computation
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_status(self, anomalies: List[Anomaly]) -> str:
        """Compute overall status from anomaly list."""
        if not anomalies:
            return "healthy"
        for anomaly in anomalies:
            if anomaly.type in CRITICAL_ANOMALY_TYPES:
                return "critical"
        return "needs_repair"

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers -- delegating to dep_map_file_utils shared module (Story #342 M1)
    # ─────────────────────────────────────────────────────────────────────────

    def _get_domain_md_files(self, output_dir: Path) -> List[Path]:
        """Return list of .md files in output_dir that are NOT underscore-prefixed."""
        return _get_domain_md_files_util(output_dir)

    def _load_domains_json(self, output_dir: Path) -> List[Dict[str, Any]]:
        """Load _domains.json, returning empty list if missing or invalid."""
        return _load_domains_json_util(output_dir)

    def _has_yaml_frontmatter(self, content: str) -> bool:
        """Check if content starts with a YAML frontmatter block (--- ... ---)."""
        return _has_yaml_frontmatter_util(content)

    def _has_required_sections(self, content: str) -> bool:
        """
        Check if domain .md content has all required section headers.

        Required sections: Overview, Repository Roles
        """
        for section in REQUIRED_SECTIONS:
            if section not in content:
                return False
        return True

    def _parse_yaml_frontmatter(self, content: str) -> Optional[Dict[str, Any]]:
        """Parse YAML frontmatter block from markdown content."""
        return _parse_yaml_frontmatter_util(content)

    def _parse_simple_yaml(self, lines: List[str]) -> Dict[str, Any]:
        """Parse a simplified YAML structure from frontmatter lines."""
        return _parse_simple_yaml_util(lines)
