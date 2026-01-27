#!/bin/bash
# Test re-registration with a single small repository
export CIDX_PASSWORD='HelloWorld123!'

# Pick a small library to test with
REPO_ALIAS="simple-cache"

echo "Testing with repository: $REPO_ALIAS"
echo ""

# First, dry run
echo "=== DRY RUN ==="
python3 tools/reregister_single_repo.py \
  --server-url https://codeindexer.lightspeedtools.cloud \
  --username scriptrunner \
  --password "$CIDX_PASSWORD" \
  --alias "$REPO_ALIAS" \
  --dry-run

echo ""
echo ""
read -p "Dry run complete. Run for REAL? (yes/no): " confirm

if [ "$confirm" = "yes" ]; then
    echo ""
    echo "=== LIVE EXECUTION ==="
    python3 tools/reregister_single_repo.py \
      --server-url https://codeindexer.lightspeedtools.cloud \
      --username scriptrunner \
      --password "$CIDX_PASSWORD" \
      --alias "$REPO_ALIAS"
else
    echo "Cancelled."
fi
