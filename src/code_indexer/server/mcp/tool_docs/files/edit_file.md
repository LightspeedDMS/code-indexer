---
name: edit_file
category: files
required_permission: repository:write
tl_dr: Edit existing file using exact string replacement with optimistic locking.
---

Edit existing file using exact string replacement with optimistic locking. USE CASES: (1) Update source code, (2) Modify configurations, (3) Fix bugs. OPTIMISTIC LOCKING: content_hash prevents concurrent edit conflicts - hash from get_file_content or previous edit. STRING REPLACEMENT: old_string must match exactly (including whitespace). Use replace_all=true to replace all occurrences. REQUIREMENTS: File must exist, content_hash must match current state. PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "file_path": "src/auth.py", "old_string": "def old_func():", "new_string": "def new_func():", "content_hash": "abc123def456"}