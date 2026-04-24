---
name: add_golden_repo_index
category: repos
required_permission: manage_golden_repos
tl_dr: Add an index type to an existing golden repository.
---

Add an index type to an existing golden repository. Submits a background job and returns job_id for tracking. INDEX TYPES: 'semantic' (embedding-based semantic search), 'fts' (Tantivy full-text search), 'temporal' (git history/time-based search), 'scip' (call graph for code navigation). WORKFLOW: (1) Call add_golden_repo_index with alias and index_type, (2) Returns job_id immediately, (3) Monitor progress via get_job_statistics. REQUIREMENTS: Repository must already exist as golden repo (use add_golden_repo first if needed). ERROR CASES: Returns error if alias not found, invalid index_type, or index already exists (idempotent). PERFORMANCE: Index addition runs in background - semantic/fts takes seconds to minutes, temporal depends on commit history size, scip depends on codebase complexity.