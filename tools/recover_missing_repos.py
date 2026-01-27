#!/usr/bin/env python3
"""
Recover missing repositories after failed re-registration.

Reads repos_backup.json and adds back any repositories that are missing.
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


async def recover_missing_repos(
    server_url: str,
    username: str,
    password: str,
    backup_file: str = "repos_backup.json",
    dry_run: bool = False,
    wait_seconds: int = 3,
) -> None:
    """Recover missing repositories from backup."""
    logger.info("=" * 80)
    logger.info("CIDX Repository Recovery")
    logger.info("=" * 80)
    logger.info(f"Server: {server_url}")
    logger.info(f"Backup: {backup_file}")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info("=" * 80)

    # Load backup
    with open(backup_file, 'r') as f:
        all_repos = json.load(f)

    logger.info(f"Loaded {len(all_repos)} repositories from backup")

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

        # Find missing repositories
        missing_repos = [repo for repo in all_repos if repo["alias"] not in current_aliases]

        logger.info("")
        logger.info(f"Missing repositories: {len(missing_repos)}")

        if not missing_repos:
            logger.info("No missing repositories! Recovery not needed.")
            await client.close()
            return

        # Show missing repos
        logger.info("")
        logger.info("Missing repositories:")
        for repo in missing_repos:
            logger.info(f"  - {repo['alias']}")

        if dry_run:
            logger.info("")
            logger.info("[DRY RUN] Would add back all missing repositories")
            await client.close()
            return

        # Add back missing repositories
        logger.info("")
        logger.info("Adding back missing repositories...")
        logger.info("(Increased timeout to handle slow server responses)")
        logger.info("")

        success_count = 0
        failed_count = 0

        for i, repo in enumerate(missing_repos, 1):
            alias = repo["alias"]
            repo_url = repo["repo_url"]
            default_branch = repo.get("default_branch", "main")

            logger.info(f"[{i}/{len(missing_repos)}] Adding: {alias}")
            logger.info(f"  URL: {repo_url}")
            logger.info(f"  Branch: {default_branch}")

            try:
                result = await client.add_golden_repository(
                    git_url=repo_url,
                    alias=alias,
                    default_branch=default_branch,
                )
                job_id = result.get("job_id", "unknown")
                logger.info(f"  ✓ Added (job_id: {job_id})")
                success_count += 1

                # Wait between operations
                await asyncio.sleep(wait_seconds)

            except Exception as e:
                logger.error(f"  ✗ Failed: {e}")
                failed_count += 1
                # Continue with next repo instead of stopping

        logger.info("")
        logger.info("=" * 80)
        logger.info("Recovery Complete!")
        logger.info("=" * 80)
        logger.info(f"Successfully added: {success_count}")
        logger.info(f"Failed: {failed_count}")
        logger.info(f"Total processed: {len(missing_repos)}")

        if failed_count > 0:
            logger.warning(f"{failed_count} repositories failed. Run recovery again to retry.")

    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Recover missing repositories from backup"
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
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=3,
        help="Seconds to wait between operations (default: 3)",
    )

    args = parser.parse_args()

    # Run async main
    asyncio.run(
        recover_missing_repos(
            server_url=args.server_url,
            username=args.username,
            password=args.password,
            backup_file=args.backup_file,
            dry_run=args.dry_run,
            wait_seconds=args.wait_seconds,
        )
    )


if __name__ == "__main__":
    main()
