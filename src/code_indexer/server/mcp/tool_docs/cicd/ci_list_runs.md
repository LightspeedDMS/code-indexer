---
name: ci_list_runs
category: cicd
required_permission: repository:read
tl_dr: List CI/CD runs for a repository, auto-detecting GitHub Actions or GitLab CI from the remote URL.
slim_description: "List CI/CD workflow runs or pipelines for a golden-repo alias. Auto-detects GitHub or GitLab from the repository remote URL."
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Golden repository alias name (e.g. 'myrepo-global')
    forge:
      type: string
      enum:
      - auto
      - github
      - gitlab
      default: auto
      description: "Force a specific forge type, or 'auto' to detect from remote URL"
    branch:
      type: string
      description: Optional branch name filter
    status:
      type: string
      description: Optional status filter (e.g. completed, failed, running)
    limit:
      type: integer
      default: 20
      description: 'Maximum number of runs to return (default: 20)'
  required:
  - repository_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    repository_alias:
      type: string
    forge:
      type: string
    runs:
      type: array
      items:
        type: object
    count:
      type: integer
---

TL;DR: List CI/CD runs for a golden-repo alias. Auto-detects GitHub Actions or GitLab CI from the repository remote URL. QUICK START: ci_list_runs(repository_alias='myrepo-global') returns recent runs. FORGE OVERRIDE: pass forge='github' or forge='gitlab' to skip auto-detection. AUTO-DETECT FAILURE: if the remote URL hostname is not github.com or gitlab.com, pass forge explicitly. FILTERS: branch='main', status='completed', limit=20. MIGRATION: replaces github_actions_list_runs(owner, repo) and gitlab_ci_list_pipelines(project_id). repository_alias is the golden repo alias, not owner/repo.
