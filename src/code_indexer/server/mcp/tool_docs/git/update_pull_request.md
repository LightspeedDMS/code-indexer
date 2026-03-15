---
name: update_pull_request
category: git
required_permission: query_repos
tl_dr: Update metadata of a GitHub pull request or GitLab merge request. Supports updating title, description, labels, assignees, and reviewers. At least one field must be provided.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    number:
      type: integer
      description: PR/MR number
    title:
      type: string
      description: New title (optional)
    description:
      type: string
      description: New description/body (optional)
    labels:
      type: array
      items:
        type: string
      description: List of label names to set (optional)
    assignees:
      type: array
      items:
        type: string
      description: List of usernames to assign (optional)
    reviewers:
      type: array
      items:
        type: string
      description: List of reviewer usernames to request (optional; GitHub only via separate endpoint)
  required:
    - repository_alias
    - number
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    url:
      type: string
      description: URL of the updated pull/merge request
    updated_fields:
      type: array
      items:
        type: string
      description: Sorted list of field names that were updated
    forge_type:
      type: string
      description: Detected forge type ('github' or 'gitlab')
    error:
      type: string
      description: Error message on failure
---

TL;DR: Update metadata of a GitHub pull request or GitLab merge request. Forge type is auto-detected from the repository remote URL. Credentials are auto-fetched from stored git credentials. At least one of title, description, labels, assignees, or reviewers must be provided. GITHUB: Uses PATCH /repos/{owner}/{repo}/pulls/{number} for title/body/labels/assignees. Reviewers use a separate POST to /pulls/{number}/requested_reviewers. GITLAB: Uses PUT /projects/{path}/merge_requests/{number}. Labels are sent as a comma-separated string (GitLab API requirement). Reviewers are not supported for GitLab v1 (pass assignees instead). LIMITATION: GitLab username-to-ID resolution for assignees is not performed; pass usernames directly if supported by your GitLab version. EXAMPLE: {"repository_alias": "my-repo", "number": 42, "title": "Fix authentication bug", "labels": ["bug", "priority-1"]} Returns: {"success": true, "url": "https://github.com/org/repo/pull/42", "updated_fields": ["labels", "title"], "forge_type": "github"}
