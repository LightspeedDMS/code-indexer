---
name: feedback-probes-must-isolate-server-home
description: Reviewer/engineer probes that import the server app write to the real server home; always point HOME at a scratch dir
metadata:
  node_type: memory
  type: feedback
  originSessionId: 469b8128-2710-4307-9c15-5e1dfb0f1a0b
  modified: 2026-10-20T04:21:16.040Z
---

Any probe or ad-hoc test that imports `code_indexer.server.app` (or runs its lifespan) initialises the server with the current HOME and rewrites the real `~/.cidx-server` state: `config.json` (bootstrap strip), `launch.json`, `cidx_server.db` / `groups.db` schema and job state. A read-only code review once did exactly this from an exported tree, silently writing newer-code state into the local dev server's home.

**Why:** the local dev server runs from that directory; newer-code writes can change its bootstrap/runtime state behind its back, and the side effect is easy to miss because the review itself "only read code".

**How to apply:** every reviewer and engineer brief that may start the app must say: set `HOME` (and any server-dir override) to a scratch directory before importing the app; never run probes against the real server home. After any run that reports app initialisation, check `~/.cidx-server` mtimes (`find ~/.cidx-server -maxdepth 2 -mmin -N`) and verify `storage_mode`/bootstrap values. Related: [[project-own-local-dev-cidx-server]].
