#!/usr/bin/env python3
"""
Clean up orphaned repository directories on the CIDX server.

These are directories that exist on disk but aren't registered in the database,
which cause git clone failures when trying to re-register.

Usage:
    python3 tools/cleanup_orphaned_repos.py \\
      --server-url https://codeindexer.lightspeedtools.cloud \\
      --username scriptrunner \\
      --password <password> \\
      --ssh-host <server-hostname> \\
      --ssh-user <ssh-username> \\
      --batch-size 5
"""

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, List

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from code_indexer.api_clients.admin_client import AdminAPIClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def run_ssh_command(host: str, user: str, command: str) -> str:
    """Execute command on remote server via SSH."""
    ssh_cmd = ["ssh", f"{user}@{host}", command]
    result = subprocess.run(
        ssh_cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise Exception(f"SSH command failed: {result.stderr}")
    return result.stdout


async def cleanup_and_register(
    server_url: str,
    username: str,
    password: str,
    ssh_host: str,
    ssh_user: str,
    backup_file: str = "repos_backup.json",
    batch_size: int = 5,
    exclude_repos: List[str] = None,
) -> None:
    """Clean up orphaned directories and re-register repositories."""
    if exclude_repos is None:
        exclude_repos = []

    logger.info("=" * 80)
    logger.info("CIDX Repository Cleanup & Re-registration")
    logger.info("=" * 80)
    logger.info(f"Server: {server_url}")
    logger.info(f"SSH: {ssh_user}@{ssh_host}")
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

        # Process each repository: cleanup disk, then add
        success_count = 0
        failed_count = 0

        for i, repo in enumerate(batch, 1):
            alias = repo["alias"]
            repo_url = repo["repo_url"]
            default_branch = repo.get("default_branch", "main")

            logger.info(f"[{i}/{len(batch)}] Processing: {alias}")
            logger.info(f"  URL: {repo_url}")
            logger.info(f"  Branch: {default_branch}")

            # Step 1: Remove orphaned directory on server (if exists)
            try:
                repo_dir = f"/opt/code-indexer/.cidx-server/data/golden-repos/{alias}"
                cleanup_cmd = f"sudo rm -rf {repo_dir}"
                logger.info(f"  Cleaning up orphaned directory: {repo_dir}")
                run_ssh_command(ssh_host, ssh_user, cleanup_cmd)
                logger.info(f"  ✓ Cleaned up orphaned directory")
            except Exception as e:
                logger.warning(f"  Cleanup failed (may not exist): {e}")

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
                    await asyncio.sleep(3)

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
            logger.info(f"  python3 tools/cleanup_orphaned_repos.py \\")
            logger.info(f"    --server-url {server_url} \\")
            logger.info(f"    --username {username} \\")
            logger.info(f"    --password <password> \\")
            logger.info(f"    --ssh-host {ssh_host} \\")
            logger.info(f"    --ssh-user {ssh_user} \\")
            logger.info(f"    --batch-size {batch_size}")

    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Clean up orphaned directories and re-register repositories"
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
        "--ssh-host",
        required=True,
        help="SSH hostname for CIDX server (required)",
    )
    parser.add_argument(
        "--ssh-user",
        required=True,
        help="SSH username for CIDX server (required)",
    )
    parser.add_argument(
        "--backup-file",
        default="repos_backup.json",
        help="Backup file path (default: repos_backup.json)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Number of repositories to process per batch (default: 5)",
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
        cleanup_and_register(
            server_url=args.server_url,
            username=args.username,
            password=args.password,
            ssh_host=args.ssh_host,
            ssh_user=args.ssh_user,
            backup_file=args.backup_file,
            batch_size=args.batch_size,
            exclude_repos=args.exclude_repos or [],
        )
    )


if __name__ == "__main__":
    main()
