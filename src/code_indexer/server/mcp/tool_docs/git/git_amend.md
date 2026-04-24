---
name: git_amend
category: git
required_permission: repository:write
tl_dr: Amend the most recent git commit.
---

TL;DR: Amend the most recent git commit. If a message is provided, replaces the commit message. If no message is provided, keeps the existing message (equivalent to git commit --amend --no-edit). Author and committer identity are taken from stored PAT credentials. USE CASES: (1) Fix a typo in the last commit message, (2) Add forgotten staged changes to the last commit without changing the message, (3) Update author metadata on the last commit. WORKFLOW: git_stage (stage additional files if needed) -> git_amend -> git_push (with --force if already pushed). WARNING: Amending rewrites history. If the commit was already pushed to a shared branch, a force push will be needed. PERMISSIONS: Requires repository:write. EXAMPLES: Fix message: {"repository_alias": "my-repo", "message": "Fix: corrected typo in feature implementation"} -> {"success": true, "commit_hash": "abc123def"}. Keep message: {"repository_alias": "my-repo"} -> {"success": true, "commit_hash": "newhash456"}
