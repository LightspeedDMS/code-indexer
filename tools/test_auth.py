#!/usr/bin/env python3
"""Test authentication with AdminAPIClient."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from code_indexer.api_clients.admin_client import AdminAPIClient


async def test_auth():
    server_url = "https://codeindexer.lightspeedtools.cloud"
    credentials = {
        "username": "scriptrunner",
        "password": "HelloWorld123!",
    }

    print(f"Testing authentication to {server_url}")
    print(f"Username: {credentials['username']}")
    print(f"Password: {'*' * len(credentials['password'])}")

    client = AdminAPIClient(
        server_url=server_url,
        credentials=credentials,
    )

    try:
        # Try to list repos - this will trigger authentication
        print("\nAttempting to list repositories...")
        result = await client.list_golden_repositories()
        print(f"Success! Found {len(result.get('golden_repos', []))} repositories")
        return result
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        await client.close()


if __name__ == "__main__":
    result = asyncio.run(test_auth())
    if result:
        print("\nRepositories:")
        for repo in result.get("golden_repos", []):
            print(f"  - {repo['alias']}: {repo['repo_url']}")
