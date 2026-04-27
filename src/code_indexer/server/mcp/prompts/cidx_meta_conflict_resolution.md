---
name: cidx-meta conflict resolution
purpose: Resolve git rebase conflicts in the mutable cidx-meta repository.
---
Resolve the git rebase conflicts for the mutable cidx-meta repository at `{repo_path}`.

Branch: `{branch}`

Conflicted files:
{conflict_files}

Instructions:
- Use cidx-local MCP tools, including search on `cidx-meta-global`, to understand the intended content and surrounding context before editing.
- Edit the conflicted files in place and remove all git conflict markers.
- Preserve valid local and remote changes when they do not conflict semantically.
- Prefer the simplest correct merged result for cidx-meta metadata files.
- Do not leave any file unmerged.
