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
      description: 'Maximum number of runs to return (default: 20). Applied to GitHub results only; on GitLab the single
        page fetched from the forge is returned as is.'
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

TL;DR: List CI/CD runs for a golden-repo alias. Auto-detects GitHub Actions or GitLab CI from the repository remote URL. QUICK START: ci_list_runs(repository_alias='myrepo-global') returns recent runs. FORGE OVERRIDE: pass forge='github' or forge='gitlab' to skip auto-detection. AUTO-DETECT FAILURE: if the remote URL hostname is not github.com or gitlab.com, pass forge explicitly. FILTERS: branch='main', status (forge-native value, e.g. 'completed' on GitHub, 'failed' on GitLab). PAGING: one page of the most recent runs is fetched from the forge at its default page size. On GitHub that list is cut to limit (default 20); on GitLab the fetched page is returned without applying limit. The response also carries rate_limit (the forge's rate-limit headers from the call). repository_alias is the golden repo alias, not owner/repo.
