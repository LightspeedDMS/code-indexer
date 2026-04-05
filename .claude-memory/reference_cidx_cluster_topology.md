---
name: CIDX Deployment Topology
description: Staging and production are SOLO/SQLite. Local dev VM is clusterized/PostgreSQL. There is NO .30 server.
type: reference
---

CIDX deployment topology.

For all IPs, credentials, and connection details, read `.local-testing`.

**Environments:**
- **Local dev VM (this machine)**: Cluster mode / PostgreSQL
- **Staging (.20)**: SOLO / SQLite — NEVER cluster
- **Production**: SOLO / SQLite — NOT clusterized (for now)

**CRITICAL: There is NO .30 server. Do NOT reference .30 as production.**

**CRITICAL: Staging is STANDALONE. NEVER configure it for cluster mode.**

**Why:** Previous sessions confused staging with cluster nodes, causing crashes. Staging and production both run solo/SQLite.

**How to apply:** Before ANY deployment or testing, verify target environment from `.local-testing`. Only the local dev VM runs cluster/PostgreSQL.

*Updated 2026-04-04*
