---
name: ci_search_logs
category: cicd
required_permission: repository:read
tl_dr: Search CI/CD run logs for a pattern, auto-detecting the forge from the remote URL.
slim_description: "Search CI/CD workflow run or pipeline logs for a pattern, auto-detecting GitHub Actions or GitLab CI."
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Golden repository alias name (e.g. 'myrepo-global')
    run_id:
      type: integer
      description: Workflow run ID (GitHub) or pipeline ID (GitLab) to search logs for
    pattern:
      type: string
      description: Search pattern (string or regex)
    forge:
      type: string
      enum:
      - auto
      - github
      - gitlab
      default: auto
      description: "Force a specific forge type, or 'auto' to detect from remote URL"
    case_sensitive:
      type: boolean
      default: true
      description: Whether the pattern search is case-sensitive (GitLab only)
  required:
  - repository_alias
  - run_id
  - pattern
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    repository_alias:
      type: string
    forge:
      type: string
    run_id:
      type: integer
    pattern:
      type: string
    matches:
      type: array
      items:
        type: object
        properties:
          job_id:
            type: integer
          job_name:
            type: string
          line_number:
            type: integer
          line_text:
            type: string
    count:
      type: integer
---

TL;DR: Search CI/CD run logs for a pattern. Auto-detects GitHub Actions or GitLab CI from the repository remote URL. QUICK START: ci_search_logs(repository_alias='myrepo-global', run_id=12345, pattern='ERROR'). FORGE OVERRIDE: pass forge='github' or forge='gitlab' to skip auto-detection. run_id maps to GitHub Actions run_id or GitLab pipeline_id. pattern renamed from 'query' (GitLab). MIGRATION: replaces github_actions_search_logs(owner, repo, run_id, query) and gitlab_ci_search_logs(project_id, pipeline_id, pattern).
