---
name: create_pull_request
category: admin
required_permission: repository:write
tl_dr: Create a GitHub pull request or GitLab merge request.
---

TL;DR: Create a GitHub pull request or GitLab merge request. The forge type is auto-detected from the repository's remote URL. REQUIRES write mode to be active. USE CASES: (1) Open a PR/MR after committing and pushing changes, (2) Create review requests for feature branches. WORKFLOW: enter_write_mode -> create_file/edit_file -> git_stage -> git_commit -> git_push -> create_pull_request -> exit_write_mode. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "title": "Add new feature", "body": "Implements...", "head": "feature/my-branch", "base": "main"} Returns: {"success": true, "pr_url": "https://github.com/org/repo/pull/42", "pr_number": 42, "forge_type": "github"}
