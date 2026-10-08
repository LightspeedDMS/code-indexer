---
name: git_commit
category: git
required_permission: repository:write
tl_dr: Create a commit with staged changes.
slim_description: "Create a git commit from currently staged files, with a required message and an optional author_name; author email and committer identity come from your stored credential when a credential with an email is stored for the origin host, otherwise your account email (or <username>@cidx.local) is used and the committer equals the author."
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    message:
      type: string
      description: Commit message
    author_name:
      type: string
      description: Optional author name (letters, digits, space, hyphen, underscore). Overridden by the stored name of
        your git credential for the origin host when that credential has both a name and an email.
  required:
  - repository_alias
  - message
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Operation succeeded
    commit_hash:
      type: string
      description: Full 40-character commit SHA
    short_hash:
      type: string
      description: 7-character short commit SHA
    message:
      type: string
      description: Commit message
    author:
      type: string
      description: Commit author
    files_committed:
      type: array
      items:
        type: string
      description: List of files included in commit
---

TL;DR: Create a commit with staged changes. USE CASES: (1) Commit staged files, (2) Create checkpoint with message, (3) Record changes with attribution. REQUIREMENTS: Must have staged files. Staged `.code-indexer/` files or `.code-indexer-override.yaml` block the commit.

IDENTITY: When a git credential (configure_git_credential) exists for the forge host of the origin remote and carries an email, that email is the author email and the committer email; the credential's name, when stored, is both the author name and the committer name, and when the credential has no name the committer name falls back to the author name (author_name or your username). Otherwise the author email is your account email (or `<username>@cidx.local`), the author name is author_name or your username, and the committer equals the author. There is no author_email parameter.

MESSAGE: The server appends two trailers to your message: `Actual-Author: <author email>` and `Committed-Via: CIDX API`. Lines in your message that start with either trailer key are removed first.

PERMISSIONS: Requires repository:write.

EXAMPLE: {"repository_alias": "my-repo", "message": "Fix authentication bug", "author_name": "Example Dev"} Returns: {"success": true, "commit_hash": "<40-character SHA>", "message": "Fix authentication bug", "author": "dev@example.com", "committer": "dev@example.com"}
