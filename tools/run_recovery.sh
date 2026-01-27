#!/bin/bash
# Recover missing repositories
export CIDX_PASSWORD='HelloWorld123!'

echo "================================================================================"
echo "CIDX Repository Recovery"
echo "================================================================================"
echo "This will add back the 41 missing repositories from repos_backup.json"
echo "================================================================================"
echo ""

python3 tools/recover_missing_repos.py \
  --server-url https://codeindexer.lightspeedtools.cloud \
  --username scriptrunner \
  --password "$CIDX_PASSWORD" \
  --backup-file repos_backup.json \
  --wait-seconds 3
