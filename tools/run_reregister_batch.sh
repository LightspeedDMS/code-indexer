#!/bin/bash
# Re-register repositories in batches (delete + re-add)
export CIDX_PASSWORD='HelloWorld123!'

# Default to batch size 2, or use first argument if provided
BATCH_SIZE=${1:-2}

python3 tools/reregister_batch.py \
  --server-url https://codeindexer.lightspeedtools.cloud \
  --username scriptrunner \
  --password "$CIDX_PASSWORD" \
  --batch-size "$BATCH_SIZE" \
  --wait-seconds 3 \
  --exclude cidx-meta
