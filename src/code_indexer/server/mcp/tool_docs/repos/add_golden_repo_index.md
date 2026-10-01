---
name: add_golden_repo_index
category: repos
required_permission: manage_golden_repos
tl_dr: '[ADMIN ONLY] Add an index type to an existing golden repository. Requires MCP elevation when enforcement is on.'
slim_description: "[ADMIN ONLY] Add an index type (semantic, fts, temporal, or scip) to an existing golden repository identified by alias. Requires MCP elevation (TOTP step-up) when enforcement is on."
inputSchema:
  type: object
  properties:
    alias:
      type: string
      description: Golden repository alias (base name, not '-global' suffix)
    index_type:
      type: string
      enum:
      - semantic
      - fts
      - temporal
      - scip
      description: 'Index type to add: ''semantic'' for embedding-based search, ''fts'' for full-text search, ''temporal''
        for git history search, ''scip'' for call graph navigation'
  required:
  - alias
  - index_type
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    job_id:
      type: string
      description: Background job ID for tracking progress
    message:
      type: string
      description: Status message with guidance on tracking progress
    error:
      type: string
      description: Error message if operation failed (alias not found, invalid type, or index already exists)
  required:
  - success
---

Add an index type to an existing golden repository. Submits a background job and returns job_id for tracking. INDEX TYPES: 'semantic' (embedding-based semantic search), 'fts' (Tantivy full-text search), 'temporal' (git history/time-based search), 'scip' (call graph for code navigation). WORKFLOW: (1) Call add_golden_repo_index with alias and index_type, (2) Returns job_id immediately, (3) Monitor progress via get_job_statistics. REQUIREMENTS: Repository must already exist as golden repo (use add_golden_repo first if needed). ERROR CASES: Returns error if alias not found, invalid index_type, or index already exists (idempotent). PERFORMANCE: Index addition runs in background - semantic/fts takes seconds to minutes, temporal depends on commit history size, scip depends on codebase complexity.

ELEVATION: Matches the REST twin (POST /api/admin/golden-repos/{alias}/indexes): requires the admin role and, when elevation enforcement is on, an active elevation window (call `elevate_session` first; MCP-credential and OAuth callers are elevated automatically).

ERRORS:
- elevation_required: TOTP step-up needed (only when elevation enforcement is on)
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)