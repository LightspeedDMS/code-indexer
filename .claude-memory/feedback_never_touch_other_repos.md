---
name: never_touch_other_repos
description: NEVER modify files outside the assigned working directory — other clones have their own agents
metadata: 
  node_type: memory
  type: feedback
  originSessionId: d05fe9e8-c6f0-4e53-86ac-410b98b678ba
---

ABSOLUTE PROHIBITION: NEVER read, write, copy, restore, or modify ANY file outside the assigned working directory (the project root you were invoked in).

**Why:** Other clones in ~/Dev/ may have editable installs and active agents. Copying files to "sync" destroyed another agent's work. Attempting to "restore" with `git checkout` made it worse by reverting legitimate uncommitted changes.

**How to apply:**
- ALL code changes go ONLY in the current project working directory
- For pytest: use `PYTHONPATH=<project-root>/src pytest ...` to force pytest to use this clone's source
- NEVER tell subagents to copy files to other repos
- NEVER run git commands in other repos
- If a subagent mentions "editable install" or another repo, that instruction is WRONG
