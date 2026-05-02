---
name: cidx-meta conflict resolution
purpose: Resolve git rebase conflicts in the mutable cidx-meta repository.
---
You must resolve git rebase conflicts in the cidx-meta repository at `{repo_path}` on branch `{branch}`.

CONFLICTED FILES:
{conflict_files}

CRITICAL: You MUST actually modify the files on disk and stage them with git. Do not respond with a description of what you would do — execute the resolution using your tools.

REQUIRED STEPS (do all of them in order):

1. For EACH conflicted file listed above:
   a. Use the Read tool on the file to see its current content. The file contains `<<<<<<< HEAD`, `=======`, and `>>>>>>> <branch>` conflict markers separating local (HEAD) and remote (branch) versions.
   b. Use the Edit tool to overwrite the file with a merged version that:
      - Removes ALL `<<<<<<< HEAD`, `=======`, and `>>>>>>> <branch>` marker lines (and the surrounding section delimiters).
      - Combines the local and remote sides into a single coherent result. For JSON files (e.g. `_domains.json`): parse, deep-merge keys; when both sides set the same key to different values, prefer the longer/more descriptive value, or combine them (concatenate descriptions with " / " or "; "). The output must be valid JSON. For markdown: combine sections without losing content from either side. For other text: prefer the union of changes when they don't semantically conflict.
      - Preserves all unchanged content outside the conflict markers exactly.
   c. Use the Bash tool to run `git -C {repo_path} add <relative-file-path>` to stage the resolved file.

2. After all files are resolved and staged, use the Bash tool to verify:
   - Run `git -C {repo_path} diff --name-only --diff-filter=U`. The output MUST be empty (no unmerged paths).
   - If the output is NOT empty, return to step 1 for those remaining files.

3. Reply with a single short confirmation line listing the files you resolved (e.g. `Resolved: file1.json, file2.md`).

GUIDELINES:
- The primary task is mechanical: remove conflict markers, write valid merged content, git-add the result. Use cidx-local MCP tools (e.g. search_code on `cidx-meta-global`) only when context is needed.
- For cidx-meta JSON files: read with the Read tool, parse, merge structurally, write valid JSON back with the Edit tool. Do not leave invalid JSON.
- Never leave any file in an unmerged state. The calling job WILL fail and abort the rebase if you do.
- Do not run `git rebase --continue` or `git commit` — the caller handles those after you finish.
