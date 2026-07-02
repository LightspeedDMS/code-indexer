---
name: ci_retry_run
category: cicd
required_permission: repository:write
tl_dr: Retry a failed CI/CD workflow or pipeline, auto-detecting the forge from the remote URL.
slim_description: "Retry a failed CI/CD workflow run or pipeline by run_id, auto-detecting GitHub Actions or GitLab CI. Requires personal git credential."
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Golden repository alias name (e.g. 'myrepo-global')
    run_id:
      type: integer
      description: Workflow run ID (GitHub) or pipeline ID (GitLab) to retry
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
  - run_id
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
    message:
      type: string
---

TL;DR: Retry a failed CI/CD workflow or pipeline. Auto-detects GitHub Actions or GitLab CI from the repository remote URL. REQUIRES: personal git credential configured via configure_git_credential (never uses global CI token). QUICK START: ci_retry_run(repository_alias='myrepo-global', run_id=12345). FORGE OVERRIDE: pass forge='github' or forge='gitlab' to skip auto-detection. AUDIT: all retry operations are logged with username and correlation_id. MIGRATION: replaces github_actions_retry_run(owner, repo, run_id) and gitlab_ci_retry_pipeline(project_id, pipeline_id).
