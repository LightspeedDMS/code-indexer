---
name: list_global_repos
category: repos
required_permission: query_repos
tl_dr: List all globally accessible repositories.
---

List all globally accessible repositories. Returns repos registered via add_golden_repo, immediately queryable as '{name}-global' alias.

TERMINOLOGY: Golden repositories are admin-registered source repos. Global repositories are the publicly queryable versions accessible via '{name}-global' alias.

ABOUT cidx-meta-global: This repository appears in the list but is NOT a code repository. It is a synthetic discovery repository containing AI-generated markdown descriptions of all other registered repositories. Search it first to find which repository covers your topic. See cidx_quick_reference for the full discovery workflow.

DISCOVERY PATTERN: Before listing all repos, search 'cidx-meta-global' to discover which repositories are relevant to your topic: search_code('authentication', repository_alias='cidx-meta-global') returns repos that handle authentication, then query those specific repos for detailed code.

Use list_global_repos() only when explicitly asked for the full repo list or to verify a repo exists. For detailed status of one repo (temporal support, refresh times), use global_repo_status instead.