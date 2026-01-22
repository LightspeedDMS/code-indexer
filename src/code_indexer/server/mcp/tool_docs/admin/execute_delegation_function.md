---
name: execute_delegation_function
category: admin
required_permission: query_repos
tl_dr: Execute a delegation function by delegating to Claude Server.
---

Execute a delegation function by delegating to Claude Server. Creates an async job that can be polled for results. 

VALUE PROPOSITION: This tool enables AI to perform code analysis, reviews, and transformations on protected repositories WITHOUT exposing source code to you. The actual AI work happens on Claude Server which has direct repository access. You receive only the AI's response, not the source code - enabling secure workflows for compliance-sensitive codebases. 

WHEN TO USE: After discovering available functions via list_delegation_functions, use this tool to execute a specific function with the required parameters. 

SECURITY: Access is validated against the function's allowed_groups. Users can only execute functions that their group membership permits - enforcing organizational policies on what AI operations each user can perform. 

EXECUTION FLOW:
1. Validates user access (group membership vs allowed_groups)
2. Validates required parameters are provided
3. Ensures required repositories are registered in Claude Server
4. Renders prompt template with parameters
5. Creates and starts job in Claude Server
6. Registers callback URL for completion notification
7. Returns job_id for async polling


ERRORS:
- 'Claude Delegation not configured' -> Delegation not set up
- 'Function not found' -> Invalid function_name
- 'Access denied' -> User not in allowed_groups
- 'Missing required parameter' -> Required param not provided
- 'Claude Server error' -> Communication error with Claude Server