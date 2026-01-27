#!/bin/bash
export CIDX_PASSWORD='HelloWorld123!'

python3 tools/reregister_all_repos.py \
  --server-url https://codeindexer.lightspeedtools.cloud \
  --username scriptrunner \
  --password "$CIDX_PASSWORD" \
  --exclude cidx-meta \
  --dry-run
