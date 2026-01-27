#!/usr/bin/env python3
"""
Re-register a single golden repository on the CIDX server.

Usage:
    python3 tools/reregister_single_repo.py --server-url http://localhost:8000 \
        --username admin --password <password> --alias repo-name [--dry-run]
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Dict, Any

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from code_indexer.api_clients.admin_client import AdminAPIClient
from code_indexer.api_clients.base_client import APIClientError, AuthenticationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def reregister_single_repo(
    server_url: str,
    username: str,
    password: str,
    alias: str,
    dry_run: bool = False,
    wait_seconds: int = 2,
) -> None:
    """Re-register a single golden repository."""
    logger.info("=" * 80)
    logger.info("CIDX Single Repository Re-registration")
    logger.info("=" * 80)
    logger.info(f"Server: {server_url}")
    logger.info(f"User: {username}")
    logger.info(f"Repository: {alias}")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
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

    try:
        # Step 1: Get repository metadata
        logger.info(f"Fetching metadata for {alias}...")
        result = await client.list_golden_repositories()
        repos = result.get("golden_repositories", result.get("golden_repos", []))

        repo = None
        for r in repos:
            if r["alias"] == alias:
                repo = r
                break

        if not repo:
            logger.error(f"Repository '{alias}' not found!")
            await client.close()
            return

        logger.info(f"Found: {repo['repo_url']} (branch: {repo.get('default_branch', 'main')})")

        # Get index status
        try:
            indexes = await client.get_golden_repo_indexes(alias)
            repo['indexes'] = {
                "has_semantic": indexes.get("has_semantic_index", False),
                "has_fts": indexes.get("has_fts_index", False),
                "has_temporal": indexes.get("has_temporal_index", False),
                "has_scip": indexes.get("has_scip_index", False),
            }
            logger.info(f"Indexes: {repo['indexes']}")
        except Exception as e:
            logger.warning(f"Could not get indexes: {e}")
            repo['indexes'] = {}

        # Save backup
        backup_file = Path(f"{alias}_backup.json")
        with open(backup_file, "w") as f:
            json.dump(repo, f, indent=2)
        logger.info(f"Backed up metadata to {backup_file}")

        # Step 2: Delete repository
        logger.info("")
        logger.info(f"Deleting repository: {alias}")
        if dry_run:
            logger.info(f"[DRY RUN] Would delete: {alias}")
        else:
            try:
                await client.delete_golden_repository(alias, force=True)
                logger.info(f"  Deleted: {alias}")
                await asyncio.sleep(wait_seconds)
            except Exception as e:
                logger.error(f"  Failed to delete {alias}: {e}")
                await client.close()
                return

        # Step 3: Re-add repository
        logger.info("")
        logger.info(f"Re-adding repository: {alias}")
        if dry_run:
            logger.info(f"[DRY RUN] Would add: {alias} from {repo['repo_url']} (branch: {repo.get('default_branch', 'main')})")
        else:
            try:
                result = await client.add_golden_repository(
                    git_url=repo['repo_url'],
                    alias=alias,
                    default_branch=repo.get('default_branch', 'main'),
                )
                job_id = result.get("job_id", "unknown")
                logger.info(f"  Added: {alias} (job_id: {job_id})")
                logger.info(f"  Monitor job status at: {server_url}/admin/jobs")
            except Exception as e:
                logger.error(f"  Failed to add {alias}: {e}")
                await client.close()
                return

        logger.info("")
        logger.info("=" * 80)
        logger.info("Re-registration complete!")
        logger.info("=" * 80)
        if not dry_run:
            logger.info(f"Job submitted. Check {server_url}/admin/jobs for progress.")
        logger.info(f"Backup saved to: {backup_file}")

    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Re-register a single golden repository on CIDX server"
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
        "--alias",
        required=True,
        help="Repository alias to re-register (required)",
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

    args = parser.parse_args()

    # Run async main
    asyncio.run(
        reregister_single_repo(
            server_url=args.server_url,
            username=args.username,
            password=args.password,
            alias=args.alias,
            dry_run=args.dry_run,
            wait_seconds=args.wait_seconds,
        )
    )


if __name__ == "__main__":
    main()
