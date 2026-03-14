"""
Bootstrap helper functions for CIDX server startup.

Extracted from app.py as part of Story #409 AC5 (app.py modularization).
These functions handle server startup bootstrapping for cidx-meta, legacy
migration, and Langfuse repo registration.
"""

import logging
import os
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.logging_utils import format_error_log

if TYPE_CHECKING:
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

logger = logging.getLogger(__name__)


def _detect_repo_root(start_from_file: bool = True) -> Optional[Path]:
    """
    Detect git repository root directory.

    Tries three strategies in order:
    1. CIDX_REPO_ROOT environment variable (explicit configuration from systemd service)
    2. Walk up from __file__ location (works for development/source installations)
    3. Walk up from current working directory (works for pip-installed packages)

    Args:
        start_from_file: If True, try __file__ location first. For testing, can be False.

    Returns:
        Path to repository root if found, None otherwise.

    Bug Fix: MONITOR-GENERAL-011 - Use explicit CIDX_REPO_ROOT env var for production
    servers to eliminate detection ambiguity.
    """
    repo_root = None

    # Strategy 1: Check CIDX_REPO_ROOT environment variable (set by systemd service)
    # This is the most reliable method for production deployments
    env_repo_root = os.environ.get("CIDX_REPO_ROOT")
    if env_repo_root:
        candidate = Path(env_repo_root).resolve()
        if (candidate / ".git").exists():
            logger.info(
                f"Self-monitoring: Detected repo root from CIDX_REPO_ROOT env var: {candidate}",
                extra={"correlation_id": get_correlation_id()},
            )
            return candidate
        else:
            logger.warning(
                f"Self-monitoring: CIDX_REPO_ROOT set to '{env_repo_root}' but no .git found",
                extra={"correlation_id": get_correlation_id()},
            )

    # Strategy 2: Try __file__-based detection (development/source installations)
    if start_from_file:
        current = Path(__file__).resolve().parent
        while current != current.parent:
            if (current / ".git").exists():
                repo_root = current
                logger.info(
                    f"Self-monitoring: Detected repo root from __file__: {repo_root}",
                    extra={"correlation_id": get_correlation_id()},
                )
                break
            current = current.parent

    # Strategy 3: Fallback to cwd (pip-installed packages on production)
    # If systemd service runs from cloned repo directory, cwd will have .git
    if not repo_root:
        cwd = Path.cwd()
        current = cwd
        while current != current.parent:
            if (current / ".git").exists():
                repo_root = current
                logger.info(
                    f"Self-monitoring: Detected repo root from cwd: {repo_root}",
                    extra={"correlation_id": get_correlation_id()},
                )
                break
            current = current.parent

    return repo_root


def migrate_legacy_cidx_meta(golden_repo_manager: "GoldenRepoManager", golden_repos_dir: str) -> None:
    """
    Migrate cidx-meta from legacy special-case to regular golden repo.

    This is a REGISTRY-ONLY migration. No file movement occurs because:
    - Versioning is for auto-refresh (cidx-meta has no remote)
    - cidx-meta stays at golden-repos/cidx-meta/ (NOT moved to .versioned/)

    Legacy detection scenarios:
    1. cidx-meta directory exists BUT not in metadata.json -> register it
    2. cidx-meta exists in metadata.json with repo_url=None -> update to local://cidx-meta

    Args:
        golden_repo_manager: GoldenRepoManager instance
        golden_repos_dir: Path to golden-repos directory
    """
    cidx_meta_path = Path(golden_repos_dir) / "cidx-meta"

    # Scenario 1: Directory exists but NOT registered in metadata.json
    if cidx_meta_path.exists() and not golden_repo_manager.golden_repo_exists(
        "cidx-meta"
    ):
        logger.info(
            "Detected legacy cidx-meta (directory exists, not in metadata.json)",
            extra={"correlation_id": get_correlation_id()},
        )
        logger.info(
            "Migrating to regular golden repo (registry-only, no file movement)",
            extra={"correlation_id": get_correlation_id()},
        )

        # Use standard registration path (Story #175)
        golden_repo_manager.register_local_repo(
            alias="cidx-meta",
            folder_path=cidx_meta_path,
            fire_lifecycle_hooks=False,  # ClaudeCliManager not initialized at startup
        )
        logger.info(
            "Legacy cidx-meta migrated via register_local_repo",
            extra={"correlation_id": get_correlation_id()},
        )

    # Scenario 2: Registered with repo_url=None (old special marker)
    elif golden_repo_manager.golden_repo_exists("cidx-meta"):
        repo = golden_repo_manager.get_golden_repo("cidx-meta")
        if repo and repo.repo_url is None:
            logger.info(
                "Detected legacy cidx-meta (repo_url=None in metadata.json)",
                extra={"correlation_id": get_correlation_id()},
            )

            # Update repo_url from None to local://cidx-meta
            repo.repo_url = "local://cidx-meta"
            # Persist to storage backend (SQLite)
            golden_repo_manager._sqlite_backend.update_repo_url(
                "cidx-meta", "local://cidx-meta"
            )
            logger.info(
                "Legacy cidx-meta migrated: repo_url updated to local://cidx-meta",
                extra={"correlation_id": get_correlation_id()},
            )


