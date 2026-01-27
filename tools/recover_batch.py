#!/usr/bin/env python3
"""
Recover missing repositories in small batches.

Usage:
    python3 tools/recover_batch.py --batch-size 2 --server-url ... --username ... --password ...
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


async def recover_batch(
    server_url: str,
    username: str,
    password: str,
    backup_file: str = "repos_backup.json",
    batch_size: int = 2,
    wait_seconds: int = 5,
) -> None:
    """Recover missing repositories in small batches."""
    logger.info("=" * 80)
    logger.info("CIDX Repository Recovery - Batch Mode")
    logger.info("=" * 80)
    logger.info(f"Server: {server_url}")
    logger.info(f"Backup: {backup_file}")
    logger.info(f"Batch size: {batch_size} repositories at a time")
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
        logger.info(f"Total missing: {len(missing_repos)} repositories")

        if not missing_repos:
            logger.info("No missing repositories! Recovery complete.")
            await client.close()
            return

        # Take only the first batch_size repos
        batch = missing_repos[:batch_size]
        remaining = len(missing_repos) - batch_size

        logger.info(f"Processing batch of {len(batch)} repositories")
        if remaining > 0:
            logger.info(f"Remaining after this batch: {remaining}")
        logger.info("")

        # Add repositories in this batch
        success_count = 0
        failed_count = 0

        for i, repo in enumerate(batch, 1):
            alias = repo["alias"]
            repo_url = repo["repo_url"]
            default_branch = repo.get("default_branch", "main")

            logger.info(f"[{i}/{len(batch)}] Adding: {alias}")
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
                if i < len(batch):  # Don't wait after last one
                    await asyncio.sleep(wait_seconds)

            except Exception as e:
                logger.error(f"  ✗ Failed: {e}")
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
            logger.info(f"To process next batch, run this command again:")
            logger.info(f"  python3 tools/recover_batch.py --batch-size {batch_size} \\")
            logger.info(f"    --server-url {server_url} \\")
            logger.info(f"    --username {username} \\")
            logger.info(f"    --password <password>")

    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Recover missing repositories in small batches"
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
        help="Number of repositories to add per batch (default: 2)",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=5,
        help="Seconds to wait between operations (default: 5)",
    )

    args = parser.parse_args()

    # Run async main
    asyncio.run(
        recover_batch(
            server_url=args.server_url,
            username=args.username,
            password=args.password,
            backup_file=args.backup_file,
            batch_size=args.batch_size,
            wait_seconds=args.wait_seconds,
        )
    )


if __name__ == "__main__":
    main()
