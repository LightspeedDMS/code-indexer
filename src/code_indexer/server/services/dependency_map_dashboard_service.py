"""
DependencyMapDashboardService for Story #212 (Dependency Map Page).

Computes job status data for the Dependency Map dashboard page, including
5-state health badge computation per the story algorithm.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class DependencyMapDashboardService:
    """
    Service for computing dependency map job status for the dashboard page.

    Computes 5-state health badge:
      GRAY   - dependency_map_enabled=False
      BLUE   - status=running
      RED    - status=failed OR last_run > 2x interval
      YELLOW - completed + (changed/new repos OR approaching stale at 75%)
      GREEN  - completed, fresh, no changed repos
    """

    def __init__(
        self,
        tracking_backend,
        config_manager,
        dependency_map_service,
    ) -> None:
        """
        Initialize the dashboard service.

        Args:
            tracking_backend: DependencyMapTrackingBackend instance
            config_manager: ServerConfigManager with get_claude_integration_config()
            dependency_map_service: DependencyMapService instance (may be None)
        """
        self._tracking_backend = tracking_backend
        self._config_manager = config_manager
        self._dependency_map_service = dependency_map_service

    def get_job_status(self) -> Dict[str, Any]:
        """
        Get full job status dict for dashboard rendering.

        Returns:
            Dict with:
                - health: str  (Disabled|Running|Unhealthy|Degraded|Healthy)
                - color:  str  (GRAY|BLUE|RED|YELLOW|GREEN)
                - status: str  (raw status from tracking backend)
                - last_run:  Optional[str] (ISO timestamp or None)
                - next_run:  Optional[str] (ISO timestamp or None)
                - error_message: Optional[str]
        """
        tracking = self._tracking_backend.get_tracking()
        config = self._config_manager.get_claude_integration_config()

        # Detect changes (safe - exception treated as no changes)
        changes = self._safe_detect_changes()

        health, color = self._compute_health(tracking, config, changes)

        return {
            "health": health,
            "color": color,
            "status": tracking.get("status"),
            "last_run": tracking.get("last_run"),
            "next_run": tracking.get("next_run"),
            "error_message": tracking.get("error_message"),
        }

    def _safe_detect_changes(self) -> Tuple[list, list, list]:
        """
        Call detect_changes() on dependency_map_service, returning empty lists on any failure.

        Returns:
            Tuple of (changed_repos, new_repos, removed_repos) - empty on error or no service
        """
        if self._dependency_map_service is None:
            return [], [], []

        try:
            return self._dependency_map_service.detect_changes()
        except Exception as e:
            logger.warning(
                "dependency_map_dashboard: detect_changes() failed, "
                "treating as no changes: %s",
                e,
            )
            return [], [], []

    def _compute_health(
        self,
        tracking: Dict[str, Any],
        config,
        changes: Tuple[list, list, list],
    ) -> Tuple[str, str]:
        """
        Compute (health_label, color) from tracking data and config.

        Algorithm per Story #212 AC3:
          1. Disabled (GRAY)  if dependency_map_enabled=False
          2. Running  (BLUE)  if status=running
          3. Unhealthy (RED)  if status=failed
          4. Unhealthy (RED)  if last_run > 2x interval_hours
          5. Completed:
             a. Degraded (YELLOW) if changed/new repos OR approaching stale (75% of 2x)
             b. Healthy  (GREEN)  otherwise
          6. Fallback: Unhealthy (RED)

        Args:
            tracking: Dict from tracking_backend.get_tracking()
            config: ClaudeIntegrationConfig instance
            changes: Tuple of (changed_repos, new_repos, removed_repos)

        Returns:
            Tuple of (health_label, color_string)
        """
        # State 1: Disabled
        if not config.dependency_map_enabled:
            return ("Disabled", "GRAY")

        status = tracking.get("status")

        # State 2: Running
        if status == "running":
            return ("Running", "BLUE")

        # State 3: Failed
        if status == "failed":
            return ("Unhealthy", "RED")

        # State 4: Stale check (requires last_run)
        last_run_str: Optional[str] = tracking.get("last_run")
        if last_run_str is not None:
            last_run = self._parse_iso(last_run_str)
            if last_run is not None:
                now = datetime.now(timezone.utc)
                hours_since_last_run = (now - last_run).total_seconds() / 3600.0
                stale_threshold_hours = config.dependency_map_interval_hours * 2

                if hours_since_last_run > stale_threshold_hours:
                    return ("Unhealthy", "RED")

                # State 5: Completed with change / approaching stale checks
                if status == "completed":
                    changed_repos, new_repos, _ = changes
                    has_changed_repos = len(changed_repos) > 0 or len(new_repos) > 0
                    approaching_stale = hours_since_last_run > (stale_threshold_hours * 0.75)

                    if has_changed_repos or approaching_stale:
                        return ("Degraded", "YELLOW")

                    return ("Healthy", "GREEN")

        # Fallback for pending/unknown with no last_run
        if status == "completed":
            # Completed but no last_run timestamp - treat as healthy (edge case)
            changed_repos, new_repos, _ = changes
            if len(changed_repos) > 0 or len(new_repos) > 0:
                return ("Degraded", "YELLOW")
            return ("Healthy", "GREEN")

        return ("Unhealthy", "RED")

    def get_repo_coverage(
        self, accessible_repos: Optional[Set[str]] = None
    ) -> Dict[str, Any]:
        """
        Get repository coverage data for dashboard rendering (Story #213).

        Computes status for each golden repo by comparing current commit hashes
        against stored tracking hashes. Applies optional access filtering for
        non-admin users.

        Status per repo:
          NEW     (BLUE)   - alias not in stored tracking hashes
          OK      (GREEN)  - current commit == stored hash
          CHANGED (YELLOW) - current commit != stored hash
          REMOVED (GRAY)   - in stored but not in current golden repos

        Coverage = (OK + CHANGED) / active_repos * 100
        REMOVED excluded from both numerator and denominator.

        Args:
            accessible_repos: Set of accessible repo aliases for non-admin users.
                              None means admin (all repos visible).

        Returns:
            Dict with:
              - repos: List[Dict] sorted alphabetically (alias, status, status_color, domains)
              - coverage_pct: float (0-100)
              - covered_count: int
              - total_count: int (excludes REMOVED)
              - coverage_color: str ("green"|"yellow"|"red")
        """
        stored_hashes = self._get_stored_hashes()
        domain_map = self._build_repo_domain_map()
        all_repos = self._compute_repo_statuses(stored_hashes, domain_map)

        # Apply access filtering: admin sees all (accessible_repos=None)
        if accessible_repos is not None:
            repos = [
                r for r in all_repos
                if r["status"] != "REMOVED" and r["alias"] in accessible_repos
            ]
        else:
            repos = all_repos

        # Sort alphabetically; REMOVED repos placed after active ones
        active = sorted(
            [r for r in repos if r["status"] != "REMOVED"],
            key=lambda r: r["alias"],
        )
        removed = sorted(
            [r for r in repos if r["status"] == "REMOVED"],
            key=lambda r: r["alias"],
        )
        repos = active + removed

        # Coverage calculation: (OK + CHANGED) / active repos
        active_count = len(active)
        covered_count = sum(
            1 for r in active if r["status"] in ("OK", "CHANGED")
        )

        if active_count == 0:
            coverage_pct = 0.0
        else:
            coverage_pct = (covered_count / active_count) * 100.0

        if coverage_pct > 80:
            coverage_color = "green"
        elif coverage_pct >= 50:
            coverage_color = "yellow"
        else:
            coverage_color = "red"

        return {
            "repos": repos,
            "coverage_pct": round(coverage_pct, 1),
            "covered_count": covered_count,
            "total_count": active_count,
            "coverage_color": coverage_color,
        }

    def _get_stored_hashes(self) -> Dict[str, str]:
        """Parse commit_hashes JSON from tracking backend, returning empty dict on error."""
        tracking = self._tracking_backend.get_tracking()
        commit_hashes_json = tracking.get("commit_hashes")
        if not commit_hashes_json:
            return {}
        try:
            return json.loads(commit_hashes_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("dependency_map_dashboard: failed to parse commit_hashes JSON")
            return {}

    def _compute_repo_statuses(
        self,
        stored_hashes: Dict[str, str],
        domain_map: Dict[str, List[str]],
    ) -> List[Dict[str, Any]]:
        """
        Compute status for all repos (active + removed).

        Uses _current_commits_provider if injected (for testing), otherwise
        reads from metadata.json in the repo's clone_path.

        The last_analyzed field is set to the tracking backend's last_run timestamp
        for repos that are present in stored_hashes (OK, CHANGED, REMOVED), and
        None for NEW repos (alias not yet in tracking data).
        """
        if self._dependency_map_service is None:
            return []

        activated = self._dependency_map_service.get_activated_repos()
        current_aliases = {r["alias"] for r in activated}

        # Fetch last_run from tracking for the last_analyzed timestamp
        tracking = self._tracking_backend.get_tracking()
        last_run = tracking.get("last_run")

        repos: List[Dict[str, Any]] = []

        for repo in activated:
            alias = repo.get("alias", "")
            if not alias:
                continue

            current_commit = self._get_current_commit(alias, repo.get("clone_path", ""))

            if alias not in stored_hashes:
                status = "NEW"
                status_color = "BLUE"
                last_analyzed = None
            elif current_commit == stored_hashes[alias]:
                status = "OK"
                status_color = "GREEN"
                last_analyzed = last_run
            else:
                status = "CHANGED"
                status_color = "YELLOW"
                last_analyzed = last_run

            repos.append({
                "alias": alias,
                "status": status,
                "status_color": status_color,
                "domains": domain_map.get(alias, []),
                "last_analyzed": last_analyzed,
            })

        # REMOVED: in stored hashes but not in current golden repos
        for alias in stored_hashes:
            if alias not in current_aliases:
                repos.append({
                    "alias": alias,
                    "status": "REMOVED",
                    "status_color": "GRAY",
                    "domains": domain_map.get(alias, []),
                    "last_analyzed": last_run,
                })

        return repos

    def _get_current_commit(self, alias: str, clone_path: str) -> Optional[str]:
        """
        Get the current commit hash for a repo.

        If _current_commits_provider is injected (testing), delegates to it.
        Otherwise reads from clone_path/.code-indexer/metadata.json.
        """
        provider = getattr(self, "_current_commits_provider", None)
        if provider is not None:
            return provider(alias)

        if not clone_path:
            return None

        metadata_path = Path(clone_path) / ".code-indexer" / "metadata.json"
        if not metadata_path.exists():
            return "local"

        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
            return metadata.get("current_commit", "unknown")
        except Exception as e:
            logger.warning(
                "dependency_map_dashboard: failed to read metadata for %s: %s", alias, e
            )
            return None

    def _build_repo_domain_map(self) -> Dict[str, List[str]]:
        """
        Build reverse map: repo_alias -> list of domain names.

        Reads from _domains.json. If _domains_file_override is set (testing),
        uses that path. Otherwise derives path from dep_map_service golden_repos_dir.

        Returns empty dict if file missing or unreadable.
        """
        domains_file_path = getattr(self, "_domains_file_override", None)

        if domains_file_path is None and self._dependency_map_service is not None:
            try:
                golden_repos_dir = self._dependency_map_service.golden_repos_dir
                domains_file_path = str(
                    Path(golden_repos_dir) / "cidx-meta" / "dependency-map" / "_domains.json"
                )
            except Exception as e:
                logger.warning(
                    "dependency_map_dashboard: failed to get golden_repos_dir: %s", e
                )
                return {}

        if domains_file_path is None:
            return {}

        domains_path = Path(domains_file_path)
        if not domains_path.exists():
            return {}

        try:
            domains_data = json.loads(domains_path.read_text())
        except Exception as e:
            logger.warning(
                "dependency_map_dashboard: failed to read _domains.json: %s", e
            )
            return {}

        repo_domain_map: Dict[str, List[str]] = {}
        for domain in domains_data:
            domain_name = domain.get("name", "")
            for repo_alias in domain.get("participating_repos", []):
                repo_domain_map.setdefault(repo_alias, []).append(domain_name)

        return repo_domain_map

    @staticmethod
    def _parse_iso(iso_str: str) -> Optional[datetime]:
        """
        Parse ISO timestamp string to timezone-aware datetime.

        Returns None if parsing fails.
        """
        if not iso_str:
            return None

        formats = [
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
        ]

        for fmt in formats:
            try:
                parsed = datetime.strptime(iso_str, fmt)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                continue

        return None
