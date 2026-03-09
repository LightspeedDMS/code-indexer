---
name: git_merge
category: git
required_permission: repository:write
tl_dr: Merge a source branch into the current branch with conflict detection.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias
    source_branch:
      type: string
      description: Branch to merge into current branch
  required:
  - repository_alias
  - source_branch
  additionalProperties: false
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether merge completed without conflicts
    merge_summary:
      type: string
      description: Git merge output summary
    conflicts:
      type: array
      description: List of conflicted files (empty if clean merge)
      items:
        type: object
        properties:
          file:
            type: string
          status:
            type: string
            description: "Git status code: UU (both modified), AA (both added), DD (both deleted)"
          conflict_type:
            type: string
            description: "Conflict type: content, add/add, modify/delete"
          is_binary:
            type: boolean
            description: Whether file is binary (no text conflict markers)
---

TL;DR: Merge a source branch into the current branch with detailed conflict detection. USE CASES: (1) Integrate upstream changes, (2) Merge feature branches, (3) Detect and list merge conflicts. REQUIREMENTS: Write mode must be active (use enter_write_mode first). PERMISSIONS: Requires repository:write. EXAMPLE: {"repository_alias": "my-repo", "source_branch": "feature/login"} Returns on success: {"success": true, "merge_summary": "..."} Returns on conflict: {"success": false, "conflicts": [{"file": "src/app.py", "status": "UU", "conflict_type": "content", "is_binary": false}]} After conflicts, use git_merge_abort to roll back to pre-merge state.
