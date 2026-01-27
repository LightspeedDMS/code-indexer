#!/usr/bin/env python3
"""
Re-register all golden repositories on the CIDX server.

This script:
1. Lists all current golden repositories
2. Saves their metadata (URL, alias, branch, description, temporal settings)
3. Removes each repository
4. Re-adds each repository with the saved metadata

Usage:
    python3 tools/reregister_all_repos.py --server-url http://localhost:8000 --username admin --password <password>

Options:
    --server-url URL    CIDX server URL (default: http://localhost:8000)
    --username USER     Admin username (default: admin)
    --password PASS     Admin password (required)
    --dry-run          Show what would be done without making changes
    --wait-seconds N    Seconds to wait between operations (default: 2)
    --exclude ALIAS     Repository alias to exclude (can be used multiple times)
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, Any, List

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from code_indexer.api_clients.admin_client import AdminAPIClient
from code_indexer.api_clients.base_client import APIClientError, AuthenticationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def list_all_repos(client: AdminAPIClient) -> List[Dict[str, Any]]:
    """List all golden repositories."""
    logger.info("Listing all golden repositories...")
    result = await client.list_golden_repositories()
    # API returns "golden_repositories" not "golden_repos"
    repos = result.get("golden_repositories", result.get("golden_repos", []))
    logger.info(f"Found {len(repos)} repositories")
    return repos


async def get_repo_indexes(client: AdminAPIClient, alias: str) -> Dict[str, bool]:
    """Get index status for a repository."""
    try:
        result = await client.get_golden_repo_indexes(alias)
        return {
            "has_semantic": result.get("has_semantic_index", False),
            "has_fts": result.get("has_fts_index", False),
            "has_temporal": result.get("has_temporal_index", False),
            "has_scip": result.get("has_scip_index", False),
        }
    except Exception as e:
        logger.warning(f"Could not get indexes for {alias}: {e}")
        return {
            "has_semantic": False,
            "has_fts": False,
            "has_temporal": False,
            "has_scip": False,
        }


async def delete_repo(client: AdminAPIClient, alias: str, dry_run: bool = False) -> None:
    """Delete a golden repository."""
    if dry_run:
        logger.info(f"[DRY RUN] Would delete: {alias}")
        return

    logger.info(f"Deleting repository: {alias}")
    try:
        result = await client.delete_golden_repository(alias, force=True)
        logger.info(f"  Deleted: {alias}")
    except Exception as e:
        logger.error(f"  Failed to delete {alias}: {e}")
        raise


async def add_repo(
    client: AdminAPIClient,
    repo_data: Dict[str, Any],
    dry_run: bool = False,
) -> str:
    """Add a golden repository."""
    alias = repo_data["alias"]
    repo_url = repo_data["repo_url"]
    default_branch = repo_data.get("default_branch", "main")

    if dry_run:
        logger.info(f"[DRY RUN] Would add: {alias} from {repo_url} (branch: {default_branch})")
        return "dry-run-job-id"

    logger.info(f"Adding repository: {alias}")
    logger.info(f"  URL: {repo_url}")
    logger.info(f"  Branch: {default_branch}")

    try:
        result = await client.add_golden_repository(
            git_url=repo_url,
            alias=alias,
            default_branch=default_branch,
        )
        job_id = result.get("job_id", "unknown")
        logger.info(f"  Added: {alias} (job_id: {job_id})")
        return job_id
    except Exception as e:
        logger.error(f"  Failed to add {alias}: {e}")
        raise


async def wait_for_job(
    client: AdminAPIClient,
    job_id: str,
    timeout_seconds: int = 300,
) -> bool:
    """Wait for a background job to complete."""
    logger.info(f"  Waiting for job {job_id} to complete...")
    start_time = time.time()

    while time.time() - start_time < timeout_seconds:
        try:
            # Note: You'll need to implement get_job_status if not already available
            # For now, just wait a fixed time
            await asyncio.sleep(5)

            # Check if job is done (simplified - adjust based on actual API)
            # This is a placeholder - you may need to implement job status checking
            logger.info(f"  Job {job_id} progress check...")

        except Exception as e:
            logger.warning(f"  Error checking job status: {e}")

        await asyncio.sleep(5)

    logger.warning(f"  Job {job_id} timeout after {timeout_seconds}s")
    return False


async def reindex_repo(
    client: AdminAPIClient,
    alias: str,
    index_types: Dict[str, bool],
    dry_run: bool = False,
) -> None:
    """Re-add indexes to a repository."""
    if dry_run:
        enabled_indexes = [k for k, v in index_types.items() if v]
        logger.info(f"[DRY RUN] Would add indexes to {alias}: {enabled_indexes}")
        return

    # Add semantic index (default)
    if index_types.get("has_semantic"):
        try:
            await client.add_index_to_golden_repo(alias, "semantic")
            logger.info(f"  Added semantic index to {alias}")
        except Exception as e:
            logger.error(f"  Failed to add semantic index: {e}")

    # Add FTS index if it was present
    if index_types.get("has_fts"):
        try:
            await client.add_index_to_golden_repo(alias, "fts")
            logger.info(f"  Added FTS index to {alias}")
        except Exception as e:
            logger.error(f"  Failed to add FTS index: {e}")

    # Add temporal index if it was present
    if index_types.get("has_temporal"):
        try:
            await client.add_index_to_golden_repo(alias, "temporal")
            logger.info(f"  Added temporal index to {alias}")
        except Exception as e:
            logger.error(f"  Failed to add temporal index: {e}")

    # Add SCIP index if it was present
    if index_types.get("has_scip"):
        try:
            await client.add_index_to_golden_repo(alias, "scip")
            logger.info(f"  Added SCIP index to {alias}")
        except Exception as e:
            logger.error(f"  Failed to add SCIP index: {e}")


async def reregister_all_repos(
    server_url: str,
    username: str,
    password: str,
    dry_run: bool = False,
    wait_seconds: int = 2,
    exclude_repos: List[str] = None,
) -> None:
    """Re-register all golden repositories."""
    if exclude_repos is None:
        exclude_repos = []

    logger.info("=" * 80)
    logger.info("CIDX Golden Repository Re-registration")
    logger.info("=" * 80)
    logger.info(f"Server: {server_url}")
    logger.info(f"User: {username}")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    if exclude_repos:
        logger.info(f"Excluding: {', '.join(exclude_repos)}")
    logger.info("=" * 80)

    # Create admin client
    credentials = {
        "username": username,
        "password": password,
    }
    client = AdminAPIClient(
        server_url=server_url,
        credentials=credentials,
    )

    # Step 1: List all repositories and save metadata
    # Authentication happens automatically on first request
    logger.info("Connecting to server and listing repositories...")
    try:
        repos = await list_all_repos(client)
    except AuthenticationError as e:
        logger.error(f"Authentication failed: {e}")
        await client.close()
        return
    except Exception as e:
        logger.error(f"Connection failed: {e}")
        await client.close()
        return

    if not repos:
        logger.warning("No repositories found. Nothing to do.")
        return

    # Filter out excluded repos
    if exclude_repos:
        original_count = len(repos)
        repos = [r for r in repos if r["alias"] not in exclude_repos]
        excluded_count = original_count - len(repos)
        if excluded_count > 0:
            logger.info(f"Excluded {excluded_count} repositories")

    # Save metadata including index status
    logger.info("")
    logger.info("Collecting metadata for all repositories...")
    repos_metadata = []
    for repo in repos:
        alias = repo["alias"]
        indexes = await get_repo_indexes(client, alias)
        repo_with_indexes = {**repo, "indexes": indexes}
        repos_metadata.append(repo_with_indexes)
        logger.info(f"  {alias}: {repo['repo_url']} (branch: {repo.get('default_branch', 'main')})")

    # Save to file for backup
    backup_file = Path("repos_backup.json")
    with open(backup_file, "w") as f:
        json.dump(repos_metadata, f, indent=2)
    logger.info(f"Backed up metadata to {backup_file}")

    # Step 2: Delete all repositories
    logger.info("")
    logger.info("Deleting all repositories...")
    for repo in repos_metadata:
        alias = repo["alias"]
        await delete_repo(client, alias, dry_run=dry_run)
        if not dry_run:
            await asyncio.sleep(wait_seconds)

    if not dry_run:
        logger.info("Waiting for deletions to complete...")
        await asyncio.sleep(wait_seconds * 2)

    # Step 3: Re-add all repositories
    logger.info("")
    logger.info("Re-adding all repositories...")
    for repo in repos_metadata:
        alias = repo["alias"]
        job_id = await add_repo(client, repo, dry_run=dry_run)

        if not dry_run:
            await asyncio.sleep(wait_seconds)

            # Optionally re-add indexes
            # Note: The add operation might already create default indexes
            # Uncomment if you need to explicitly re-add indexes
            # await reindex_repo(client, alias, repo["indexes"], dry_run=dry_run)
            # await asyncio.sleep(wait_seconds)

    logger.info("")
    logger.info("=" * 80)
    logger.info("Re-registration complete!")
    logger.info("=" * 80)
    logger.info(f"Total repositories processed: {len(repos_metadata)}")
    logger.info(f"Backup saved to: {backup_file}")

    # Close client
    await client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Re-register all golden repositories on CIDX server"
    )
    parser.add_argument(
        "--server-url",
        default="http://localhost:8000",
        help="CIDX server URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--username",
        default="admin",
        help="Admin username (default: admin)",
    )
    parser.add_argument(
        "--password",
        required=True,
        help="Admin password (required)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=2,
        help="Seconds to wait between operations (default: 2)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        dest="exclude_repos",
        help="Repository alias to exclude (can be used multiple times)",
    )

    args = parser.parse_args()

    # Run async main
    asyncio.run(
        reregister_all_repos(
            server_url=args.server_url,
            username=args.username,
            password=args.password,
            dry_run=args.dry_run,
            wait_seconds=args.wait_seconds,
            exclude_repos=args.exclude_repos or [],
        )
    )


if __name__ == "__main__":
    main()
