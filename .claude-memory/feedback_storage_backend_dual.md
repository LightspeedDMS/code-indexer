---
name: Never assume SQLite — CIDX runs on both SQLite and PostgreSQL
description: When discussing DB operations on code-indexer, cover BOTH SQLite (solo) and PostgreSQL (cluster) backends — never mention only one
type: feedback
---

Never refer to the CIDX storage backend as "SQLite" without qualification. The project runs in two modes:

- **Solo mode**: SQLite (`~/.cidx-server/logs.db`, `database.db`, etc.)
- **Cluster mode**: PostgreSQL (DSN from `config.json` `postgres_dsn`, node-shared schema)

Controlled by `storage_mode` bootstrap key in `config.json` — see project CLAUDE.md §"Config Bootstrap vs Runtime" and §"Post-E2E Log Audit".

**Why:** On 2026-04-20 during #876 audit I wrote "SQLite `_migrate_*` method" and "SQLite mirror" as if PostgreSQL did not exist. The user (correctly) blew up: "don't fucking say sqlite, when running in cluster mode, it could be PG." Assuming one backend erases half the deployment model and produces broken resumption plans because cluster-specific work (PostgreSQL migrations in `postgres/migrations/sql/`, advisory locks, `INSERT ... ON CONFLICT`) has a parallel SQLite implementation that must be delivered in lockstep. Every migration story on this repo ships a `.sql` migration file for PostgreSQL AND a corresponding `_migrate_*` method or inline DDL in `database_manager.py` for SQLite — they are peers, not a primary/secondary.

**How to apply:**
- When discussing migrations: say "PostgreSQL migration `NNN_name.sql` + SQLite `_migrate_*` in `database_manager.py`" — always both, or "both backends" as shorthand.
- When discussing schema / DDL / tables / indexes: enumerate both or use backend-agnostic phrasing ("the logs table", "the `background_jobs` row").
- When discussing queries or transactions: call out that the primitive differs (`INSERT ... ON CONFLICT ... RETURNING` on PostgreSQL vs `BEGIN IMMEDIATE; INSERT OR IGNORE` on SQLite) and ship both code paths.
- When writing audit reports or resumption plans: any gap that's "missing on SQLite" must also be verified on PostgreSQL (and vice-versa) before the item is filed as MISSING vs DONE.
- NEVER propose "document as cluster-only by design" or "document as solo-only by design" as an acceptable resolution — see Complete Symmetry Invariant below.

## Complete Symmetry Invariant — NON-NEGOTIABLE

On 2026-04-20 the user said: *"I want complete symmetry between solo or cluster mode. if you missed this, you fucked up, so fucking fix it."*

**Rule**: Every feature, column, index, table, migration, query path, and write primitive that ships on one backend MUST ship on the other in the same PR/story. There is NO "option (b) cluster-only-by-design" escape hatch. There is NO "solo-mode-does-not-need-this" exemption. Any PostgreSQL-only column (e.g. `executing_node`, `claimed_at` on `background_jobs` as of 2026-04-20) is a BUG to be fixed, not a design feature to be documented.

**Implication for audits**: when cataloguing gaps, a feature present on one backend and absent on the other is PARTIAL, never DONE — the DONE state requires parity. Never file a one-sided item as DONE.

**Implication for resumption plans**: symmetry fixes are first-class tasks, same priority as the primary work — not follow-ups, not nice-to-haves. If a story has introduced one-sided schema, the symmetry restoration is in-scope for that story.

**Why the rule is hard**: the user has had to catch this failure mode repeatedly. It arises when PostgreSQL already had a column from an earlier initial-schema file (`001_initial_schema.sql`) and the current work adds functionality that depends on it — the instinct is to assume "SQLite already has it too." It does not. `src/code_indexer/server/storage/database_manager.py` is authoritative for the SQLite schema; diff it against `src/code_indexer/server/storage/postgres/migrations/sql/001_initial_schema.sql` before claiming parity.
