---
name: feedback-staging-solo-is-a-separate-host
description: "Every staging validation must cover BOTH clustered and staging solo; verify a host's storage_mode before calling it solo -- .local-testing section 1 is stale"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 51a123fc-6e33-494f-85a8-3b7d83248eee
  modified: 2026-09-21T17:39:30.768Z
---

Every staging test/validation must cover BOTH the clustered (PostgreSQL) environment and staging
SOLO (SQLite). The user was explicit: "when you test staging, you must test there too."

**Where staging solo is: `.local-testing` section 13** (host, access, port, login, fixture,
log DB, sudo trap) -- read it FIRST. It is a dedicated solo host, distinct from the cluster
nodes and from the dev box. The user supplied its details once and I failed to write them down;
they were lost at compaction. Anything learned about a test target goes into `.local-testing`
immediately.

**Do not identify a host as "solo" from documentation. Prove it from the host's own config.** Read
its `config.json` and check `storage_mode` and `cluster.node_id`. Solo means `storage_mode: sqlite`
with no cluster node id. `postgres` + a node id means it is a CLUSTER node, whatever the notes say.

`.local-testing` section 1 ("CIDX Server (Linux)") is STALE: that host is now configured as a
cluster node (`storage_mode: postgres`), among the clustered nodes section 12 documents. It is
covered by clustered validation and is NOT staging solo. Its section 4 admin credentials still
apply to it.

**Why:** In epic #1906 I declared staging solo validated three separate times, each time on the
wrong target: first the dev box (which I then wrongly "corrected" away), then this section 1 host
from documentation alone. Each claim was built on an assumption about what a host was rather than
on its own configuration. A cluster node reads MFA from the shared Postgres, which is also why its
local SQLite `user_mfa` table was empty.

**How to apply:** Before claiming solo is validated, confirm `storage_mode: sqlite` on the target
from its config, and that its `server_version` matches the release. If no host with that config can
be located, say so and ask -- do not relabel a cluster node or the dev box as solo. Never record
host addresses here. Related: [[project_verify_both_staging_environments]],
[[project_staging_cluster_mfa_is_self_serviceable]].
