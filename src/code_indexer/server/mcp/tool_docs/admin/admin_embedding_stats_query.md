---
name: admin_embedding_stats_query
category: admin
required_permission: manage_users
tl_dr: Query embedding/reranker call tracking stats for vendor cost reconciliation.
slim_description: "Query embedding_call_stats records with optional filters for provider, purpose, golden_repo_alias, job_id, and time range."
inputSchema:
  type: object
  properties:
    provider:
      type: string
      description: 'Filter by provider: ''voyageai'' or ''cohere'''
      enum:
      - voyageai
      - cohere
    purpose:
      type: string
      description: 'Filter by purpose: ''index'', ''refresh'', ''query'', ''temporal'', ''key_test'', or ''cache_shadow_audit'''
    golden_repo_alias:
      type: string
      description: Filter by golden repo alias
    job_id:
      type: string
      description: Filter by job id
    start_time:
      type: number
      description: Filter to records with occurred_at >= this Unix timestamp
    end_time:
      type: number
      description: Filter to records with occurred_at < this Unix timestamp
    limit:
      type: integer
      description: Maximum records to return (1-1000, default 200)
    offset:
      type: integer
      description: Pagination offset (default 0)
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation success status
    records:
      type: array
      description: Array of embedding_call_stats records
      items:
        type: object
        properties:
          provider:
            type: string
          call_type:
            type: string
          model:
            type: string
          item_count:
            type: integer
          token_count:
            type: integer
          batch_size:
            type: integer
          purpose:
            type: string
          success:
            type: boolean
          latency_ms:
            type: integer
          occurred_at:
            type: number
          golden_repo_alias:
            type:
            - string
            - 'null'
          job_id:
            type:
            - string
            - 'null'
          node_id:
            type:
            - string
            - 'null'
    count:
      type: integer
      description: Number of records returned
  required:
  - success
  - records
  - count
---

Query embedding/reranker call tracking stats (embedding_call_stats table) for vendor cost reconciliation. USE CASES: (1) Reconcile actual vendor-billed calls against invoices, (2) Audit call volume by provider/purpose/golden-repo/job, (3) Investigate a specific indexing job's embedding costs. RETURNS: Array of embedding_call_stats records with provider, call_type, model, item_count, token_count, batch_size, purpose, success, latency_ms, occurred_at, golden_repo_alias, job_id, node_id. PERMISSIONS: Requires admin role (admin only).

ERRORS:
- Permission denied: non-admin user attempted to call this tool

EXAMPLE: {"provider": "voyageai", "purpose": "index", "limit": 100}
