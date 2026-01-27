#!/bin/bash
# Live execution: Re-register all repositories (excluding cidx-meta)
export CIDX_PASSWORD='HelloWorld123!'

echo "================================================================================"
echo "CIDX Golden Repository Re-registration - LIVE EXECUTION"
echo "================================================================================"
echo "Server: https://codeindexer.lightspeedtools.cloud"
echo "User: scriptrunner"
echo "Excluding: cidx-meta"
echo "Total repositories to process: 58"
echo ""
echo "This will:"
echo "  1. Backup all repository metadata to repos_backup.json"
echo "  2. Delete all 58 repositories"
echo "  3. Re-add all 58 repositories"
echo ""
echo "Estimated time: 5-10 minutes"
echo "================================================================================"
echo ""
read -p "Are you SURE you want to proceed? Type 'yes' to confirm: " confirm

if [ "$confirm" != "yes" ]; then
    echo "Cancelled."
    exit 0
fi

echo ""
echo "Starting re-registration..."
echo ""

python3 tools/reregister_all_repos.py \
  --server-url https://codeindexer.lightspeedtools.cloud \
  --username scriptrunner \
  --password "$CIDX_PASSWORD" \
  --exclude cidx-meta \
  --wait-seconds 2

echo ""
echo "================================================================================"
echo "Re-registration complete!"
echo "================================================================================"
echo ""
echo "Next steps:"
echo "  1. Check https://codeindexer.lightspeedtools.cloud/admin/jobs for job progress"
echo "  2. Verify repositories at https://codeindexer.lightspeedtools.cloud/admin/golden-repos"
echo "  3. Backup saved to: repos_backup.json"
echo ""
