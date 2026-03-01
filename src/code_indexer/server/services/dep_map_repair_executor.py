"""
DepMapRepairExecutor for Story #342.

Orchestrates surgical repair of dependency map anomalies detected by DepMapHealthDetector.

5-phase repair algorithm:
  Phase 1: Re-analyze broken domains via Claude CLI (expensive, optional)
  Phase 2: Remove orphan files (free)
  Phase 3: Reconcile _domains.json to match disk state (free)
  Phase 4: Regenerate _index.md programmatically (free)
  Phase 5: Re-validate via health detector

Anomaly types handled:
  - missing_domain_file  -> Phase 1 (re-analyze)
  - zero_char_domain     -> Phase 1 (re-analyze)
  - undersized_domain    -> Phase 1 (re-analyze)
  - incomplete_domain    -> Phase 1 (re-analyze)
  - malformed_domain     -> Phase 1 (re-analyze)
  - orphan_domain_file   -> Phase 2 (remove)
  - domain_count_mismatch -> Phase 3 (reconcile JSON)
  - missing_index        -> Phase 4 (regenerate)
  - stale_index          -> Phase 4 (regenerate)
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from code_indexer.server.services.dep_map_file_utils import (
    load_domains_json as _load_domains_json_util,
)
from code_indexer.server.services.dep_map_health_detector import (
    REPAIRABLE_ANOMALY_TYPES,
    DepMapHealthDetector,
    HealthReport,
)
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator

logger = logging.getLogger(__name__)


@dataclass
class RepairResult:
    """Structured result of a repair run."""

    status: str  # "completed", "nothing_to_repair", "partial", "failed"
    fixed: List[str] = field(default_factory=list)  # descriptions of what was fixed
    errors: List[str] = field(default_factory=list)  # descriptions of what failed
    final_health_status: str = "unknown"  # health status after Phase 5 re-validation
    anomalies_before: int = 0
    anomalies_after: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "status": self.status,
            "fixed": self.fixed,
            "errors": self.errors,
            "final_health_status": self.final_health_status,
            "anomalies_before": self.anomalies_before,
            "anomalies_after": self.anomalies_after,
        }


class DepMapRepairExecutor:
    """
    Orchestrates repair of dependency map anomalies detected by DepMapHealthDetector.

    Constructor args:
        health_detector: DepMapHealthDetector instance (real, no mocking)
        index_regenerator: IndexRegenerator instance (real, no mocking)
        domain_analyzer: Optional callable for Claude CLI per-domain analysis.
            Signature: (output_dir: Path, domain: Dict, domain_list: List[Dict],
                        repo_list: List[Dict]) -> bool
            Returns True if domain was successfully re-analyzed (file has >0 chars).
            If None, Phase 1 (Claude CLI) is skipped -- free-fix phases still run.
        journal_callback: Optional callable for logging progress.
            Signature: (message: str) -> None
    """

    MAX_DOMAIN_RETRIES = 3

    def __init__(
        self,
        health_detector: DepMapHealthDetector,
        index_regenerator: IndexRegenerator,
        domain_analyzer: Optional[Callable] = None,
        journal_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._health_detector = health_detector
        self._index_regenerator = index_regenerator
        self._domain_analyzer = domain_analyzer
        self._journal_callback = journal_callback

    def execute(self, output_dir: Path, health_report: HealthReport) -> RepairResult:
        """
        Execute repair based on health report anomalies.

        Phase 1: Re-analyze broken domains (Claude CLI -- expensive, optional)
        Phase 2: Remove orphan files (free)
        Phase 3: Reconcile _domains.json (free)
        Phase 4: Regenerate _index.md (free -- programmatic)
        Phase 5: Re-validate via health detector

        Returns:
            RepairResult with status, fixed/error lists, and post-repair health status.
        """
        output_dir = Path(output_dir)

        if health_report.is_healthy:
            return RepairResult(
                status="nothing_to_repair",
                final_health_status="healthy",
                anomalies_before=0,
                anomalies_after=0,
            )

        anomalies_before = len(health_report.anomalies)
        fixed: List[str] = []
        errors: List[str] = []

        self._log(
            f"Repair started: {anomalies_before} anomalies detected "
            f"(status={health_report.status})"
        )

        # Phase 1: Re-analyze broken domains (EXPENSIVE -- Claude CLI)
        self._run_phase1(output_dir, health_report, fixed, errors)

        # Phase 2: Remove orphan files (FREE)
        self._run_phase2(output_dir, health_report, fixed, errors)

        # Phase 3: Reconcile _domains.json (FREE)
        self._run_phase3(output_dir, health_report, fixed, errors)

        # Phase 4: Regenerate _index.md (FREE -- programmatic)
        self._run_phase4(output_dir, health_report, fixed, errors)

        # Phase 5: Re-validate
        new_report = self._health_detector.detect(output_dir)
        anomalies_after = len(new_report.anomalies)

        self._log(
            f"Repair complete: {len(fixed)} fixed, {len(errors)} errors, "
            f"final status={new_report.status}"
        )

        # Determine result status
        if len(errors) == 0 and new_report.is_healthy:
            status = "completed"
        elif len(fixed) > 0:
            status = "partial"
        else:
            status = "failed"

        return RepairResult(
            status=status,
            fixed=fixed,
            errors=errors,
            final_health_status=new_report.status,
            anomalies_before=anomalies_before,
            anomalies_after=anomalies_after,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 1: Re-analyze broken domains
    # ─────────────────────────────────────────────────────────────────────────

    def _run_phase1(
        self,
        output_dir: Path,
        health_report: HealthReport,
        fixed: List[str],
        errors: List[str],
    ) -> None:
        """Phase 1: Re-analyze broken domains via domain_analyzer (Claude CLI)."""
        if self._domain_analyzer is None:
            return  # Phase 1 skipped when no analyzer provided

        broken_domains = [
            a
            for a in health_report.anomalies
            if a.type in REPAIRABLE_ANOMALY_TYPES and a.domain is not None
        ]

        if not broken_domains:
            return

        domain_list = self._load_domains_json(output_dir)

        for anomaly in broken_domains:
            domain_info = next(
                (d for d in domain_list if d.get("name") == anomaly.domain), None
            )
            if domain_info is None:
                errors.append(
                    f"Domain '{anomaly.domain}' not found in _domains.json"
                )
                continue

            self._log(f"Repairing domain: {anomaly.domain}")
            success = False

            for attempt in range(1, self.MAX_DOMAIN_RETRIES + 1):
                try:
                    # Bug B fix: delete broken file before analyzer runs,
                    # so Claude starts fresh rather than building on broken content
                    domain_file = output_dir / f"{anomaly.domain}.md"
                    if domain_file.exists():
                        domain_file.unlink()
                        self._log(f"Removed broken domain file: {anomaly.domain}")

                    self._domain_analyzer(output_dir, domain_info, domain_list, [])
                    domain_file = output_dir / f"{anomaly.domain}.md"
                    if domain_file.exists() and domain_file.stat().st_size > 0:
                        # Bug A fix: re-run health detector to verify anomaly is actually gone
                        post_report = self._health_detector.detect(output_dir)
                        anomaly_still_present = any(
                            a.type == anomaly.type and a.domain == anomaly.domain
                            for a in post_report.anomalies
                        )
                        if not anomaly_still_present:
                            fixed.append(f"repaired domain: {anomaly.domain}")
                            self._log(
                                f"Repaired: {anomaly.domain} "
                                f"({domain_file.stat().st_size} chars)"
                            )
                            success = True
                            break
                        else:
                            self._log(
                                f"Retry {attempt}/{self.MAX_DOMAIN_RETRIES} "
                                f"for {anomaly.domain}: anomaly still present after re-analysis"
                            )
                            continue
                except Exception as e:
                    self._log(
                        f"Retry {attempt}/{self.MAX_DOMAIN_RETRIES} "
                        f"for {anomaly.domain}: {e}"
                    )

            if not success:
                errors.append(
                    f"Failed to repair {anomaly.domain} after "
                    f"{self.MAX_DOMAIN_RETRIES} attempts"
                )
                self._log(f"FAILED: {anomaly.domain}")

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 2: Remove orphan files
    # ─────────────────────────────────────────────────────────────────────────

    def _run_phase2(
        self,
        output_dir: Path,
        health_report: HealthReport,
        fixed: List[str],
        errors: List[str],
    ) -> None:
        """Phase 2: Delete orphan .md files not tracked in _domains.json."""
        orphans = [a for a in health_report.anomalies if a.type == "orphan_domain_file"]

        for orphan in orphans:
            orphan_path = output_dir / orphan.file
            if orphan_path.exists():
                try:
                    orphan_path.unlink()
                    fixed.append(f"removed orphan: {orphan.file}")
                    self._log(f"Removed orphan: {orphan.file}")
                except OSError as e:
                    errors.append(f"Failed to remove orphan {orphan.file}: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 3: Reconcile _domains.json
    # ─────────────────────────────────────────────────────────────────────────

    def _run_phase3(
        self,
        output_dir: Path,
        health_report: HealthReport,
        fixed: List[str],
        errors: List[str],
    ) -> None:
        """Phase 3: Reconcile _domains.json to match domain files on disk."""
        mismatch = [
            a for a in health_report.anomalies if a.type == "domain_count_mismatch"
        ]
        if not mismatch:
            return

        try:
            self._reconcile_domains_json(output_dir)
            fixed.append("reconciled _domains.json")
            self._log("Reconciled _domains.json")
        except Exception as e:
            errors.append(f"Failed to reconcile _domains.json: {e}")

    def _reconcile_domains_json(self, output_dir: Path) -> None:
        """
        Reconcile _domains.json to match domain .md files present on disk.

        - Reads current _domains.json (or []).
        - Scans non-underscore .md files on disk.
        - Keeps entries whose .md file exists; drops entries without a file.
        - Adds minimal entries for .md files with no JSON entry.
        - Writes updated _domains.json.
        """
        old_list = self._load_domains_json(output_dir)

        # Index existing entries by name for fast lookup
        existing_by_name: Dict[str, Dict[str, Any]] = {
            d.get("name", ""): d for d in old_list if d.get("name")
        }

        # Scan domain .md files on disk
        md_files = [
            f for f in output_dir.glob("*.md") if not f.name.startswith("_")
        ]

        new_list: List[Dict[str, Any]] = []
        for md_file in sorted(md_files):
            stem = md_file.stem
            if stem in existing_by_name:
                # Keep existing entry with all its metadata
                new_list.append(existing_by_name[stem])
            else:
                # Create minimal entry for untracked .md file
                new_list.append({"name": stem})

        domains_file = output_dir / "_domains.json"
        domains_file.write_text(json.dumps(new_list, indent=2), encoding="utf-8")

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 4: Regenerate _index.md
    # ─────────────────────────────────────────────────────────────────────────

    def _run_phase4(
        self,
        output_dir: Path,
        health_report: HealthReport,
        fixed: List[str],
        errors: List[str],
    ) -> None:
        """Phase 4: Regenerate _index.md if missing, stale, or a domain was repaired."""
        needs_index_regen = any(
            a.type in ("missing_index", "stale_index") for a in health_report.anomalies
        ) or any("repaired domain:" in f for f in fixed)

        if not needs_index_regen:
            return

        try:
            self._index_regenerator.regenerate(output_dir)
            fixed.append("regenerated _index.md")
            self._log("Regenerated _index.md")
        except Exception as e:
            errors.append(f"Failed to regenerate _index.md: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _load_domains_json(self, output_dir: Path) -> List[Dict[str, Any]]:
        """Load _domains.json, returning empty list if missing or invalid."""
        return _load_domains_json_util(output_dir)

    def _log(self, message: str) -> None:
        """Log a message via journal_callback (if set) and Python logger."""
        logger.debug("[RepairExecutor] %s", message)
        if self._journal_callback is not None:
            try:
                self._journal_callback(message)
            except Exception as e:
                logger.warning("journal_callback raised: %s", e)
