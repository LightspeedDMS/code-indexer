---
name: add_golden_repo
category: repos
required_permission: manage_golden_repos
tl_dr: Register a new repository for indexing (ASYNC operation).
---

Register a new repository for indexing (ASYNC operation). Returns immediately but indexing runs in background. WORKFLOW: (1) Call add_golden_repo(url, alias), (2) Poll get_job_statistics() until active=0 and pending=0, (3) Repository becomes available as '{alias}-global' for querying. NAMING: Use descriptive aliases; '-global' suffix added automatically for global access. NAMING WARNING: Avoid aliases that already end in '-global' as this creates confusing double-suffixed names like 'myrepo-global-global'. TEMPORAL: Set enable_temporal=true to index git history for time-based searches. PERFORMANCE EXPECTATIONS: Small repos (<1K files): seconds to minutes. Medium repos (1K-10K files): 1-5 minutes. Large repos (10K-100K files): 5-30 minutes. Very large repos (>100K files, multi-GB): 30 minutes to hours. Monitor progress with get_job_statistics. If job stays in PENDING/RUNNING for 2x expected time, check server logs.