---
name: feedback-local-only-memories
description: "Package/release/session resume notes and any memory holding environment, credential-procedure or business detail are LOCAL ONLY: gitignored, never pushed"
metadata:
  type: feedback
---
The memory directory is committed to the public repository, but some memories have no place in a
remote. Keep these on disk only, through the "Local-only agent memories" section of `.gitignore`:
- package, release, batch, backlog and session resume notes (`project_package_*`,
  `project_release_*`, `project_batch_*`, `project_backlog_*`), and any other note that tracks the
  state of current work;
- notes about a specific environment (`project_staging_*`, host or storage runbooks, ports,
  topology, the local dev machine), credential or login procedures, and business detail (costs,
  account tiers, deployment plans);
- a new memory that needs to stay local and fits no pattern: name it `local_<topic>.md`.
**Why:** owner, 2026-10-28: "those package files remove them from git, and ignore them. keep in the
local memory, but they have no place in remote." A review the same day found tracked memories that
described an admin login procedure, environment topology and cost figures.
**How to apply:** before writing a memory, decide whether it is a lasting, generic lesson (tracked)
or state/environment detail (local only); for the latter use an ignored name pattern and confirm with
`git check-ignore -v <file>`. Index lines in MEMORY.md (tracked) for local-only files stay neutral.
Removing a file from git does not remove it from history. Related: [[feedback-no-secrets-in-memory]].
