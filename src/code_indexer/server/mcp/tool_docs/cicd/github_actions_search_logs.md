---
name: github_actions_search_logs
category: cicd
required_permission: repository:read
tl_dr: Search GitHub Actions workflow run logs for a pattern using regex matching.
inputSchema:
  type: object
  properties:
    owner:
      type: string
      description: Repository owner
    repo:
      type: string
      description: Repository name
    run_id:
      type: integer
      description: Workflow run ID
    query:
      type: string
      description: Search query string (case-insensitive regex)
  required:
  - owner
  - repo
  - run_id
  - query
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

TL;DR: Search GitHub Actions workflow run logs for a pattern using regex matching. QUICK START: github_actions_search_logs(owner='user', repo='project', run_id=12345, query='error') finds errors in logs. USE CASES: (1) Find error messages in logs, (2) Search for specific patterns, (3) Debug workflow failures. RETURNS: List of matching log lines with job_id, job_name, line, line_number. PERMISSIONS: Requires repository:read. EXAMPLE: github_actions_search_logs(owner='user', repo='project', run_id=12345, query='ERROR|FAIL')