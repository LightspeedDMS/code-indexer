---
name: feedback_faithful_db_mocks
description: "DB mocks must be faithful to the real driver API; psycopg3 executemany is on the cursor, not the connection — verify against real PG"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 43243d70-1318-4096-b79a-3b82c3e767bb
---

psycopg v3 `Connection` has NO `executemany()` — it is a `Cursor` method only (`with conn.cursor() as cur: cur.executemany(...)`). `Connection.execute()` exists (a shortcut), which misleads. SQLite's `sqlite3.Connection` DOES have `executemany`, which deepens the confusion when the same code targets both backends.

During Epic #1161 / Bug #1181, two PostgreSQL batch writers (payload_cache `store_batch`, query_embedding_cache `touch_last_used_batch`) called `conn.executemany(...)`. Wrapped in fail-open `try/except`, the `AttributeError` was swallowed → both batched writes were COMPLETE SILENT NO-OPS in PostgreSQL mode (payload rows never persisted → `/cache/{handle}` 404; last_used never updated). Every unit test AND two code reviews passed — because the test `FakeConn` defined `executemany` ON THE CONNECTION, modeling an API the real library lacks. Green tests certified dead production code. It was caught ONLY by benchmarking against a REAL throwaway PostgreSQL (cache_handle round-trip 404, distinct-timestamp commit counting).

**Why:** A mock more capable than the real driver (Messi Rule #1 anti-mock) hides real bugs and makes fail-open swallow them silently (Messi #2/#13). Reviewers reading code+tests can't catch an API that doesn't exist if the mock pretends it does.

**How to apply:** (1) For any psycopg3 batch write use `with conn.cursor() as cur: cur.executemany(...)` in the same transaction (SET LOCAL → executemany → single commit); never `conn.executemany`. (2) DB-backend test mocks MUST mirror the real driver surface exactly — put `executemany` on a `cursor()` context manager, not the connection. (3) For PG-backend correctness, prefer a real-PG regression test gated on an env DSN (e.g. `CIDX_TEST_PG_DSN`) over mock-only tests; mock-only PG tests give false confidence. (4) When a perf/storage change "passes all tests," verify the write actually persists against real PG before trusting it. Relates to [[feedback_e2e_not_code_inspection]] and [[project_test_gates_flake_under_load]].
