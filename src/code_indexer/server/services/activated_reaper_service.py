"""
Activated Repository Reaper Service (Story #967).

Scans activated repositories, deactivates those idle beyond the configured TTL,
and returns structured cycle results for dashboard visibility.
"""

import dataclasses
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ReapCycleResult:
    """Result of a single reaper cycle run."""

    scanned: int
    reaped: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)


def _parse_last_accessed(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 last_accessed string to an aware datetime, or return None."""
    if not value:
        return None
    try:
        # Python 3.9 does not support the 'Z' UTC suffix in fromisoformat().
        # Replace it with '+00:00' for compatibility.
        normalized = value
        if isinstance(value, str) and value.endswith("Z"):
            normalized = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


class ActivatedReaperService:
    """
    Scans activated repositories and submits deactivation jobs for idle repos.

    A repo is considered idle when its last_accessed timestamp is older than
    activated_reaper_config.ttl_days days, or when last_accessed is None/missing
    (treated as never-accessed, i.e., always expired).
    """

    def __init__(
        self,
        activated_repo_manager: Any,
        background_job_manager: Any,
        config_service: Any,
    ) -> None:
        """
        Initialise the service.

        Args:
            activated_repo_manager: Provides list_all_activated_repositories()
                                    and _do_deactivate_repository().
            background_job_manager: Provides submit_job().
            config_service:         Provides get_config() returning ServerConfig.
        """
        self._activated_repo_manager = activated_repo_manager
        self._background_job_manager = background_job_manager
        self._config_service = config_service

    def run_reap_cycle(self) -> Dict[str, Any]:
        """
        Run one reaper cycle.

        Re-reads TTL from config on each call so that Web UI changes take effect
        without a server restart (AC4).

        Returns:
            Plain dict with scanned, reaped, skipped, and errors keys
            (JSON-serializable for BackgroundJobManager persistence).
        """
        config = self._config_service.get_config()
        ttl_days: int = config.activated_reaper_config.ttl_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)

        all_repos: List[Dict[str, Any]] = (
            self._activated_repo_manager.list_all_activated_repositories()
        )
        scanned = len(all_repos)
        reaped: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for repo in all_repos:
            username: str = repo.get("username", "")
            user_alias: str = repo.get("user_alias", "")
            raw_ts: Optional[str] = repo.get("last_accessed")
            last_accessed: Optional[datetime] = _parse_last_accessed(raw_ts)

            # AC5: None/missing last_accessed is treated as expired.
            is_expired = last_accessed is None or last_accessed < cutoff

            if is_expired:
                try:
                    self._background_job_manager.submit_job(
                        "deactivate_repository",
                        self._activated_repo_manager._do_deactivate_repository,
                        submitter_username="system",
                        is_admin=True,
                        repo_alias=user_alias,
                        username=username,
                        user_alias=user_alias,
                    )
                    reaped.append(
                        {
                            "username": username,
                            "user_alias": user_alias,
                            "last_accessed": raw_ts,
                        }
                    )
                    logger.info(
                        "Reaper: submitted deactivation for %s/%s (last_accessed=%s)",
                        username,
                        user_alias,
                        raw_ts,
                    )
                except Exception as exc:  # AC6: one failure must not abort the cycle
                    errors.append(
                        {
                            "username": username,
                            "user_alias": user_alias,
                            "error": str(exc),
                        }
                    )
                    logger.warning(
                        "Reaper: failed to submit deactivation for %s/%s: %s",
                        username,
                        user_alias,
                        exc,
                    )
            else:
                skipped.append(
                    {
                        "username": username,
                        "user_alias": user_alias,
                        "last_accessed": raw_ts,
                    }
                )

        result_obj = ReapCycleResult(
            scanned=scanned,
            reaped=reaped,
            skipped=skipped,
            errors=errors,
        )
        return dataclasses.asdict(result_obj)
