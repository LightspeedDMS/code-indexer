---
name: comment_on_pull_request
category: git
required_permission: query_repos
tl_dr: Add a comment to a GitHub pull request or GitLab merge request. Supports both general conversation comments and inline review comments attached to a specific file and line.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    number:
      type: integer
      description: PR/MR number
    body:
      type: string
      description: Comment text
    file_path:
      type: string
      description: File path for inline comment (optional; requires line_number)
    line_number:
      type: integer
      description: Line number for inline comment (required when file_path is provided)
  required:
    - repository_alias
    - number
    - body
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    comment_id:
      type: integer
      description: ID of the created comment
    url:
      type: string
      description: URL of the created comment
    forge_type:
      type: string
      description: Detected forge type ('github' or 'gitlab')
    error:
      type: string
      description: Error message on failure
---

TL;DR: Add a comment to a GitHub pull request or GitLab merge request. Forge type is auto-detected from the repository remote URL. Credentials are auto-fetched from stored git credentials. USE CASES: (1) Post a general review comment on a PR/MR, (2) Add an inline review comment on a specific file and line. GENERAL COMMENT: Omit file_path and line_number. INLINE COMMENT: Provide both file_path and line_number (line_number is required when file_path is given). GITHUB: General comments POST to /issues/{number}/comments; inline comments first fetch head.sha via GET /pulls/{number} then POST to /pulls/{number}/comments with commit_id, path, line, side=RIGHT. GITLAB: General comments POST to .../merge_requests/{number}/notes; inline comments first fetch diff_refs via GET .../merge_requests/{number} then POST notes with a position object. EXAMPLE (general): {"repository_alias": "my-repo", "number": 42, "body": "LGTM!"} EXAMPLE (inline): {"repository_alias": "my-repo", "number": 42, "body": "Consider extracting this", "file_path": "src/auth.py", "line_number": 55} Returns: {"success": true, "comment_id": 9001, "url": "https://github.com/org/repo/pull/42#issuecomment-9001", "forge_type": "github"}
