---
name: list_git_credentials
category: admin
required_permission: query_repos
tl_dr: List all git forge credentials configured for your account.
---

TL;DR: List all git forge credentials configured for your account. Tokens are redacted (only last 4 characters shown).

USE CASES:
- View which forges you have credentials configured for
- Check credential identity details (username, email)

INPUTS: None

RETURNS:
- credentials: Array of credential objects with forge_type, forge_host, forge_username, git_user_name, git_user_email, name, token_suffix, created_at

SECURITY: You can only see your own credentials. Tokens are redacted.

EXAMPLE: {} Returns: {"success": true, "credentials": [{"forge_type": "github", "forge_host": "github.com", "forge_username": "octocat", "token_suffix": "ab12"}], "count": 1}
