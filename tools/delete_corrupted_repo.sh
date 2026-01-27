#!/bin/bash
# Delete the corrupted repo entry from the database
export CIDX_PASSWORD='HelloWorld123!'

echo "Deleting corrupted repo: lightspeeddms-private-internal-services-dmwservice"

python3 -c "
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'src'))

from code_indexer.api_clients.admin_client import AdminAPIClient

async def delete_repo():
    client = AdminAPIClient(
        server_url='https://codeindexer.lightspeedtools.cloud',
        credentials={'username': 'scriptrunner', 'password': '$CIDX_PASSWORD'}
    )
    try:
        await client.delete_golden_repository('lightspeeddms-private-internal-services-dmwservice', force=True)
        print('✓ Successfully deleted corrupted repo')
    except Exception as e:
        print(f'Error: {e}')
    finally:
        await client.close()

asyncio.run(delete_repo())
"
