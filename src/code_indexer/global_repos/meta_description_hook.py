"""
Meta description lifecycle hooks for golden repositories.

Provides hooks that automatically create/delete .md files in cidx-meta
when golden repos are added/removed, eliminating the need for special-case
meta directory management code.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from code_indexer.server.services.claude_cli_manager import (
    get_claude_cli_manager,
)

logger = logging.getLogger(__name__)

# README file detection order
README_NAMES = [
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "readme.md",
    "Readme.md",
]

# Module-level tracking backend (initialized by set_tracking_backend)
_tracking_backend: Optional["DescriptionRefreshTrackingBackend"] = None  # type: ignore
_scheduler: Optional["DescriptionRefreshScheduler"] = None  # type: ignore
_refresh_scheduler: Optional[Any] = None  # type: ignore


def set_tracking_backend(backend) -> None:
    """
    Set module-level tracking backend.

    Args:
        backend: DescriptionRefreshTrackingBackend instance

    Note:
        Called during server startup to inject the tracking backend.
    """
    global _tracking_backend
    _tracking_backend = backend


def set_scheduler(scheduler) -> None:
    """
    Set module-level scheduler.

    Args:
        scheduler: DescriptionRefreshScheduler instance

    Note:
        Called during server startup to inject the scheduler.
    """
    global _scheduler
    _scheduler = scheduler


def set_refresh_scheduler(scheduler) -> None:
    """
    Set module-level RefreshScheduler for triggering cidx-meta reindex.

    Args:
        scheduler: RefreshScheduler instance (from GlobalReposLifecycleManager)

    Note:
        Called during server startup after global_lifecycle_manager is created.
        Used by on_repo_added() and on_repo_removed() to trigger versioned
        CoW reindex of cidx-meta via trigger_refresh_for_repo().
    """
    global _refresh_scheduler
    _refresh_scheduler = scheduler


def on_repo_added(
    repo_name: str,
    repo_url: str,
    clone_path: str,
    golden_repos_dir: str,
) -> None:
    """
    Hook called after a golden repository is successfully added.

    Creates a .md description file in cidx-meta and triggers reindex via RefreshScheduler.
    Also creates a tracking record for periodic description refresh (Story #190).

    Args:
        repo_name: Name/alias of the repository
        repo_url: Repository URL
        clone_path: Path to cloned repository
        golden_repos_dir: Path to golden-repos directory

    Note:
        - Skips cidx-meta itself (no self-referential .md file)
        - Handles missing clone paths gracefully (logs warning, no crash)
        - Triggers cidx-meta reindex via RefreshScheduler after creating .md file
        - Falls back to README copy when Claude CLI unavailable or fails
        - Creates tracking record for scheduled refresh (if tracking backend available)
    """
    # Skip cidx-meta itself
    if repo_name == "cidx-meta":
        logger.info("Skipping meta description generation for cidx-meta itself")
        return

    # Create tracking record for scheduled refresh (Story #190)
    if _tracking_backend is not None:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()

            # Calculate next run using scheduler if available, else use now
            if _scheduler is not None:
                next_run = _scheduler.calculate_next_run(repo_name)
            else:
                next_run = now_iso

            _tracking_backend.upsert_tracking(
                repo_alias=repo_name,
                status="pending",
                next_run=next_run,
                created_at=now_iso,
                updated_at=now_iso,
            )
            logger.info(f"Created tracking record for {repo_name} (next_run: {next_run})")
        except Exception as e:
            # Don't block repo add if tracking fails
            logger.warning(
                f"Failed to create tracking record for {repo_name}: {e}", exc_info=True
            )
    else:
        logger.debug(f"Tracking backend not available, skipping tracking record for {repo_name}")

    cidx_meta_path = Path(golden_repos_dir) / "cidx-meta"

    # Ensure cidx-meta directory exists
    if not cidx_meta_path.exists():
        logger.warning(
            f"cidx-meta directory does not exist at {cidx_meta_path}, cannot create .md file"
        )
        return

    # Use global ClaudeCliManager singleton (Story #23, AC4)
    # This ensures consistent API key handling and avoids creating multiple instances
    cli_manager = get_claude_cli_manager()

    # Determine whether to use Claude CLI or README fallback
    if cli_manager is None:
        logger.info(
            f"ClaudeCliManager not initialized, using README fallback for {repo_name}"
        )
        _create_readme_fallback(Path(clone_path), repo_name, cidx_meta_path)

    elif not cli_manager.check_cli_available():
        logger.info(f"Claude CLI unavailable, using README fallback for {repo_name}")
        _create_readme_fallback(Path(clone_path), repo_name, cidx_meta_path)

    else:
        # Generate .md file using Claude CLI
        try:
            md_content = _generate_repo_description(repo_name, repo_url, clone_path)
            md_file = cidx_meta_path / f"{repo_name}.md"
            md_file.write_text(md_content)
            logger.info(f"Created meta description file: {md_file}")

        except Exception as e:
            logger.error(
                f"Failed to create meta description for {repo_name}: {e}", exc_info=True
            )
            # Fall back to README copy
            logger.info(f"Falling back to README copy for {repo_name}")
            _create_readme_fallback(Path(clone_path), repo_name, cidx_meta_path)

    # Trigger cidx-meta reindex to make the new description searchable
    if _refresh_scheduler is not None:
        try:
            _refresh_scheduler.trigger_refresh_for_repo("cidx-meta-global")
            logger.info(f"Triggered cidx-meta refresh after adding {repo_name}")
        except Exception as e:
            logger.warning(f"Failed to trigger cidx-meta refresh for {repo_name}: {e}")


def on_repo_removed(repo_name: str, golden_repos_dir: str) -> None:
    """
    Hook called after a golden repository is successfully removed.

    Deletes the .md description file from cidx-meta and triggers reindex via RefreshScheduler.
    Also deletes the tracking record for description refresh (Story #190).

    Args:
        repo_name: Name/alias of the repository being removed
        golden_repos_dir: Path to golden-repos directory

    Note:
        - Handles nonexistent .md files gracefully (no crash)
        - Triggers cidx-meta reindex via RefreshScheduler if file was actually deleted
        - Deletes tracking record (if tracking backend available)
    """
    cidx_meta_path = Path(golden_repos_dir) / "cidx-meta"
    md_file = cidx_meta_path / f"{repo_name}.md"

    # Delete .md file if it exists
    if md_file.exists():
        try:
            md_file.unlink()
            logger.info(f"Deleted meta description file: {md_file}")

            # Trigger cidx-meta reindex after successful deletion
            if _refresh_scheduler is not None:
                try:
                    _refresh_scheduler.trigger_refresh_for_repo("cidx-meta-global")
                    logger.info(
                        f"Triggered cidx-meta refresh after removing {repo_name}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to trigger cidx-meta refresh for {repo_name}: {e}"
                    )

        except Exception as e:
            logger.error(
                f"Failed to delete meta description for {repo_name}: {e}", exc_info=True
            )
            # Don't crash the golden repo remove operation - log and continue
    else:
        logger.debug(f"No meta description file to delete for {repo_name}")

    # Delete tracking record (Story #190)
    if _tracking_backend is not None:
        try:
            _tracking_backend.delete_tracking(repo_name)
            logger.info(f"Deleted tracking record for {repo_name}")
        except Exception as e:
            # Don't block repo removal if tracking deletion fails
            logger.warning(
                f"Failed to delete tracking record for {repo_name}: {e}", exc_info=True
            )
    else:
        logger.debug(f"Tracking backend not available, skipping tracking record deletion for {repo_name}")


def _find_readme(repo_path: Path) -> Optional[Path]:
    """
    Find README file in repository.

    Args:
        repo_path: Path to repository

    Returns:
        Path to README file if found, None otherwise

    Note:
        Checks README files in priority order defined by README_NAMES.
    """
    for readme_name in README_NAMES:
        readme_path = repo_path / readme_name
        if readme_path.exists():
            return readme_path
    return None


def _create_readme_fallback(
    repo_path: Path, alias: str, meta_dir: Path
) -> Optional[Path]:
    """
    Create README fallback file in meta directory.

    Args:
        repo_path: Path to repository
        alias: Repository alias/name
        meta_dir: Path to cidx-meta directory

    Returns:
        Path to created fallback file if README found, None otherwise

    Note:
        - Creates file named <alias>_README.md
        - Preserves original README content exactly
        - Called from on_repo_added() which triggers cidx-meta reindex
    """
    readme_path = _find_readme(repo_path)
    if readme_path is None:
        logger.warning(f"No README found in {repo_path} for fallback")
        return None

    # Create fallback file with <alias>_README.md naming
    fallback_path = meta_dir / f"{alias}_README.md"

    try:
        # Copy README content exactly
        content = readme_path.read_text(encoding="utf-8")
        fallback_path.write_text(content, encoding="utf-8")
        logger.info(f"Created README fallback: {fallback_path}")

        return fallback_path

    except Exception as e:
        logger.error(
            f"Failed to create README fallback for {alias}: {e}", exc_info=True
        )
        return None


def _generate_repo_description(repo_name: str, repo_url: str, clone_path: str) -> str:
    """
    Generate .md file content for a repository using RepoAnalyzer.

    Args:
        repo_name: Repository name/alias
        repo_url: Repository URL
        clone_path: Path to cloned repository

    Returns:
        Markdown content for .md file with rich metadata from Claude analysis
    """
    from datetime import datetime, timezone

    from .repo_analyzer import RepoAnalyzer

    now = datetime.now(timezone.utc).isoformat()

    # Use RepoAnalyzer for rich metadata extraction (uses Claude SDK if available)
    analyzer = RepoAnalyzer(clone_path)
    info = analyzer.extract_info()

    # Build YAML frontmatter with rich metadata
    tech_list = (
        "\n".join(f"  - {tech}" for tech in info.technologies)
        if info.technologies
        else "  []"
    )

    frontmatter = f"""---
name: {repo_name}
url: {repo_url}
technologies:
{tech_list}
purpose: {info.purpose}
last_analyzed: {now}
---
"""

    # Build body with summary and details
    body = f"""
# {repo_name}

{info.summary}

**Repository URL**: {repo_url}
"""

    # Add features section if available
    if info.features:
        body += "\n## Features\n\n"
        for feat in info.features[:10]:
            body += f"- {feat}\n"

    # Add use cases section if available
    if info.use_cases:
        body += "\n## Use Cases\n\n"
        for uc in info.use_cases[:5]:
            body += f"- {uc}\n"

    return frontmatter + body


