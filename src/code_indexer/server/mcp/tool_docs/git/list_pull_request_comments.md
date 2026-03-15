---
name: list_pull_request_comments
category: git
required_permission: query_repos
tl_dr: Read all comments and review notes on a pull request / merge request. Returns both inline review comments and general conversation in unified format.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    number:
      type: integer
      description: PR/MR number
    limit:
      type: integer
      description: 'Max comments to return (default: 50)'
      default: 50
      minimum: 1
      maximum: 200
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
    comments:
      type: array
      description: List of unified comment objects
      items:
        type: object
        properties:
          id:
            type: integer
            description: Comment/note ID
          author:
            type: string
            description: Author username
          body:
            type: string
            description: Comment text
          created_at:
            type: string
            description: Creation timestamp
          updated_at:
            type: string
            description: Last update timestamp
          file_path:
            type: string
            description: File path for inline comments (null for general)
          line_number:
            type: integer
            description: Line number for inline comments (null for general)
          is_review_comment:
            type: boolean
            description: True for inline review comments, False for general conversation
          resolved:
            type: boolean
            description: Resolution status for resolvable comments (null if not applicable)
    forge_type:
      type: string
      description: Detected forge type ('github' or 'gitlab')
    count:
      type: integer
      description: Number of comments returned
    error:
      type: string
      description: Error message on failure
---

TL;DR: Read all comments on a GitHub pull request or GitLab merge request. Returns both inline review comments (attached to specific lines) and general conversation comments in a single unified list sorted by created_at. Forge type is auto-detected from the repository remote URL. Credentials are auto-fetched from stored git credentials. USE CASES: (1) Read all feedback on a PR before addressing review comments, (2) Check if inline review threads are resolved, (3) Get full conversation history for a PR/MR. GITHUB: Merges two API endpoints - pull request review comments (inline) and issue comments (general). resolved field is always null (not available in GitHub API). GITLAB: Fetches merge request notes, filters out system notes (status changes, assignments). resolved field reflects GitLab resolvable/resolved fields. EXAMPLE: {"repository_alias": "my-repo", "number": 42, "limit": 50} Returns: {"success": true, "comments": [{"id": 101, "author": "reviewer1", "body": "This needs error handling", "created_at": "2026-03-11T10:00:00Z", "updated_at": "2026-03-11T10:00:00Z", "file_path": "src/auth.py", "line_number": 42, "is_review_comment": true, "resolved": false}], "forge_type": "github", "count": 1}
