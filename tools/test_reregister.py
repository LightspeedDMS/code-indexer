#!/usr/bin/env python3
"""Test reregister script with hardcoded credentials."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Import the reregister function
from tools.reregister_all_repos import reregister_all_repos


async def main():
    await reregister_all_repos(
        server_url="https://codeindexer.lightspeedtools.cloud",
        username="scriptrunner",
        password="HelloWorld123!",
        dry_run=True,
        wait_seconds=2,
        exclude_repos=["meta"],
    )


if __name__ == "__main__":
    asyncio.run(main())
