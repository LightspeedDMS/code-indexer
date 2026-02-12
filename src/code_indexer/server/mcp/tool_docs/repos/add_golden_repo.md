---
name: add_golden_repo
category: repos
required_permission: manage_golden_repos
tl_dr: Register a new repository for indexing (ASYNC operation).
inputSchema:
  type: object
  properties:
    url:
      type: string
      description: Repository URL
    alias:
      type: string
      description: Repository alias
    branch:
      type: string
      description: Default branch (optional)
    enable_temporal:
      type: boolean
      default: false
      description: 'Enable temporal indexing (git history search). When true, repository is indexed with --index-commits flag
        to support time-based queries. Default: false for backward compatibility.'
    temporal_options:
      type: object
      description: Temporal indexing configuration options. Only used when enable_temporal=true.
      properties:
        max_commits:
          type: integer
          description: Maximum number of commits to index. Omit for all commits.
          minimum: 1
        since_date:
          type: string
          description: 'Only index commits after this date (format: YYYY-MM-DD).'
        diff_context:
          type: integer
          default: 5
          description: 'Number of context lines in diffs. Default: 5. Higher values increase storage.'
          minimum: 0
          maximum: 50
  required:
  - url
  - alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    job_id:
      type:
      - string
      - 'null'
      description: Background job ID for tracking indexing progress
    message:
      type: string
      description: Human-readable status message
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Register a new repository for indexing (ASYNC operation). Returns immediately but indexing runs in background.

WORKFLOW: (1) Call add_golden_repo(url, alias), (2) Poll get_job_statistics() until active=0 and pending=0, (3) Repository becomes available as '{alias}-global' for querying.

NAMING: Use descriptive aliases; '-global' suffix added automatically. NAMING WARNING: Avoid aliases that already end in '-global' as this creates confusing double-suffixed names like 'myrepo-global-global'.

TEMPORAL: Set enable_temporal=true to index git history for time-based searches. Indexing time ranges from seconds (small repos) to hours (very large repos). Monitor progress with get_job_statistics.