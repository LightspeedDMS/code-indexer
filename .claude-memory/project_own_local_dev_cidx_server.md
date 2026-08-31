---
name: project-own-local-dev-cidx-server
description: "I own and am responsible for maintaining the local dev machine's cidx-server installation and its systemd service"
metadata: 
  node_type: memory
  type: project
  originSessionId: bf453024-c658-4c98-bc2d-eebbb3ac44f3
  modified: 2026-08-12T13:12:06.045Z
---

I (the agent) own and am responsible for the systemd-managed `cidx-server.service` running on this local development machine, and for the code-indexer checkout it runs from (`/home/jsbattig/code-indexer` -- separate from the interactive session working directory `/home/jsbattig/Dev/code-indexer`).

**Standing requirements for this local install:**
- Track the `development` branch only -- never leave it on an epic/feature branch or any other ref.
- No auto-update mechanism should run against it (no `cidx-auto-update` timer/service, no background git-pull loop) -- I update it manually when appropriate.
- Keep it healthy and clean: if it's crash-looping, broken, or stale, that's mine to fix, not an unrelated pre-existing artifact to route around.

**Why:** User explicitly said "you own this machine too. keep it clean... it should follow development, and not do auto update. this is your dev machine. yours." (2026-08-03), after I found `cidx-server.service` crash-looping (2482 restarts) on an abandoned `epic/408-cidx-clusterization` checkout and initially treated it as unrelated/out-of-scope. Also: "memorize that you own the running code-indexer server in the local development machine where you are working. don't reveal secret" -- the "don't reveal secret" reminder means admin passwords/tokens/private key content for this local server must never be printed in visible chat output (use the SECRET_FILE/SECRET_TEXT declaration protocol as usual).

**Known failure mode -- server running OUT-OF-BAND from the working tree (seen 2026-08-09):**
The port was held by a manually-started uvicorn with PPID 1 (orphaned to init, so `systemctl stop`
could not reach it), loading `PYTHONPATH=/home/jsbattig/Dev/code-indexer/src` -- i.e. the INTERACTIVE
WORKING TREE, not the dedicated checkout. Two consequences, both bad: (1) every source edit made
during development was one restart away from becoming the live server's behaviour; (2) the systemd
unit could never bind, so `Restart=always` spun ~6,000 doomed processes in a day, each running the
full lifespan against the same SQLite DBs -- which is what caused Bug #1549 (jobs reaped by stranger
processes) and left 803 failed jobs behind.

Tell-tales: `systemctl is-active` says `activating`/`inactive` while port 8000 is served; `NRestarts`
in the thousands; the reported server `version` matches NEITHER checkout's current version (it matches
whatever the working tree held when the process started).

Recovery (safe order): confirm `active_jobs == 0` via `/health` FIRST, `git -C /home/jsbattig/code-indexer
pull --ff-only origin development`, `kill -TERM <out-of-band pid>`, wait for the port to free, then
`sudo systemctl start cidx-server`. Verify `version`, `NRestarts=0`, and that jobs created AFTER
startup complete rather than being marked restart-orphaned.

**It is mine to USE, not merely to maintain (user, 2026-08-12):** this server exists so I can TEST MY
OWN WORK on it. That is its entire purpose. So:

- After landing a change, update this install to the current `development` tip and RESTART it, then
  exercise the change against it. Do not leave it several versions behind while verifying only on a
  shared remote environment -- that wastes the one environment I am free to break.
- It is the correct first place to try anything I would hesitate to try on a shared environment:
  destructive config modes, cleanup/reconciler sweeps, migrations, restart-dependent behaviour.
  Reach for it BEFORE reaching for a remote environment, not after.
- Restarting it is normal maintenance, not an escalation -- confirm `active_jobs == 0` first (a
  restart kills in-flight long jobs), then restart via systemd.

Failing to use it is a real miss: on 2026-08-12 I shipped a seven-item batch and verified it only on
the remote environment while this server sat four minor versions stale, and the user rightly called
that out. Its data also makes it a genuinely better test target than a clean instance -- it carries
real accumulated repos and artifacts, which is what surfaced a reconciler coverage gap that a fresh
instance could not have shown.

**How to apply:** In any future session on this machine, proactively check `cidx-server.service`'s health/branch/auto-update state rather than assuming someone else owns it or that it's out of scope. Fix drift (wrong branch, crash loops, stale deploys) as part of normal maintenance, not as a special escalation. Also verify the server is actually running FROM the dedicated checkout under systemd -- an `active` unit is not enough if something else holds the port.