def bootstrap_cidx_meta(golden_repo_manager: "GoldenRepoManager", golden_repos_dir: str) -> None:
    """
    Bootstrap cidx-meta as a regular golden repo on fresh installations.

    Creates cidx-meta directory, registers it with local://cidx-meta URL,
    and initializes the CIDX index structure.
    This is idempotent - safe to call multiple times.

    Args:
        golden_repo_manager: GoldenRepoManager instance
        golden_repos_dir: Path to golden-repos directory
    """
    # Import dependencies
    import subprocess

    # Create directory structure
    cidx_meta_path = Path(golden_repos_dir) / "cidx-meta"
    cidx_meta_path.mkdir(parents=True, exist_ok=True)

    # Check if cidx-meta already exists
    already_registered = golden_repo_manager.golden_repo_exists("cidx-meta")

    if not already_registered:
        logger.info(
            "Bootstrapping cidx-meta as regular golden repo",
            extra={"correlation_id": get_correlation_id()},
        )

        # Initialize CIDX index structure if not already done
        if not (cidx_meta_path / ".code-indexer").exists():
            try:
                logger.info(
                    "Initializing cidx-meta index structure",
                    extra={"correlation_id": get_correlation_id()},
                )
                subprocess.run(
                    ["cidx", "init"],
                    cwd=str(cidx_meta_path),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                logger.info(
                    "Successfully initialized cidx-meta index structure",
                    extra={"correlation_id": get_correlation_id()},
                )
            except subprocess.CalledProcessError as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-004",
                        f"Failed to initialize cidx-meta: {e.stderr if e.stderr else str(e)}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                # Continue with registration even if init fails - don't break server startup
            except Exception as e:
                logger.error(
                    format_error_log(
                        "APP-GENERAL-005",
                        f"Unexpected error during cidx-meta initialization: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                # Continue with registration even if init fails - don't break server startup

        # Use standard registration path (Story #175)
        golden_repo_manager.register_local_repo(
            alias="cidx-meta",
            folder_path=cidx_meta_path,
            fire_lifecycle_hooks=False,  # ClaudeCliManager not initialized at startup
        )
        logger.info(
            "Bootstrapped cidx-meta via register_local_repo",
            extra={"correlation_id": get_correlation_id()},
        )


def register_langfuse_golden_repos(golden_repo_manager: "GoldenRepoManager", golden_repos_dir: str) -> None:
    """
    Register any unregistered Langfuse trace folders as golden repos.

    Scans golden-repos/ for langfuse_* directories and registers them
    using register_local_repo() standard path. Idempotent.

    Args:
        golden_repo_manager: GoldenRepoManager instance
        golden_repos_dir: Path to golden-repos directory
    """
    golden_repos_path = Path(golden_repos_dir)
    if not golden_repos_path.exists():
        return

    for folder in sorted(golden_repos_path.iterdir()):
        if not folder.is_dir() or not folder.name.startswith("langfuse_"):
            continue

        alias = folder.name

        # Use standard registration path (Story #175)
        newly_registered = golden_repo_manager.register_local_repo(
            alias=alias,
            folder_path=folder,
            fire_lifecycle_hooks=False,  # No cidx-meta description for trace folders
        )

        if newly_registered:
            logger.info(
                f"Auto-registered Langfuse folder as golden repo: {alias}",
                extra={"correlation_id": get_correlation_id()},
            )
