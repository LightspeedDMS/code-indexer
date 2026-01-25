---
name: gh_actions_search_logs
category: cicd
required_permission: repository:read
tl_dr: Search workflow run logs for a pattern using ripgrep-style matching.
inputSchema:
  type: object
  properties:
    repository:
      type: string
      description: Repository in 'owner/repo' format
    run_id:
      type: integer
      description: Workflow run ID
    pattern:
      type: string
      description: Pattern to search for (case-insensitive regex)
  required:
  - repository
  - run_id
  - pattern
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    matches:
      type: array
      items:
        type: object
        properties:
          job_id:
            type: integer
          job_name:
            type: string
          line:
            type: string
          line_number:
            type: integer
---

TL;DR: Search workflow run logs for a pattern using ripgrep-style matching. QUICK START: gh_actions_search_logs(repository='owner/repo', run_id=12345, pattern='error') finds errors in logs. USE CASES: (1) Find error messages in logs, (2) Search for specific patterns, (3) Debug workflow failures. RETURNS: List of matching log lines with job_id, job_name, line, line_number. PERMISSIONS: Requires repository:read. EXAMPLE: gh_actions_search_logs(repository='owner/repo', run_id=12345, pattern='ERROR|FAIL')