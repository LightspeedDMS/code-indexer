#!/bin/bash
export CIDX_PASSWORD='HelloWorld123!'

# Pick a small library to test with
REPO_ALIAS="simple-cache"

echo "Testing re-registration with: $REPO_ALIAS"
echo ""

python3 tools/reregister_single_repo.py \
  --server-url https://codeindexer.lightspeedtools.cloud \
  --username scriptrunner \
  --password "$CIDX_PASSWORD" \
  --alias "$REPO_ALIAS" \
  "$@"
