#!/usr/bin/env python3
"""
List all golden repositories on the CIDX server.

Usage:
    python3 tools/list_repos.py --server-url http://localhost:8000 --username admin --password <password>
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from code_indexer.api_clients.admin_client import AdminAPIClient


async def list_repos(server_url: str, username: str, password: str):
    """List all golden repositories."""
    client = AdminAPIClient(
        server_url=server_url,
        credentials={"username": username, "password": password},
    )

    try:
        # List repos (authentication happens automatically)
        result = await client.list_golden_repositories()
        # API returns "golden_repositories" not "golden_repos"
        repos = result.get("golden_repositories", result.get("golden_repos", []))

        print(f"\nFound {len(repos)} repositories:\n")
        for repo in repos:
            print(f"Alias: {repo['alias']}")
            print(f"  URL: {repo['repo_url']}")
            print(f"  Branch: {repo.get('default_branch', 'main')}")
            print(f"  Created: {repo.get('created_at', 'unknown')}")

            # Get indexes
            try:
                indexes = await client.get_golden_repo_indexes(repo['alias'])
                print(f"  Indexes:")
                print(f"    Semantic: {indexes.get('has_semantic_index', False)}")
                print(f"    FTS: {indexes.get('has_fts_index', False)}")
                print(f"    Temporal: {indexes.get('has_temporal_index', False)}")
                print(f"    SCIP: {indexes.get('has_scip_index', False)}")
            except Exception as e:
                print(f"  Indexes: Error - {e}")

            print()
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="List golden repositories")
    parser.add_argument("--server-url", default="http://localhost:8000")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True)

    args = parser.parse_args()

    asyncio.run(list_repos(args.server_url, args.username, args.password))


if __name__ == "__main__":
    main()
