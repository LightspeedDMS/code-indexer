---
name: list_delegation_functions
category: admin
required_permission: query_repos
tl_dr: List available delegation functions for the current user.
---

List available delegation functions for the current user. Returns functions from the configured delegation repository that the user has access to based on their group membership. 

VALUE PROPOSITION: Claude Delegation allows AI to work on repositories that are NOT directly exposed to this MCP client. Source code stays protected on Claude Server - you only see the AI's response. This enables secure code analysis, reviews, and transformations on protected codebases without exposing source code. 

WHEN TO USE: When you need to discover what pre-approved AI workflows are available for working with protected source code that you cannot access directly. 

GROUP SECURITY: Functions are filtered by group membership - you only see functions whose allowed_groups include at least one of your groups. This provides fine-grained access control over what AI operations each user can perform. 

RETURNS: List of functions with name, description, and parameters. 

IMPERSONATION: When an admin is impersonating another user, the impersonated user's groups are used for filtering, not the admin's groups. 

ERRORS:
- 'Claude Delegation not configured' -> Delegation feature not set up by admin
- Empty functions list -> User has no accessible functions or repo is empty