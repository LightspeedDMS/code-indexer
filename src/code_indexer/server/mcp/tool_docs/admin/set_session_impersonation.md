---
name: set_session_impersonation
category: admin
required_permission: query_repos
tl_dr: '[ADMIN ONLY] Set or clear session impersonation to execute queries on behalf
  of another user.'
---

TL;DR: [ADMIN ONLY] Set or clear session impersonation to execute queries on behalf of another user. 

WHAT IT DOES:
Allows ADMIN users to assume another user's identity for the duration of their MCP session. All subsequent tool calls will use the target user's permissions until impersonation is cleared.

USE CASES:
(1) Support/helpdesk: Debug access issues by seeing what a user can see
(2) Delegated queries: claude.ai integrations executing on behalf of end users
(3) Testing: Verify permission configurations for specific users

IMPORTANT SECURITY NOTES:
- Only ADMIN users can impersonate
- Impersonation CONSTRAINS permissions to the target user's level
- Admins CANNOT elevate permissions through impersonation
- All actions while impersonating are audit logged with both original actor and impersonated user

SETTING IMPERSONATION:
set_session_impersonation(username='target_user')
-> All subsequent calls use target_user's permissions

CLEARING IMPERSONATION:
set_session_impersonation(username=null)
-> Restores original ADMIN permissions

RETURNS:
{
  "status": "ok",
  "impersonating": "target_user" // or null if cleared
}

ERRORS:
- 'Impersonation requires ADMIN role' -> Only admins can impersonate
- 'User not found: xyz' -> Target username doesn't exist
