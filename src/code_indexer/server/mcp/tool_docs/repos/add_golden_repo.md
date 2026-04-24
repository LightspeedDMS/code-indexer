---
name: add_golden_repo
category: repos
required_permission: manage_golden_repos
tl_dr: Register a new repository for indexing (ASYNC operation).
---

Register a new repository for indexing (ASYNC operation). Returns immediately but indexing runs in background.

WORKFLOW: (1) Call add_golden_repo(url, alias), (2) Poll get_job_statistics() until active=0 and pending=0, (3) Repository becomes available as '{alias}-global' for querying.

NAMING: Use descriptive aliases; '-global' suffix added automatically. NAMING WARNING: Avoid aliases that already end in '-global' as this creates confusing double-suffixed names like 'myrepo-global-global'.

TEMPORAL: Set enable_temporal=true to index git history for time-based searches. Indexing time ranges from seconds (small repos) to hours (very large repos). Monitor progress with get_job_statistics.