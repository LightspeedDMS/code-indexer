#!/bin/bash
# Recover missing repositories with longer wait times
export CIDX_PASSWORD='HelloWorld123!'

echo "================================================================================"
echo "CIDX Repository Recovery (Slow Mode)"
echo "================================================================================"
echo "This will add back the 40 missing repositories with 5-second waits"
echo "================================================================================"
echo ""

python3 tools/recover_missing_repos.py \
  --server-url https://codeindexer.lightspeedtools.cloud \
  --username scriptrunner \
  --password "$CIDX_PASSWORD" \
  --backup-file repos_backup.json \
  --wait-seconds 5
