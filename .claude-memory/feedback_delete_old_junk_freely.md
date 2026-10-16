---
name: feedback-delete-old-junk-freely
description: "NEVER ask before deleting junk in /tmp, ~/.tmp or the scratchpad (incl. stale clones there) - just delete it and say what went"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 41d5247d-a313-4d21-8302-a19984e861bc
  modified: 2026-10-04T20:15:05.848Z
---

Never ask the owner before deleting junk in temp areas (`/tmp`, `~/.tmp`, the session scratchpad): stale DBs, test homes, A/B trees, throwaway PG clusters, and old git clones parked there. Delete it and mention briefly what was removed. The owner stated this as a firm standing rule (2026-10-04) after being asked about the same cleanup three times.

**Why:** temp areas are disposable by definition; asking costs the owner a round-trip for an answer that is always yes. Junk otherwise accumulates without bound (see [[project-pytest-tmp-accumulates-unbounded]]).

**How to apply:** the only remaining checks are mechanical safety, not permission:
- (0) grep the repo for the path first (`grep -rn '<name>' <repo> --include=*.py --include=*.sh`). Some `~/.tmp` dirs are DURABLE e2e fixtures: `~/.tmp/temporal_recall_full_repo` is the pre-built dual-embedder temporal index used by `tests/e2e/server/test_18*` and `test_19*`. It was deleted by mistake on 2026-10-04 and cannot be rebuilt as-is (its probe commit existed only there); those tests skip until a deterministic builder exists (#2035).
- (1) skip anything a RUNNING process may be using (age filter older than the current run's start, e.g. `-mmin`; `find` here is bfs).
- (2) use literal absolute paths (the harness blocks variable-built `rm -rf` targets).
- (3) never touch tracked repo files, the real `~/.cidx-server`, owner content outside temp areas (e.g. repo-root `tmp/doc_audit`), or other sessions' uncommitted work.
- Root-owned leftovers can go with `sudo -n rm -rf` on literal paths.
