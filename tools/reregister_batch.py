#!/usr/bin/env python3
"""
Re-register repositories in small batches (delete + re-add).

Usage:
    python3 tools/reregister_batch.py --batch-size 2 --server-url ... --username ... --password ...
"""

import argparse
import asyncio
import json
import logging
import sys
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


async def reregister_batch(
    server_url: str,
    username: str,
    password: str,
    backup_file: str = "repos_backup.json",
    batch_size: int = 2,
    wait_seconds: int = 3,
    exclude_repos: List[str] = None,
) -> None:
    """Re-register repositories in small batches (delete + re-add)."""
    if exclude_repos is None:
        exclude_repos = []

    logger.info("=" * 80)
    logger.info("CIDX Repository Re-registration - Batch Mode")
    logger.info("=" * 80)
    logger.info(f"Server: {server_url}")
    logger.info(f"Backup: {backup_file}")
    logger.info(f"Batch size: {batch_size} repositories at a time")
    if exclude_repos:
        logger.info(f"Excluding: {', '.join(exclude_repos)}")
    logger.info("=" * 80)

    # Load backup
    with open(backup_file, 'r') as f:
        all_repos = json.load(f)

    # Filter excluded
    all_repos = [r for r in all_repos if r["alias"] not in exclude_repos]
    logger.info(f"Loaded {len(all_repos)} repositories from backup (after exclusions)")

    # Create admin client
    credentials = {
        "username": username,
        "password": password,
    }
    client = AdminAPIClient(
        server_url=server_url,
        credentials=credentials,
    )

    try:
        # Get current repositories
        logger.info("Fetching current repositories from server...")
        result = await client.list_golden_repositories()
        current_repos = result.get("golden_repositories", result.get("golden_repos", []))
        current_aliases = {repo["alias"] for repo in current_repos}

        logger.info(f"Found {len(current_aliases)} repositories on server")

        # Find repositories that need re-registration (not on server)
        missing_repos = [repo for repo in all_repos if repo["alias"] not in current_aliases]

        logger.info("")
        logger.info(f"Total missing from server: {len(missing_repos)} repositories")

        if not missing_repos:
            logger.info("No missing repositories! All are registered.")
            await client.close()
            return

        # Take only the first batch_size repos
        batch = missing_repos[:batch_size]
        remaining = len(missing_repos) - batch_size

        logger.info(f"Processing batch of {len(batch)} repositories")
        if remaining > 0:
            logger.info(f"Remaining after this batch: {remaining}")
        logger.info("")

        # Process each repository in batch: delete (if exists) then add
        success_count = 0
        failed_count = 0

        for i, repo in enumerate(batch, 1):
            alias = repo["alias"]
            repo_url = repo["repo_url"]
            default_branch = repo.get("default_branch", "main")

            logger.info(f"[{i}/{len(batch)}] Processing: {alias}")
            logger.info(f"  URL: {repo_url}")
            logger.info(f"  Branch: {default_branch}")

            # Step 1: Try to delete (in case it exists in a bad state)
            try:
                logger.info(f"  Checking if {alias} exists...")
                await client.delete_golden_repository(alias, force=True)
                logger.info(f"  Deleted existing {alias}")
                await asyncio.sleep(2)  # Wait for deletion to complete
            except Exception as e:
                # If delete fails, repo probably doesn't exist - that's fine
                logger.info(f"  No existing repo to delete (expected)")

            # Step 2: Add the repository
            try:
                result = await client.add_golden_repository(
                    git_url=repo_url,
                    alias=alias,
                    default_branch=default_branch,
                )
                job_id = result.get("job_id", "unknown")
                logger.info(f"  ✓ Added (job_id: {job_id})")
                success_count += 1

                # Wait between repositories
                if i < len(batch):  # Don't wait after last one
                    await asyncio.sleep(wait_seconds)

            except Exception as e:
                logger.error(f"  ✗ Failed to add: {e}")
                failed_count += 1

        logger.info("")
        logger.info("=" * 80)
        logger.info("Batch Complete!")
        logger.info("=" * 80)
        logger.info(f"Successfully added: {success_count}")
        logger.info(f"Failed: {failed_count}")
        logger.info(f"Remaining to process: {remaining}")
        logger.info("")

        if remaining > 0:
            logger.info(f"To process next batch, run:")
            logger.info(f"  ./tools/run_reregister_batch.sh {batch_size}")

    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Re-register repositories in small batches (delete + re-add)"
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
        "--backup-file",
        default="repos_backup.json",
        help="Backup file path (default: repos_backup.json)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=2,
        help="Number of repositories to process per batch (default: 2)",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=3,
        help="Seconds to wait between operations (default: 3)",
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
        reregister_batch(
            server_url=args.server_url,
            username=args.username,
            password=args.password,
            backup_file=args.backup_file,
            batch_size=args.batch_size,
            wait_seconds=args.wait_seconds,
            exclude_repos=args.exclude_repos or [],
        )
    )


if __name__ == "__main__":
    main()
