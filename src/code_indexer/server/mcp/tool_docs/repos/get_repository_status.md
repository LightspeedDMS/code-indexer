---
name: get_repository_status
category: repos
required_permission: query_repos
tl_dr: Get comprehensive status of YOUR activated repository including indexing state,
  file counts, git branch info, and temporal capabilities.
---

Get comprehensive status of YOUR activated repository including indexing state, file counts, git branch info, and temporal capabilities. Returns activation_status, file_count, index_size, branches_list, enable_temporal, and last_updated fields.

REQUIRES: User-specific repository activation (use your custom alias, NOT '-global' suffix). For global read-only repositories, use global_repo_status instead. Check enable_temporal field to confirm if time_range queries are supported in search_code.