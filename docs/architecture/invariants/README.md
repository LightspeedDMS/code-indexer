# Architecture Invariants

Rules the code must keep, grouped by topic. Each file states the rule, the module that enforces it and, where one
exists, the test that guards it. The short form of every group is the "Critical Architecture Invariants" section of
the repository's `CLAUDE.md`, which links here. For how a subsystem works rather than what it must not break, follow
the architecture links in each file.

| Group | Covers |
|-------|--------|
| [Cluster state and background jobs](cluster-and-jobs.md) | no per-process state across requests, payload cache, job dedup, cancellation, pod-pull work stealing |
| [Server runtime](server-runtime.md) | lazy module singletons, lazy app construction, connection hygiene, cache caps, pooling, throughput benchmark |
| [Shared storage protocol (NFS)](shared-storage.md) | the single NFSv3 CoW mount, why NFSv4 is excluded, code that must tolerate a blocking mount |
| [X-Ray](xray.md) | tree-sitter lazy loading, Rust evaluator boundary, compile cache identity, pattern library |
| [Auth](auth.md) | TOTP elevation error codes and kill switch, CLI retry, JWT logout revocation, loopback-only maintenance |
| [Golden repositories and snapshots](golden-repos.md) | mutable base clone vs immutable snapshot, canonical predicate, retention, registry-orphan reconcile, activation wiring |
| [Query path](query-path.md) | drift-safe caches, query-embedding cache, search timeouts, coalescer and 4-lane governor, call tracking |
| [Indexing and migrations](indexing-and-migrations.md) | no indexing timeouts, fail loud, temporal indexing, HNSW orphan repair and sweep, schema migrations |
| [Chunk storage and fleet migration](chunk-storage.md) | layout authority, in-place consolidation, fleet migration gates, temporal data location |
| [Auto-updater and pace-maker](auto-update.md) | installer plus self-heal rule, launch settings, deployment lock, pace-maker guard |
| [Dependency map, cidx-meta, description refresh](depmap-and-description-refresh.md) | resumable delta, sentinels, graph repair, backup mirror, description quarantine and refinement |
| [Global alias fallback](global-alias-fallback.md) | bare alias promoted to `-global` on read paths only |
| [Fault injection and memory retrieval](fault-injection-and-memory-retrieval.md) | non-production harness guard, provider HTTP through the factory, memory pipeline confinement |

Invariants documented with their subsystem instead of here:

- Refresh failure classification, backoff, strikes and deferred triggers: [Refresh Recovery](../refresh-recovery.md).
- Chunk storage layouts and data flow: [Storage](../storage.md).
- Login throttling, MFA challenges and elevation from the operator's side:
  [Login and Elevation](../../server/auth/login-and-elevation.md).
- SIEM delivery: [SIEM Operations](../../server/siem/operations.md).
