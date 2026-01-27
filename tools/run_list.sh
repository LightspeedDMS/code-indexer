#!/bin/bash
# List repositories
export CIDX_PASSWORD='HelloWorld123!'

python3 tools/list_repos.py \
  --server-url https://codeindexer.lightspeedtools.cloud \
  --username scriptrunner \
  --password "$CIDX_PASSWORD"
