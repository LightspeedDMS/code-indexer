---
name: feedback_versioned_path_trap
description: _resolve_golden_repo_path returns VERSIONED SNAPSHOT path — NEVER write to it. Use base clone resolution for any write operations.
type: feedback
---

_resolve_golden_repo_path() returns the VERSIONED SNAPSHOT path (from alias JSON target_path). This path is IMMUTABLE per CLAUDE.md architecture. NEVER write config, metadata, or any file to a .versioned/ path.

**Why:** Multiple bugs caused by writing to versioned snapshots instead of base clones. The written data gets lost on next refresh when a new snapshot replaces the old one. This has happened repeatedly and is a critical architectural violation.

**How to apply:**
- Any code that needs to WRITE to a repo's config/metadata MUST resolve to the base clone path first
- Use the same resolution logic as `_provider_index_job` lines 16624-16632: detect `.versioned` in path parts, extract alias, construct `golden_repos_dir / alias_name`
- `_resolve_golden_repo_path()` documentation MUST be updated to warn callers that the returned path is READ-ONLY (versioned snapshot)
- When reviewing code that calls `_resolve_golden_repo_path()`, always check if the caller writes to the returned path — if yes, it's a bug
