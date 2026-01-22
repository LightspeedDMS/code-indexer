"""
Git Operations and File CRUD Integration Tests.

Comprehensive test suite for CIDX git operations and file CRUD operations
exposed via MCP/REST APIs. Tests cover 27 operations (23 git + 4 file CRUD)
using real external repository: git@github.com:LightspeedDMS/VivaGoals-to-pptx.git

Test Categories:
- F1: File CRUD (create_file, edit_file, delete_file, get_file_content)
- F2: Git Status/Inspection (git_status, git_diff, git_log)
- F3: Git Staging/Commit (git_stage, git_unstage, git_commit)
- F4: Git Remote Operations (git_push, git_pull, git_fetch)
- F5: Git Recovery (git_reset, git_clean, git_merge_abort, git_checkout_file)
- F6: Git Branch Management (git_branch_list, git_branch_create, git_branch_switch, git_branch_delete)

Key Features:
- Idempotent tests with automatic rollback to original state
- Branch-based isolation for test execution
- Confirmation token handling for destructive operations
- Script-based execution suitable for CI/CD
- NO Python mocks - all operations are REAL git commands

Author: CIDX Testing Epic
"""
