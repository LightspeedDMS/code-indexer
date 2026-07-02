"""
DepMapRepairExecutor for Story #342.

Orchestrates surgical repair of dependency map anomalies detected by DepMapHealthDetector.

6-phase repair algorithm:
  Phase 0: Discover uncovered repos via domain discovery (Story #716)
  Phase 1: Re-analyze broken domains via Claude CLI (expensive, optional)
  Phase 1.5: Remove stale repo references from domains (Story #717)
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
  - uncovered_repo       -> Phase 0 (discover)
  - stale_participating_repo -> Phase 1.5 (cleanup)
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple, cast

if TYPE_CHECKING:
    from code_indexer.server.services.dep_map_parser_hygiene import AnomalyEntry

from code_indexer.server.services.dep_map_repair_phase37 import (
    Action,  # noqa: F401 — re-exported for test backward compat
    JournalEntry,  # noqa: F401 — re-exported for test backward compat
    RepairJournal,  # noqa: F401 — re-exported for test backward compat
    acquire_domain_lock,
    atomic_write_text,
    body_byte_offset,
    build_and_append_journal_entry,
    emit_repos_lines,
    reemit_frontmatter_from_domain_info,
    remove_self_loop_rows,
    resolve_self_loop_md_path,
    run_phase37,
)
from code_indexer.server.services.dep_map_repair_malformed_yaml import (
    run_malformed_yaml_repairs,
)
from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser
from code_indexer.server.services.dep_map_parser_hygiene import AnomalyType
from code_indexer.server.services.dep_map_repair_bidirectional import (
    audit_one_bidirectional_mismatch,
)

from code_indexer.global_repos.lifecycle_batch_runner import LifecycleBatchRunner
from code_indexer.global_repos.yaml_emitter_utils import yaml_quote_if_unsafe
from code_indexer.server.services.dep_map_file_utils import (
    load_domains_json as _load_domains_json_util,
    parse_yaml_frontmatter as _parse_yaml_frontmatter_util,
)
from code_indexer.server.services.dep_map_health_detector import (
    REPAIRABLE_ANOMALY_TYPES,
    DepMapHealthDetector,
    HealthReport,
)
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator

logger = logging.getLogger(__name__)

# Sentinel for branch-separated progress events. The numeric percent is -1
# so scalar dashboards cannot accidentally render it; consumers MUST parse
# the JSON payload in ``info`` to extract dep_map / lifecycle status
# (Story #876 Phase B-2 D2).
_BRANCH_PROGRESS_SENTINEL: int = -1

# Valid values for per-anomaly-type enablement config flags (Story #920).
# NO helper function for validation — inline only (MESSI Rule 3 KISS, gemini C6).
_VALID_PER_TYPE_VALUES: frozenset = frozenset({"disabled", "dry_run", "enabled"})


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


@dataclass(frozen=True)
class DryRunReport:
    """Verdict-only summary returned by _run_phase37(dry_run=True).

    All fields are plain Python types so json.dumps(asdict(report), default=str)
    round-trips without error (AC2/AC6).
    errors: non-empty when the dry-run could not proceed (e.g., missing output dir).
    """

    mode: str  # always "dry_run"
    timestamp: str
    total_anomalies: int
    per_type_counts: Dict[str, int]
    per_verdict_counts: Dict[str, int]
    per_action_counts: Dict[str, int]
    would_be_writes: List[Tuple[str, str]]  # (file_path, operation)
    skipped: List[Tuple[str, str]]  # (anomaly_type, reason)
    errors: List[str]  # visible error messages; non-empty on failure


def is_effective_dry_run(invocation_dry_run: bool, per_type_flag: str) -> bool:
    """Composition rule: invocation-level dry_run OR per-type flag == 'dry_run'.

    invocation_dry_run=True takes precedence over any per-type flag (incl. 'enabled').
    Used by both Story 7 (#919) and Story 8 (#920).
    """
    return invocation_dry_run or (per_type_flag == "dry_run")


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
        discovery_callback: Optional[Callable] = None,
        journal_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        lifecycle_invoker: Optional[Callable] = None,
        golden_repos_dir: Optional[Path] = None,
        enable_graph_channel_repair: bool = True,
        repo_path_resolver: Optional[Callable[[str], str]] = None,
        invoke_llm_fn: Optional[Callable] = None,
        graph_repair_self_loop: Optional[str] = None,
        graph_repair_malformed_yaml: Optional[str] = None,
        graph_repair_garbage_domain: Optional[str] = None,
        graph_repair_bidirectional_mismatch: Optional[str] = None,
    ) -> None:
        self._health_detector = health_detector
        self._index_regenerator = index_regenerator
        self._domain_analyzer = domain_analyzer
        self._discovery_callback = discovery_callback
        self._journal_callback = journal_callback
        self._progress_callback = progress_callback
        self._lifecycle_invoker = lifecycle_invoker
        self._golden_repos_dir = golden_repos_dir
        self._enable_graph_channel_repair: bool = enable_graph_channel_repair
        self._repo_path_resolver: Optional[Callable[[str], str]] = repo_path_resolver
        self._invoke_llm_fn: Optional[Callable] = invoke_llm_fn
        logger.debug(
            "[RepairExecutor] enable_graph_channel_repair=%s",
            self._enable_graph_channel_repair,
        )
        # Per-anomaly-type enablement flags (Story #920).
        # Step 1: declare typed attributes with default "dry_run" (mypy-visible).
        self._graph_repair_self_loop: str = "dry_run"
        self._graph_repair_malformed_yaml: str = "dry_run"
        self._graph_repair_garbage_domain: str = "dry_run"
        self._graph_repair_bidirectional_mismatch: str = "dry_run"
        # Step 2: validate all inputs before any assignment (fail-fast, no partial state).
        for _param_name, _raw_value in (
            ("graph_repair_self_loop", graph_repair_self_loop),
            ("graph_repair_malformed_yaml", graph_repair_malformed_yaml),
            ("graph_repair_garbage_domain", graph_repair_garbage_domain),
            (
                "graph_repair_bidirectional_mismatch",
                graph_repair_bidirectional_mismatch,
            ),
        ):
            if _raw_value is not None and _raw_value not in _VALID_PER_TYPE_VALUES:
                raise ValueError(
                    f"{_param_name}: {_raw_value!r} not in {sorted(_VALID_PER_TYPE_VALUES)}"
                )
        # Step 3: direct assignment — None inputs keep the "dry_run" default.
        if graph_repair_self_loop is not None:
            self._graph_repair_self_loop = graph_repair_self_loop
        if graph_repair_malformed_yaml is not None:
            self._graph_repair_malformed_yaml = graph_repair_malformed_yaml
        if graph_repair_garbage_domain is not None:
            self._graph_repair_garbage_domain = graph_repair_garbage_domain
        if graph_repair_bidirectional_mismatch is not None:
            self._graph_repair_bidirectional_mismatch = (
                graph_repair_bidirectional_mismatch
            )
        self._log(
            f"Phase 3.7 per-type flags: self_loop={self._graph_repair_self_loop}, "
            f"malformed_yaml={self._graph_repair_malformed_yaml}, "
            f"garbage_domain={self._graph_repair_garbage_domain}, "
            f"bidirectional_mismatch={self._graph_repair_bidirectional_mismatch}"
        )

    def execute(
        self,
        output_dir: Path,
        health_report: HealthReport,
        parent_job_id: Optional[str] = None,
    ) -> RepairResult:
        """Fork/join Branch A (dep_map) and Branch B (lifecycle).  Branch B
        fires only when lifecycle is non-empty AND both lifecycle_invoker
        and golden_repos_dir are wired; its exceptions land in errors
        without swallowing Branch A."""
        output_dir = Path(output_dir)
        if health_report.is_healthy:
            self._report_progress(100, "Nothing to repair")
            return RepairResult(
                status="nothing_to_repair",
                final_health_status="healthy",
                anomalies_before=0,
                anomalies_after=0,
            )
        anomalies_before = len(health_report.anomalies)
        fixed: List[str] = []
        errors: List[str] = []
        lifecycle_active = bool(
            health_report.lifecycle
            and self._lifecycle_invoker is not None
            and self._golden_repos_dir is not None
        )
        if lifecycle_active:
            self._report_progress(
                _BRANCH_PROGRESS_SENTINEL,
                json.dumps({"dep_map": "running", "lifecycle": "running"}),
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                fa = pool.submit(
                    self._run_branch_a_dep_map, output_dir, health_report, fixed, errors
                )
                fb = pool.submit(
                    self._run_branch_b_lifecycle,
                    list(health_report.lifecycle),
                    parent_job_id,
                )
                new_report = fa.result()
                try:
                    fb.result()
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"lifecycle: {exc}")
        else:
            new_report = self._run_branch_a_dep_map(
                output_dir, health_report, fixed, errors
            )
        self._log(
            f"Repair complete: {len(fixed)} fixed, {len(errors)} errors, "
            f"final status={new_report.status}"
        )
        self._report_progress(100, "Repair complete")
        status = (
            "completed"
            if not errors and new_report.is_healthy
            else "partial"
            if fixed
            else "failed"
        )
        return RepairResult(
            status=status,
            fixed=fixed,
            errors=errors,
            final_health_status=new_report.status,
            anomalies_before=anomalies_before,
            anomalies_after=len(new_report.anomalies),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Fork/join branches (Story #876 Phase B-2 D2)
    # ─────────────────────────────────────────────────────────────────────────

    def _run_branch_a_dep_map(
        self,
        output_dir: Path,
        health_report: HealthReport,
        fixed: List[str],
        errors: List[str],
    ) -> HealthReport:
        """Branch A: existing dep_map Phase 0 -> 5 pipeline (with start log)."""
        self._log(
            f"Repair started: {len(health_report.anomalies)} anomalies detected "
            f"(status={health_report.status})"
        )
        self._report_progress(5, "Phase 0: Discovering uncovered repos")
        self._run_phase0(output_dir, health_report, fixed, errors)
        self._report_progress(10, "Phase 1: Re-analyzing broken domains")
        self._run_phase1(output_dir, health_report, fixed, errors)
        self._report_progress(60, "Phase 1.5: Cleaning stale repo references")
        self._run_phase15(output_dir, health_report, fixed, errors)
        self._report_progress(65, "Phase 2: Removing orphan files")
        self._run_phase2(output_dir, health_report, fixed, errors)
        self._report_progress(70, "Phase 3: Reconciling domains.json")
        self._run_phase3(output_dir, health_report, fixed, errors)
        self._report_progress(75, "Phase 3.5: Backfilling JSON metadata from markdown")
        self._run_phase35(output_dir, fixed, errors)
        self._report_progress(78, "Phase 3.7: Repairing graph-channel anomalies")
        self._run_phase37(output_dir, fixed, errors)
        self._report_progress(80, "Phase 4: Regenerating index")
        self._run_phase4(output_dir, health_report, fixed, errors)
        self._report_progress(90, "Phase 5: Validating repair")
        return self._health_detector.detect(output_dir)

    def _run_branch_b_lifecycle(
        self,
        aliases: List[str],
        parent_job_id: Optional[str],
    ) -> None:
        """Branch B: LifecycleBatchRunner.run(aliases, parent_job_id)."""
        runner = LifecycleBatchRunner(
            golden_repos_dir=self._golden_repos_dir,
            job_tracker=None,
            refresh_scheduler=None,
            debouncer=None,
            claude_cli_invoker=self._lifecycle_invoker,
        )
        runner.run(aliases, parent_job_id=parent_job_id)

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 0: Discover uncovered repos (Story #716)
    # ─────────────────────────────────────────────────────────────────────────

    def _run_phase0(
        self,
        output_dir: Path,
        health_report: HealthReport,
        fixed: List[str],
        errors: List[str],
    ) -> None:
        """Phase 0: Discover and assign uncovered repos via domain discovery."""
        if self._discovery_callback is None:
            return  # Phase 0 skipped when no discovery callback provided

        uncovered_anomalies = [
            a
            for a in health_report.anomalies
            if a.type == "uncovered_repo" and a.missing_repos
        ]

        if not uncovered_anomalies:
            return

        # Collect all uncovered repo aliases
        uncovered_aliases: List[str] = []
        for anomaly in uncovered_anomalies:
            if anomaly.missing_repos:
                uncovered_aliases.extend(anomaly.missing_repos)

        if not uncovered_aliases:
            return

        self._log(
            f"Phase 0: Discovering domains for {len(uncovered_aliases)} "
            f"uncovered repos: {sorted(uncovered_aliases)}"
        )

        try:
            discovered_domains = self._discovery_callback(output_dir, uncovered_aliases)
            if discovered_domains:
                fixed.append(
                    f"discovered domains for {len(uncovered_aliases)} "
                    f"uncovered repos: {sorted(discovered_domains)}"
                )
                self._log(
                    f"Phase 0: Assigned uncovered repos to "
                    f"{len(discovered_domains)} domains"
                )
            else:
                self._log("Phase 0: No domain assignments returned")
        except Exception as e:
            errors.append(f"Phase 0 discovery failed: {e}")
            self._log(f"Phase 0 FAILED: {e}")

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
                errors.append(f"Domain '{anomaly.domain}' not found in _domains.json")
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
    # Phase 1.5: Stale repo cleanup (Story #717)
    # ─────────────────────────────────────────────────────────────────────────

    def _run_phase15(
        self,
        output_dir: Path,
        health_report: HealthReport,
        fixed: List[str],
        errors: List[str],
    ) -> None:
        """Phase 1.5: Remove stale repo references from _domains.json and .md files."""
        stale_aliases = self._collect_stale_aliases(health_report)
        if not stale_aliases:
            return

        self._log(
            f"Phase 1.5: Removing {len(stale_aliases)} stale repo references: "
            f"{sorted(stale_aliases)}"
        )

        domain_list = self._load_domains_json(output_dir)
        if not domain_list:
            return

        changed = self._remove_stale_aliases_from_domains(
            domain_list, stale_aliases, output_dir
        )
        if not changed:
            return

        if not self._persist_domains_and_regenerate(output_dir, domain_list, errors):
            return

        fixed.append(f"removed stale repo references: {sorted(stale_aliases)}")

    def _collect_stale_aliases(self, health_report: HealthReport) -> Set[str]:
        """Collect all stale repo aliases from stale_participating_repo anomalies."""
        stale_aliases: Set[str] = set()
        for anomaly in health_report.anomalies:
            if anomaly.type == "stale_participating_repo" and anomaly.missing_repos:
                stale_aliases.update(anomaly.missing_repos)
        return stale_aliases

    def _remove_stale_aliases_from_domains(
        self,
        domain_list: List[Dict[str, Any]],
        stale_aliases: Set[str],
        output_dir: Path,
    ) -> bool:
        """
        Mutate domain_list in-place, removing stale aliases from participating_repos.

        Also triggers cleanup of stale sections from each affected domain .md file.
        Uses _is_safe_domain_name to validate domain names before path construction.
        Returns True if any domain was modified.
        """
        changed = False
        for domain in domain_list:
            participating = domain.get("participating_repos", [])
            cleaned = [r for r in participating if r not in stale_aliases]
            if len(cleaned) < len(participating):
                domain["participating_repos"] = cleaned
                changed = True
                removed = len(participating) - len(cleaned)
                self._log(
                    f"  Removed {removed} stale repo(s) from domain "
                    f"'{domain.get('name', '?')}'"
                )
                domain_name = domain.get("name", "")
                if self._is_safe_domain_name(domain_name):
                    domain_file = output_dir / f"{domain_name}.md"
                    if domain_file.exists():
                        self._remove_stale_sections_from_md(domain_file, stale_aliases)
        return changed

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
        md_files = [f for f in output_dir.glob("*.md") if not f.name.startswith("_")]

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
    # Phase 3.7: Repair SELF_LOOP graph-channel anomalies (Story #908)
    # ─────────────────────────────────────────────────────────────────────────

    def _run_phase37(
        self,
        output_dir: Path,
        fixed: List[str],
        errors: List[str],
        dry_run: bool = False,
    ) -> Optional["DryRunReport"]:
        """Phase 3.7 shim: check enable flag then delegate to repair sub-modules.

        No-op when enable_graph_channel_repair is False.
        dry_run=True: per-handler gating via is_effective_dry_run — no disk writes
        or journal appends. Returns DryRunReport (AC1/AC3).
        dry_run=False: existing mutation behavior, return None (AC4).
        """
        from datetime import datetime, timezone

        if not self._enable_graph_channel_repair:
            return None

        # Determine if any type-level dry-run is in effect (Story #920).
        # any_type_dry is True when at least one per-type flag is "dry_run" OR
        # the invocation-level dry_run is True.  This governs DryRunReport return.
        any_type_dry = dry_run or any(
            f == "dry_run"
            for f in (
                self._graph_repair_self_loop,
                self._graph_repair_malformed_yaml,
                self._graph_repair_garbage_domain,
                self._graph_repair_bidirectional_mismatch,
            )
        )
        would_be_writes: List[Tuple[str, str]] = []
        per_type: Dict[str, int] = {}
        skipped: List[Tuple[str, str]] = []
        extra_verdict_counts: Dict[str, int] = {}
        extra_action_counts: Dict[str, int] = {}
        if any_type_dry:
            per_type, skipped = self._collect_phase37_anomaly_scan(output_dir)

        self._run_phase37_repairs(
            output_dir,
            fixed,
            errors,
            dry_run=dry_run,
            would_be_writes=would_be_writes,
            extra_verdict_counts=extra_verdict_counts,
            extra_action_counts=extra_action_counts,
            skipped=skipped,
        )

        if not any_type_dry:
            return None

        per_action, per_verdict = self._tally_dry_run_actions(
            fixed, extra_verdict_counts, extra_action_counts
        )
        return DryRunReport(
            mode="dry_run",
            timestamp=datetime.now(timezone.utc).isoformat(),
            total_anomalies=sum(per_type.values()),
            per_type_counts=per_type,
            per_verdict_counts=per_verdict,
            per_action_counts=per_action,
            would_be_writes=would_be_writes,
            skipped=skipped,
            errors=list(errors),
        )

    def _dispatch_per_type(
        self,
        type_label: str,
        per_type_flag: str,
        handler: Callable,
        invocation_dry_run: bool,
        skipped: Optional[List[Tuple[str, str]]],
        disabled_recorded: Set[str],
    ) -> None:
        """Gate and invoke one Phase 3.7 handler based on per-type flag (Story #920).

        disabled: log observed-only, record in skipped once (disabled_recorded guards dupes).
        dry_run / enabled: compute effective dry_run and invoke handler with:
          - dry_run: True when either invocation_dry_run or per_type_flag=='dry_run' (skip writes)
          - journal_disabled: True ONLY when invocation_dry_run=True (Story #919 suppresses journal)
          - effective_mode: the per_type_flag itself ('enabled' or 'dry_run') for journal labeling

        Composition table:
          invocation_dry_run=False, per_type='enabled'  => dry_run=False, journal_disabled=False, effective_mode='enabled'
          invocation_dry_run=False, per_type='dry_run'  => dry_run=True,  journal_disabled=False, effective_mode='dry_run'
          invocation_dry_run=True,  per_type='enabled'  => dry_run=True,  journal_disabled=True,  effective_mode='enabled'
          invocation_dry_run=True,  per_type='dry_run'  => dry_run=True,  journal_disabled=True,  effective_mode='dry_run'
        """
        if per_type_flag == "disabled":
            self._log(f"Phase 3.7: {type_label} disabled by config; observed only")
            if skipped is not None and type_label not in disabled_recorded:
                skipped.append((type_label.lower(), "type_disabled_by_config"))
                disabled_recorded.add(type_label)
            return
        effective_dry_run = is_effective_dry_run(invocation_dry_run, per_type_flag)
        journal_disabled = invocation_dry_run
        handler(
            dry_run=effective_dry_run,
            journal_disabled=journal_disabled,
            effective_mode=per_type_flag,
        )

    def _run_phase37_repairs(
        self,
        target_dir: Path,
        fixed: List[str],
        errors: List[str],
        dry_run: bool = False,
        would_be_writes: Optional[List] = None,
        extra_verdict_counts: Optional[Dict[str, int]] = None,
        extra_action_counts: Optional[Dict[str, int]] = None,
        skipped: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        """Coordinator: run all Phase 3.7 repairs with per-type flag gating (Story #920).

        Each handler gated by _dispatch_per_type using the per-type flag.
        disabled_recorded set prevents duplicate skipped entries in the anomaly loop.
        """
        disabled_recorded: Set[str] = set()
        self._dispatch_per_type(
            "SELF_LOOP",
            self._graph_repair_self_loop,
            lambda dry_run, journal_disabled, effective_mode: run_phase37(
                target_dir,
                fixed,
                errors,
                dry_run=dry_run,
                would_be_writes=would_be_writes,
                journal_disabled=journal_disabled,
                effective_mode=effective_mode,
            ),
            dry_run,
            skipped,
            disabled_recorded,
        )
        self._dispatch_per_type(
            "MALFORMED_YAML",
            self._graph_repair_malformed_yaml,
            lambda dry_run,
            journal_disabled,
            effective_mode: run_malformed_yaml_repairs(
                target_dir,
                fixed,
                errors,
                domain_analyzer=self._domain_analyzer,
                load_domains_json_fn=self._load_domains_json,
                log_fn=self._log,
                locate_frontmatter_bounds_fn=self._locate_frontmatter_bounds,
                is_safe_domain_name_fn=self._is_safe_domain_name,
                dry_run=dry_run,
                journal_disabled=journal_disabled,
                effective_mode=effective_mode,
                would_be_writes=would_be_writes,
            ),
            dry_run,
            skipped,
            disabled_recorded,
        )
        parser = DepMapMCPParser(dep_map_path=target_dir.parent)
        _, all_anomalies, _p, _d = parser.get_cross_domain_graph_with_channels()
        domains_json = self._load_domains_json(target_dir)
        for _a in all_anomalies:
            if _a.type == AnomalyType.GARBAGE_DOMAIN_REJECTED:
                self._dispatch_per_type(
                    "GARBAGE_DOMAIN_REJECTED",
                    self._graph_repair_garbage_domain,
                    lambda dry_run,
                    journal_disabled,
                    effective_mode: self._repair_garbage_domain_rejected(
                        target_dir,
                        _a,
                        fixed,
                        errors,
                        dry_run=dry_run,
                        journal_disabled=journal_disabled,
                        effective_mode=effective_mode,
                        would_be_writes=would_be_writes,
                    ),
                    dry_run,
                    skipped,
                    disabled_recorded,
                )
            elif (
                _a.type == AnomalyType.BIDIRECTIONAL_MISMATCH
                and self._invoke_llm_fn is not None
            ):
                self._dispatch_per_type(
                    "BIDIRECTIONAL_MISMATCH",
                    self._graph_repair_bidirectional_mismatch,
                    lambda dry_run,
                    journal_disabled,
                    effective_mode: self._audit_bidirectional_mismatch(
                        target_dir,
                        _a,
                        domains_json,
                        fixed,
                        errors,
                        dry_run=dry_run,
                        journal_disabled=journal_disabled,
                        effective_mode=effective_mode,
                        would_be_writes=would_be_writes,
                        extra_verdict_counts=extra_verdict_counts,
                        extra_action_counts=extra_action_counts,
                    ),
                    dry_run,
                    skipped,
                    disabled_recorded,
                )

    def _collect_phase37_anomaly_scan(
        self,
        output_dir: Path,
    ) -> Tuple[Dict[str, int], List[Tuple[str, str]]]:
        """Scan anomalies for DryRunReport pre-scan (called only during dry-run).

        Returns (per_type, skipped). BIDIRECTIONAL_MISMATCH is recorded as skipped
        only when invoke_llm_fn is None — when wired, the Claude+ripgrep audit
        runs during dry-run (backfill write is the only gated operation).
        """
        from collections import defaultdict

        per_type: Dict[str, int] = defaultdict(int)
        skipped: List[Tuple[str, str]] = []
        parser = DepMapMCPParser(dep_map_path=output_dir.parent)
        _, all_anom, _p, _d = parser.get_cross_domain_graph_with_channels()
        for _a in all_anom:
            atype = str(_a.type.value if hasattr(_a.type, "value") else _a.type)
            per_type[atype] += 1
            if (
                _a.type == AnomalyType.BIDIRECTIONAL_MISMATCH
                and self._invoke_llm_fn is None
            ):
                skipped.append((atype, "no_invoke_llm_fn"))
        return dict(per_type), skipped

    @staticmethod
    def _tally_dry_run_actions(
        fixed_entries: List[str],
        extra_verdict_counts: Optional[Dict[str, int]] = None,
        extra_action_counts: Optional[Dict[str, int]] = None,
    ) -> Tuple[Dict[str, int], Dict[str, int]]:
        """Derive per_action and per_verdict dicts from fixed[] entry strings.

        extra_verdict_counts / extra_action_counts: optional injected tallies for
        bidirectional verdicts (INCONCLUSIVE, REFUTED) that never produce fixed[]
        entries, passed from _run_phase37_repairs via _audit_bidirectional_mismatch.
        """
        from collections import defaultdict

        per_action: Dict[str, int] = defaultdict(int)
        per_verdict: Dict[str, int] = defaultdict(int)
        for entry in fixed_entries:
            if "self-loop" in entry:
                per_action["self_loop_deleted"] += 1
                per_verdict["NA"] += 1
            elif "malformed" in entry or "reemitted" in entry:
                per_action["malformed_yaml_reemitted"] += 1
                per_verdict["NA"] += 1
            elif "garbage" in entry or "rescued" in entry:
                per_action["garbage_domain_remapped"] += 1
                per_verdict["NA"] += 1
            # NOTE: BIDIRECTIONAL_MISMATCH outcomes are NOT tallied from fixed[] strings.
            # extra_verdict_counts / extra_action_counts are the authoritative source for
            # bidirectional verdicts (set in _audit_one_impl). Tallying from fixed[] would
            # double-count CONFIRMED outcomes (fixed[] entry + extra_* both present).
        if extra_verdict_counts:
            for k, v in extra_verdict_counts.items():
                per_verdict[k] += v
        if extra_action_counts:
            for k, v in extra_action_counts.items():
                per_action[k] += v
        return dict(per_action), dict(per_verdict)

    def _audit_bidirectional_mismatch(
        self,
        output_dir: "Path",
        anomaly: Any,
        domains_json: List[Dict[str, Any]],
        fixed: List[str],
        errors: List[str],
        dry_run: bool = False,
        journal_disabled: bool = False,
        effective_mode: str = "enabled",
        would_be_writes: Optional[List] = None,
        extra_verdict_counts: Optional[Dict[str, int]] = None,
        extra_action_counts: Optional[Dict[str, int]] = None,
    ) -> None:
        """AC12 shim: load prompt template and delegate to audit_one_bidirectional_mismatch.

        Encapsulates journal creation and template path resolution so _run_phase37
        stays focused on anomaly routing.
        dry_run=True: Claude+ripgrep verification still runs; only the backfill write
        is skipped (gated inside dep_map_repair_bidirectional_backfill.py).
        journal_disabled=True: suppresses journaling entirely (invocation-level dry_run).
        effective_mode: label written to journal entries ('enabled' or 'dry_run').
        extra_verdict_counts / extra_action_counts: mutated in-place to surface
        INCONCLUSIVE/REFUTED verdicts that never produce fixed[] entries.
        """
        from pathlib import Path as _Path

        prompt_template_path = (
            _Path(__file__).parent.parent
            / "mcp"
            / "prompts"
            / "bidirectional_mismatch_audit.md"
        )
        journal = None if journal_disabled else RepairJournal()
        audit_one_bidirectional_mismatch(
            output_dir=output_dir,
            anomaly=anomaly,
            domains_json=domains_json,
            invoke_llm_fn=self._invoke_llm_fn,
            repo_path_resolver=self._repo_path_resolver,
            journal=journal,
            fixed=fixed,
            errors=errors,
            prompt_template_path=prompt_template_path,
            dry_run=dry_run,
            effective_mode=effective_mode,
            would_be_writes=would_be_writes,
            extra_verdict_counts=extra_verdict_counts,
            extra_action_counts=extra_action_counts,
        )

    def _repair_self_loop(
        self,
        output_dir: Path,
        anomaly: "AnomalyEntry",
        fixed: List[str],
        errors: List[str],
        *,
        journal_path: Optional[Path] = None,
        journal: Optional["RepairJournal"] = None,
    ) -> None:
        """Remove one SELF_LOOP row from a domain .md file and journal the repair.

        Delegates to phase37 step functions. Never raises (AC8). Idempotent (AC4).
        """
        raw_file = anomaly.file
        if ".." in raw_file:
            errors.append(f"Phase 3.7: unsafe path rejected (traversal): {raw_file!r}")
            return

        filename = Path(raw_file).name  # basename — handles absolute paths from parser
        if not filename.endswith(".md"):
            errors.append(
                f"Phase 3.7: anomaly file does not end with .md: {raw_file!r}"
            )
            return

        domain_name = filename[: -len(".md")]
        md_path = resolve_self_loop_md_path(output_dir, domain_name, errors)
        if md_path is None:
            return

        try:
            original_lines = md_path.read_text(encoding="utf-8").splitlines(
                keepends=True
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Phase 3.7: cannot read {domain_name}.md: {exc}")
            return

        new_lines = remove_self_loop_rows(domain_name, original_lines)
        if new_lines == original_lines:
            return  # idempotent — no self-loop row present

        try:
            with acquire_domain_lock(domain_name):
                if not atomic_write_text(md_path, "".join(new_lines), errors):
                    return
                fixed.append(f"Phase 3.7: removed self-loop from {domain_name}.md")
                build_and_append_journal_entry(
                    md_path, domain_name, journal_path, journal, errors
                )
        except TimeoutError as exc:
            errors.append(f"Phase 3.7: {exc}")

    def _repair_malformed_yaml(
        self,
        output_dir: Path,
        anomaly: Any,
        fixed: List[str],
        errors: List[str],
    ) -> None:
        """Thin shim kept for test backward compat; delegates to module-level helper.

        All logic lives in dep_map_repair_malformed_yaml.repair_single_malformed_yaml_anomaly.
        Domain lock is acquired inside rewrite_malformed_yaml_file (Story #917).
        """
        from code_indexer.server.services.dep_map_repair_malformed_yaml import (
            repair_single_malformed_yaml_anomaly,
        )

        repair_single_malformed_yaml_anomaly(
            output_dir,
            anomaly,
            self._load_domains_json(output_dir),
            fixed,
            errors,
            domain_analyzer=self._domain_analyzer,
            log_fn=self._log,
            locate_frontmatter_bounds_fn=self._locate_frontmatter_bounds,
            is_safe_domain_name_fn=self._is_safe_domain_name,
        )

    @staticmethod
    def _extract_prose_fragment(message: str) -> str:
        """Extract the prose fragment string from a GARBAGE_DOMAIN_REJECTED message.

        Message format: "prose-fragment target domain rejected: '<fragment>'"
        Falls back to stripping outer quotes if ast.literal_eval fails.
        Raises TypeError when message is None.
        """
        import ast

        if message is None:
            raise TypeError("message must not be None")
        prefix = "prose-fragment target domain rejected: "
        if message.startswith(prefix):
            raw = message[len(prefix) :].strip()
            try:
                return str(ast.literal_eval(raw))
            except (ValueError, SyntaxError):
                logger.debug(
                    "ast.literal_eval failed for prose-fragment; falling back to quote-strip: %r",
                    raw,
                )
                return raw.strip("'\"")
        return message

    def _append_garbage_journal(
        self,
        journal: Any,
        source_domain: str,
        target_domain: str,
        action: Any,
        citations: List[str],
        file_writes: Optional[List[Dict[str, str]]] = None,
        errors: Optional[List[str]] = None,
        effective_mode: str = "enabled",
    ) -> None:
        """Centralized GARBAGE_DOMAIN_REJECTED journal entry creation and append.

        Validates journal has append() and action has .value before creating entry.
        On failure, appends to errors when provided; otherwise logs a warning.
        effective_mode: label written to journal entry ('enabled' or 'dry_run').
        """
        if journal is None or not hasattr(journal, "append"):
            raise TypeError("journal must have an append() method")
        if action is None or not hasattr(action, "value"):
            raise TypeError("action must be an Action enum member with .value")
        try:
            entry = JournalEntry(
                anomaly_type="GARBAGE_DOMAIN_REJECTED",
                source_domain=source_domain,
                target_domain=target_domain,
                source_repos=[],
                target_repos=[],
                verdict="N_A",
                action=action.value,
                citations=citations,
                file_writes=file_writes or [],
                claude_response_raw="",
                effective_mode=effective_mode,
            )
            journal.append(entry)
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            msg = f"Phase 3.7: garbage-domain journal write failed: {exc}"
            if errors is not None:
                errors.append(msg)
            else:
                logger.warning(msg)

    def _journal_and_backfill_garbage(
        self,
        journal: Any,
        stem: str,
        target_domain: str,
        source_path: Path,
        target_path: Path,
        outgoing_cells: List[str],
        fixed: List[str],
        errors: List[str],
        dry_run: bool = False,
        journal_disabled: bool = False,
        effective_mode: str = "enabled",
        would_be_writes: Optional[List] = None,
    ) -> None:
        """Thin shim delegating to journal_and_backfill_garbage_domain.

        Domain locks are acquired inside _execute_unique_rewrite (source) and
        _write_target_backfill (target) within the production helpers (Story #917).
        dry_run=True: passed through to journal_and_backfill_garbage_domain.
        journal_disabled=True: passed through; suppresses all journaling.
        effective_mode: label written to journal entries ('enabled' or 'dry_run').
        """
        from code_indexer.server.services.dep_map_repair_garbage_domain import (
            journal_and_backfill_garbage_domain,
        )

        journal_and_backfill_garbage_domain(
            journal,
            stem,
            target_domain,
            source_path,
            target_path,
            outgoing_cells,
            fixed,
            errors,
            append_journal_fn=self._append_garbage_journal,
            dry_run=dry_run,
            journal_disabled=journal_disabled,
            effective_mode=effective_mode,
            would_be_writes=would_be_writes,
        )

    def _repair_garbage_domain_rejected(
        self,
        output_dir: Path,
        anomaly: Any,
        fixed: List[str],
        errors: List[str],
        dry_run: bool = False,
        journal_disabled: bool = False,
        effective_mode: str = "enabled",
        would_be_writes: Optional[List] = None,
    ) -> None:
        """Repair GARBAGE_DOMAIN_REJECTED anomalies via _domains.json inverted-index lookup.

        Unique mapping -> source outgoing cell rewrite + mirror incoming backfill.
        Ambiguous or no match -> journal for manual operator review.
        Handles both AnomalyEntry and AnomalyAggregate (Story #911 AC6).
        dry_run=True: skips file writes but may still journal (per-type dry_run observation mode).
        journal_disabled=True: suppresses all journaling (invocation-level dry_run, Story #919).
        effective_mode: label written to journal entries ('enabled' or 'dry_run').
        """
        from code_indexer.server.services.dep_map_parser_hygiene import AnomalyAggregate
        from code_indexer.server.services.dep_map_repair_garbage_domain import (
            build_inverted_repo_index,
            repair_one_garbage_domain_anomaly,
        )

        examples = (
            [anomaly] if not isinstance(anomaly, AnomalyAggregate) else anomaly.examples
        )
        domain_list = self._load_domains_json(output_dir)
        repo_to_domains = build_inverted_repo_index(domain_list)
        journal = None if journal_disabled else RepairJournal()
        for example in examples:
            repair_one_garbage_domain_anomaly(
                output_dir,
                example,
                repo_to_domains,
                journal,
                fixed,
                errors,
                is_safe_domain_name_fn=self._is_safe_domain_name,
                append_journal_fn=self._append_garbage_journal,
                journal_and_backfill_fn=self._journal_and_backfill_garbage,
                extract_prose_fn=self._extract_prose_fragment,
                log_fn=self._log,
                dry_run=dry_run,
                journal_disabled=journal_disabled,
                effective_mode=effective_mode,
                would_be_writes=would_be_writes,
            )

    @staticmethod
    def _body_byte_offset(raw_bytes: bytes, close_idx: int) -> int:
        """Delegate to phase37.body_byte_offset (extracted per Finding #3 / Story #910)."""
        return int(body_byte_offset(raw_bytes, close_idx))

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
    # Phase 3.5: Backfill JSON description + sync frontmatter repos
    # ─────────────────────────────────────────────────────────────────────────

    def _run_phase35(
        self,
        output_dir: Path,
        fixed: List[str],
        errors: List[str],
    ) -> None:
        """
        Phase 3.5 orchestrator — two sub-tasks run in sequence:

        Sub-task A (Bug #687 Fix 4): Backfill empty JSON descriptions from .md.
        Sub-task B (Story #688): Sync frontmatter participating_repos from JSON.
        """
        self._backfill_json_descriptions(output_dir, fixed, errors)
        self._sync_all_frontmatter_repos(output_dir, fixed, errors)

    def _backfill_json_descriptions(
        self,
        output_dir: Path,
        fixed: List[str],
        errors: List[str],
    ) -> None:
        """
        Sub-task A of Phase 3.5: backfill empty JSON descriptions from .md files.

        Idempotent: domains with a non-empty description are unchanged.
        """
        domain_list = self._load_domains_json(output_dir)
        updated_count = 0

        for domain in domain_list:
            name = domain.get("name", "")
            if not self._is_safe_domain_name(name):
                continue
            desc = domain.get("description", "") or ""
            if desc.strip():
                continue
            md_file = output_dir / f"{name}.md"
            if not md_file.exists():
                continue
            extracted = self._extract_description_from_md(md_file)
            if extracted:
                domain["description"] = extracted
                updated_count += 1

        if updated_count > 0:
            domains_file = output_dir / "_domains.json"
            try:
                domains_file.write_text(
                    json.dumps(domain_list, indent=2), encoding="utf-8"
                )
                fixed.append(f"backfilled description for {updated_count} domain(s)")
                self._log(
                    f"Phase 3.5: backfilled description for {updated_count} domain(s)"
                )
            except OSError as e:
                errors.append(f"Phase 3.5: failed to write _domains.json: {e}")
                self._log(f"Phase 3.5 write error: {e}")

    def _sync_all_frontmatter_repos(
        self,
        output_dir: Path,
        fixed: List[str],
        errors: List[str],
    ) -> None:
        """
        Sub-task B of Phase 3.5: sync frontmatter participating_repos from JSON.

        Re-loads domain_list so sub-task A writes are visible.
        Calls _sync_frontmatter_repos for each domain whose .md exists.
        """
        domain_list = self._load_domains_json(output_dir)
        for domain in domain_list:
            name = domain.get("name", "")
            if not self._is_safe_domain_name(name):
                continue
            json_repos = domain.get("participating_repos") or []
            if not isinstance(json_repos, list):
                continue
            md_file = output_dir / f"{name}.md"
            if not md_file.exists():
                continue
            try:
                if self._sync_frontmatter_repos(md_file, json_repos):
                    fixed.append(f"synced frontmatter repos for: {name}")
                    self._log(f"Phase 3.5: synced frontmatter repos for {name}")
            except OSError as e:
                errors.append(f"Phase 3.5: failed to sync frontmatter for {name}: {e}")
                self._log(f"Phase 3.5 frontmatter sync error for {name}: {e}")

    def _sync_frontmatter_repos(self, md_file: Path, json_repos: List[str]) -> bool:
        """
        Rewrite md_file's frontmatter participating_repos to match json_repos.

        Idempotent: returns False without writing when current_repos == json_repos
        (exact ordered equality). Appends the key if absent. Preserves all other
        frontmatter keys and the full markdown body. Returns True only when the
        file content actually changed.
        """
        content = md_file.read_text(encoding="utf-8")
        frontmatter = _parse_yaml_frontmatter_util(content)
        if frontmatter is None:
            return False

        current_repos = frontmatter.get("participating_repos")
        if not isinstance(current_repos, list):
            current_repos = []
        if current_repos == json_repos:
            return False

        bounds = self._locate_frontmatter_bounds(content)
        if bounds is None:
            return False
        open_idx, close_idx = bounds

        lines = content.split("\n")
        fm_lines = lines[open_idx + 1 : close_idx]
        body_lines = lines[close_idx:]

        new_fm_lines = self._rebuild_frontmatter_repos_block(fm_lines, json_repos)
        new_content = "\n".join(["---"] + new_fm_lines + body_lines)
        if new_content == content:
            return False

        try:
            with acquire_domain_lock(md_file.stem):
                md_file.write_text(new_content, encoding="utf-8")
        except TimeoutError as exc:
            raise OSError(str(exc)) from exc
        return True

    @staticmethod
    def _locate_frontmatter_bounds(content: str):
        """
        Return (open_idx, close_idx) line indices for the --- delimiters, or None.

        open_idx is always 0 (the opening ---).
        close_idx is the index of the closing --- line.
        """
        lines = content.split("\n")
        if not lines or lines[0].strip() != "---":
            return None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return (0, i)
        return None

    @staticmethod
    def _rebuild_frontmatter_repos_block(
        fm_lines: List[str], json_repos: List[str]
    ) -> List[str]:
        """
        Return fm_lines with participating_repos replaced by json_repos.

        If the key is absent, it is appended at the end.
        Old indented list items belonging to participating_repos are dropped.
        """
        new_repos_lines: List[str]
        if json_repos:
            new_repos_lines = ["participating_repos:"] + [
                f"  - {yaml_quote_if_unsafe(r)}" for r in json_repos
            ]
        else:
            new_repos_lines = ["participating_repos: []"]

        result: List[str] = []
        key_found = False
        skip_indented = False
        for line in fm_lines:
            if line.startswith("participating_repos:"):
                result.extend(new_repos_lines)
                key_found = True
                skip_indented = True
                continue
            if skip_indented and (line.startswith("  ") or line.startswith("\t")):
                continue
            skip_indented = False
            result.append(line)

        if not key_found:
            result.extend(new_repos_lines)
        return result

    @staticmethod
    def _emit_repos_lines(json_repos: List[str]) -> List[str]:
        """Delegate to phase37.emit_repos_lines (extracted per Finding #3 / Story #910)."""
        # cast: emit_repos_lines is typed -> List[str] but mypy infers Any at this call site.
        return cast(List[str], emit_repos_lines(json_repos))

    @staticmethod
    def _reemit_frontmatter_from_domain_info(
        content: str,
        bounds: tuple,
        domain_info: Dict[str, Any],
    ) -> str:
        """Delegate to phase37.reemit_frontmatter_from_domain_info (Finding #3 / Story #910)."""
        # cast: reemit_frontmatter_from_domain_info is typed -> str but mypy infers Any here.
        return cast(
            str, reemit_frontmatter_from_domain_info(content, bounds, domain_info)
        )

    def _extract_description_from_md(self, md_file: Path) -> str:
        """
        Extract a description string from a domain .md file.

        Strategy (in order):
        1. YAML frontmatter 'description' field (present after Fix 1)
        2. First non-empty, non-heading content line after '## Overview' section

        Returns empty string if nothing useful is found.
        """
        try:
            content = md_file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Phase 3.5: could not read %s: %s", md_file, e)
            return ""

        # Strategy 1: frontmatter description field
        frontmatter = _parse_yaml_frontmatter_util(content)
        if frontmatter:
            desc = (frontmatter.get("description") or "").strip()
            if desc:
                return desc

        # Strategy 2: first content line of ## Overview section
        in_overview = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == "## Overview":
                in_overview = True
                continue
            if in_overview:
                if stripped.startswith("##"):
                    break  # entered next section — nothing found
                if stripped and not stripped.startswith("#"):
                    return stripped
        return ""

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _persist_domains_and_regenerate(
        self,
        output_dir: Path,
        domain_list: List[Dict[str, Any]],
        errors: List[str],
    ) -> bool:
        """Write updated _domains.json and regenerate _index.md. Returns True on success."""
        try:
            domains_json_path = output_dir / "_domains.json"
            domains_json_path.write_text(json.dumps(domain_list, indent=2))
            self._log("Phase 1.5: Updated _domains.json")
        except Exception as e:
            errors.append(f"Phase 1.5: Failed to write _domains.json: {e}")
            return False

        try:
            self._index_regenerator.regenerate(output_dir)
            self._log("Phase 1.5: Regenerated _index.md")
        except Exception as e:
            errors.append(f"Phase 1.5: Failed to regenerate _index.md: {e}")
            return False

        return True

    def _remove_stale_sections_from_md(
        self, domain_file: Path, stale_aliases: Set[str]
    ) -> None:
        """Remove markdown sections whose header references a stale repo alias."""
        import re

        try:
            content = domain_file.read_text()
        except OSError as e:
            self._log(f"  Warning: Could not read {domain_file.name}: {e}")
            return

        lines = content.split("\n")
        result_lines: List[str] = []
        skip_until_next_header = False
        current_header_level = 0

        for line in lines:
            header_match = re.match(r"^(#{1,6})\s+(.*)", line)
            if header_match:
                level = len(header_match.group(1))
                header_text = header_match.group(2).strip()
                if skip_until_next_header:
                    if level <= current_header_level:
                        skip_until_next_header = False
                    else:
                        continue  # Still inside stale repo subsection
                if any(alias in header_text for alias in stale_aliases):
                    skip_until_next_header = True
                    current_header_level = level
                    continue
                result_lines.append(line)
            else:
                if not skip_until_next_header:
                    result_lines.append(line)

        new_content = "\n".join(result_lines)
        if new_content != content:
            try:
                with acquire_domain_lock(domain_file.stem):
                    domain_file.write_text(new_content)
                self._log(f"  Cleaned stale repo sections from {domain_file.name}")
            except (OSError, TimeoutError) as e:
                self._log(f"  Warning: Could not write {domain_file.name}: {e}")

    @staticmethod
    def _is_safe_domain_name(name: str) -> bool:
        """Return True if name is a non-empty string with no path-traversal chars."""
        return bool(name) and "/" not in name and "\\" not in name and ".." not in name

    def _load_domains_json(self, output_dir: Path) -> List[Dict[str, Any]]:
        """Load _domains.json, returning empty list if missing or invalid."""
        result: List[Dict[str, Any]] = _load_domains_json_util(output_dir)
        return result

    def _report_progress(self, progress: int, info: str = "") -> None:
        """Report progress milestone via progress_callback (if set)."""
        if self._progress_callback is not None:
            try:
                self._progress_callback(progress, info)
            except Exception as e:
                logger.warning("progress_callback raised: %s", e)

    def _log(self, message: str) -> None:
        """Log a message via journal_callback (if set) and Python logger."""
        logger.debug("[RepairExecutor] %s", message)
        if self._journal_callback is not None:
            try:
                self._journal_callback(message)
            except Exception as e:
                logger.warning("journal_callback raised: %s", e)
