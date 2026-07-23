---
name: project-verify-both-staging-environments
description: "Standing process rule: every staging release/deploy verification must check BOTH the solo (SQLite) staging replica and the clustered (PostgreSQL/HAProxy) staging environment, always -- not just one"
metadata:
  type: project
  originSessionId: ede890d5-0358-4ba6-a741-0ed8d5e2db8f
---

The user explicitly established this as a standing process rule (2026-07-19), not a one-time request: whenever verifying a code-indexer release against staging, ALWAYS check both environments, going forward, every time:

1. The 3-node clustered staging environment (`192.168.60.20`/`.22`/`.23`, HAProxy-fronted, PostgreSQL-backed, `storage_mode: postgres`) -- see [[reference_staging_totp_programmatic_auth]] and the cluster topology notes in this project's `.local-testing`.
2. The solo SQLite production-replica (`192.168.68.167`, `storage_mode: sqlite`, no cluster/HAProxy) -- built specifically to catch solo/SQLite-only bugs the cluster can never surface (e.g. #1444's health-check bug only manifested in solo mode; #1442's CLI dependency-staleness gap was deliberately reproduced there too).

**Why**: production is a solo/SQLite deployment, not a cluster. Bugs that only manifest in solo mode (confirmed real example: #1444, permanently-unhealthy `/healthz` on solo installs) are invisible on the cluster nodes alone. Verifying only the cluster gives false confidence that doesn't transfer to what's actually running in production.

**How to apply**: Before declaring any staging deploy/release verification complete, confirm health + the relevant fix/feature on BOTH hosts -- not one as a proxy for the other. Both environments track the `staging` branch via their own `cidx-auto-update.timer` and should be treated as equally mandatory verification targets, indefinitely, for all future sessions on this project.
