---
name: never_touch_other_repos
description: NEVER modify files in ~/Dev/code-indexer/ or any repo outside the working directory. Only work in code-indexer-master.
type: feedback
---

ABSOLUTE PROHIBITION: NEVER read, write, copy, restore, or modify ANY file in ~/Dev/code-indexer/ or any directory outside the assigned working directory (~/Dev/code-indexer-master/).

**Why:** ~/Dev/code-indexer/ has an editable install (`pip install -e .`) that pytest uses. Another agent works there. Copying files to "sync the editable install" destroyed that agent's work. Then attempting to "restore" with `git checkout` made it worse by reverting the other agent's legitimate uncommitted changes.

**How to apply:**
- ALL code changes go ONLY in ~/Dev/code-indexer-master/
- For pytest: use `PYTHONPATH=/home/jsbattig/Dev/code-indexer-master/src pytest ...` to force pytest to use code-indexer-master's source, overriding the editable install
- NEVER tell subagents to copy files to ~/Dev/code-indexer/
- NEVER run git commands in ~/Dev/code-indexer/
- If a subagent mentions "editable install" or the other repo, that instruction is WRONG
