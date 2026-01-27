# Re-register All Repositories Script

This script programmatically removes and re-adds all golden repositories on a CIDX server.

## Use Cases

- After server migration or upgrade
- To rebuild all repository metadata
- To clear corrupted state and start fresh
- For bulk repository reconfiguration

## How It Works

1. Lists all current golden repositories
2. Backs up metadata to `repos_backup.json`
3. Deletes all repositories (preserving metadata)
4. Re-adds all repositories with saved metadata

## Usage

### Dry Run (Recommended First)

```bash
python3 tools/reregister_all_repos.py \
  --server-url http://localhost:8000 \
  --username admin \
  --password YOUR_PASSWORD \
  --dry-run
```

### Live Execution

```bash
python3 tools/reregister_all_repos.py \
  --server-url http://localhost:8000 \
  --username admin \
  --password YOUR_PASSWORD
```

### With Custom Wait Time

```bash
python3 tools/reregister_all_repos.py \
  --server-url http://localhost:8000 \
  --username admin \
  --password YOUR_PASSWORD \
  --wait-seconds 5
```

## Options

- `--server-url URL`: CIDX server URL (default: http://localhost:8000)
- `--username USER`: Admin username (default: admin)
- `--password PASS`: Admin password (required)
- `--dry-run`: Show what would be done without making changes
- `--wait-seconds N`: Seconds to wait between operations (default: 2)

## Output

The script creates a backup file `repos_backup.json` containing all repository metadata before making any changes.

Example backup:
```json
[
  {
    "alias": "myrepo",
    "repo_url": "https://github.com/user/repo.git",
    "default_branch": "main",
    "clone_path": "/path/to/clone",
    "created_at": "2025-01-23T10:00:00Z",
    "enable_temporal": false,
    "temporal_options": null,
    "indexes": {
      "has_semantic": true,
      "has_fts": true,
      "has_temporal": false,
      "has_scip": false
    }
  }
]
```

## Alternative: Python Script

You can also use the AdminAPIClient directly in your own scripts:

```python
import asyncio
from code_indexer.api_clients.admin_client import AdminAPIClient

async def main():
    # Create client
    client = AdminAPIClient(
        server_url="http://localhost:8000",
        credentials={"username": "admin", "password": "your_password"}
    )

    # Authenticate
    await client.authenticate()

    # List repos
    result = await client.list_golden_repositories()
    repos = result["golden_repos"]

    # Delete and re-add each repo
    for repo in repos:
        alias = repo["alias"]
        repo_url = repo["repo_url"]
        branch = repo.get("default_branch", "main")

        # Delete
        await client.delete_golden_repository(alias, force=True)

        # Re-add
        await client.add_golden_repository(
            git_url=repo_url,
            alias=alias,
            default_branch=branch
        )

asyncio.run(main())
```

## Safety Features

- Dry run mode to preview changes
- Automatic backup of metadata before deletion
- Wait intervals to avoid overwhelming the server
- Error handling for each operation

## Notes

- Requires admin credentials
- All repositories will be temporarily unavailable during re-registration
- Background jobs are created for each add operation
- You may need to wait for indexing jobs to complete after re-registration
- The backup file can be used for manual recovery if needed
