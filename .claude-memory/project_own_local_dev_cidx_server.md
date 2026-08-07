---
name: project-own-local-dev-cidx-server
description: "I own and am responsible for maintaining the local dev machine's cidx-server installation and its systemd service"
metadata: 
  node_type: memory
  type: project
  originSessionId: bf453024-c658-4c98-bc2d-eebbb3ac44f3
  modified: 2026-08-03T20:23:38.214Z
---

I (the agent) own and am responsible for the systemd-managed `cidx-server.service` running on this local development machine, and for the code-indexer checkout it runs from (`/home/jsbattig/code-indexer` -- separate from the interactive session working directory `/home/jsbattig/Dev/code-indexer`).

**Standing requirements for this local install:**
- Track the `development` branch only -- never leave it on an epic/feature branch or any other ref.
- No auto-update mechanism should run against it (no `cidx-auto-update` timer/service, no background git-pull loop) -- I update it manually when appropriate.
- Keep it healthy and clean: if it's crash-looping, broken, or stale, that's mine to fix, not an unrelated pre-existing artifact to route around.

**Why:** User explicitly said "you own this machine too. keep it clean... it should follow development, and not do auto update. this is your dev machine. yours." (2026-08-03), after I found `cidx-server.service` crash-looping (2482 restarts) on an abandoned `epic/408-cidx-clusterization` checkout and initially treated it as unrelated/out-of-scope. Also: "memorize that you own the running code-indexer server in the local development machine where you are working. don't reveal secret" -- the "don't reveal secret" reminder means admin passwords/tokens/private key content for this local server must never be printed in visible chat output (use the SECRET_FILE/SECRET_TEXT declaration protocol as usual).

**How to apply:** In any future session on this machine, proactively check `cidx-server.service`'s health/branch/auto-update state rather than assuming someone else owns it or that it's out of scope. Fix drift (wrong branch, crash loops, stale deploys) as part of normal maintenance, not as a special escalation.
