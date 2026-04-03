---
name: CIDX Cluster Testing Topology
description: Network topology for Epic #408 cluster testing — staging is STANDALONE, NEVER cluster. Read .local-testing section 9 for IPs.
type: reference
---

CIDX cluster test environment topology (Epic #408).

For all IPs, credentials, and connection details, read `.local-testing` section 9.

Key roles: Dev machine (Node 1), CIDX Node 2, CIDX Node 3, HAProxy, PostgreSQL, Staging, Langfuse.

**CRITICAL: Staging is STANDALONE. NEVER configure it for cluster mode.**

**Why:** Previous session catastrophically confused staging with cluster node 2, resulting in staging being configured with postgres/cluster settings and crashing. This wasted significant time and required manual recovery.

**How to apply:** Before ANY SSH connection for cluster testing, verify the target IP from `.local-testing` section 9. Staging and node 2 are on different subnets — never confuse them.
