---
name: list_repositories
category: repos
required_permission: query_repos
tl_dr: Lists YOUR activated repositories (user-specific workspaces).
---

Lists YOUR activated repositories (user-specific workspaces). Shows both single-repo activations and composite repositories you've created with user_alias, current_branch, and activation status. Does NOT show global repositories (use list_global_repos for that).

KEY DIFFERENCE FROM list_global_repos:
- list_repositories: YOUR activated repos (editable, user-specific, custom branches)
- list_global_repos: Shared repos (read-only, available to all users, default branches)

USE CASES: See which repositories you've activated for editing or branch-specific work. Find your custom repository aliases to use in file CRUD or git operations. Check if you have an activation before trying to edit files. Empty list means you haven't activated any repositories yet - use activate_repository first.
