---
name: ci_get_run
category: cicd
required_permission: repository:read
tl_dr: Get detailed information for a specific CI/CD run or pipeline, auto-detecting the forge from the remote URL.
slim_description: "Get detailed information for a specific CI/CD workflow run or pipeline by run_id, auto-detecting GitHub Actions or GitLab CI."
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Golden repository alias name (e.g. 'myrepo-global')
    run_id:
      type: integer
      description: Workflow run ID (GitHub) or pipeline ID (GitLab)
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
    run:
      type: object
      description: Detailed run/pipeline information including jobs
---

TL;DR: Get detailed information for a specific CI/CD run or pipeline. Auto-detects GitHub Actions or GitLab CI from the repository remote URL. QUICK START: ci_get_run(repository_alias='myrepo-global', run_id=12345). FORGE OVERRIDE: pass forge='github' or forge='gitlab' to skip auto-detection. run_id maps to GitHub Actions run_id or GitLab pipeline_id. MIGRATION: replaces github_actions_get_run(owner, repo, run_id) and gitlab_ci_get_pipeline(project_id, pipeline_id).
