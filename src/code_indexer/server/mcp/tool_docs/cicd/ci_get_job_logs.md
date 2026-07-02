---
name: ci_get_job_logs
category: cicd
required_permission: repository:read
tl_dr: Get full log output for a specific CI/CD job, auto-detecting the forge from the remote URL.
slim_description: "Get complete log output for a specific CI/CD job by job_id, auto-detecting GitHub Actions or GitLab CI."
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Golden repository alias name (e.g. 'myrepo-global')
    job_id:
      type: integer
      description: Job ID to retrieve logs for
    forge:
      type: string
      enum:
      - auto
      - github
      - gitlab
      default: auto
      description: "Force a specific forge type, or 'auto' to detect from remote URL"
  required:
  - repository_alias
  - job_id
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    repository_alias:
      type: string
    forge:
      type: string
    job_id:
      type: integer
    logs:
      type: string
      description: Full log text for the job
---

TL;DR: Get complete log output for a specific CI/CD job. Auto-detects GitHub Actions or GitLab CI from the repository remote URL. QUICK START: ci_get_job_logs(repository_alias='myrepo-global', job_id=67890). FORGE OVERRIDE: pass forge='github' or forge='gitlab' to skip auto-detection. job_id maps to GitHub Actions job_id or GitLab job_id. MIGRATION: replaces github_actions_get_job_logs(owner, repo, job_id) and gitlab_ci_get_job_logs(project_id, job_id).
