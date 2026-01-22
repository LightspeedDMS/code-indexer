---
name: git_checkout_file
category: git
required_permission: repository:write
tl_dr: Discard local changes and restore file to HEAD.
---

TL;DR: Discard local changes and restore file to HEAD. Restore file to HEAD version (discard local changes). USE CASES: (1) Discard unwanted changes, (2) Restore deleted file, (3) Reset file to last commit. SAFETY: This discards local modifications to the file. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "file_path": "src/file.py"} Returns: {"success": true, "restored_files": ["src/file.py"]}