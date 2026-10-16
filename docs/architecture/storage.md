# Index Storage

How CIDX stores semantic, full-text and temporal indexes on disk, how the two chunk layouts coexist, and how a
repository moves from one to the other. Audience: maintainers and contributors. The rules this design must keep are
in [Chunk Storage Invariants](invariants/chunk-storage.md); how indexes are built is in [Indexing](indexing.md).

## Where indexes live

Every indexed repository has a `.code-indexer/` directory at its root:

```
.code-indexer/
  config.json             project configuration (cidx init)
  index/<collection>/     one directory per vector collection
  tantivy_index/          full-text (Tantivy) index
  scip/                   SCIP databases (index.scip.db per project)
```

- A semantic collection is named after its embedding model with `/` and `:` replaced by `_`, for example
  `voyage-code-3`, `embed-v4.0`, `voyage-multimodal-3` (markdown with images).
- Temporal (git history) collections are quarterly shards per embedder, `code-indexer-temporal-{model_slug}-{YYYY}Q{N}`,
  plus a bare `code-indexer-temporal` bookkeeping directory that holds the temporal metadata store.
- In server context temporal collections do not live in the repository; see
  [Temporal index location](#temporal-index-location).
- The full-text index is always `.code-indexer/tantivy_index/`. The server builds a fresh `TantivyIndexManager` from
  that directory for each FTS query, so a rewritten index is picked up without cache invalidation.

The storage engine for all vector collections is `FilesystemVectorStore`
(`src/code_indexer/storage/filesystem_vector_store.py`) with an HNSW graph per collection
(`src/code_indexer/storage/hnsw_index_manager.py`, hnswlib with `M=16`, `ef_construction=200`).

## Two chunk layouts

| | SHARDED_JSON | CHUNKS_DB |
|---|---|---|
| Chunk records | one `vector_<id>.json` per chunk, in hash-sharded subdirectories of the collection | one SQLite `chunks.db` per collection (`ChunkStore`, `src/code_indexer/storage/sqlite_chunk_store.py`) |
| Point id lookup | `id_index.bin` (point id to file) | `chunks.db` primary key; no `id_index.bin` |
| Common files | `collection_meta.json`, `hnsw_index.bin`, `path_index.bin`, `projection_matrix.npy` | the same, plus the `chunks_db` discriminator in `collection_meta.json` |

`collection_meta.json` also carries the HNSW `id_mapping` (graph label to point id) and the counters readers trust
(`vector_count`, `unique_file_count`, `points_count`).

### Which layout a collection has

`resolve_chunk_layout(collection_dir)` (`src/code_indexer/storage/shared/chunk_layout.py`) is the only authority. A
collection is CHUNKS_DB exactly when `collection_meta.json` has a top-level `{"chunks_db": {"version": <int >= 1>}}`;
everything else resolves to SHARDED_JSON. The discriminator is written last, after `chunks.db` and its indexes are
durable, so a half-built collection is never read as consolidated. During a build in progress the store uses
`_is_chunks_db_collection()`, which also knows the session's own build intent.

### Which layout a new collection gets

| Caller | New semantic collection | New temporal collection |
|--------|------------------------|-------------------------|
| `cidx index` (CLI or daemon), no flag | SHARDED_JSON, unless `CIDX_CHUNKS_DB_NEW_COLLECTIONS` is truthy | CHUNKS_DB |
| `cidx index --new-collection-layout=chunks_db` | CHUNKS_DB | CHUNKS_DB |
| `cidx index --new-collection-layout=sharded_json` | SHARDED_JSON | rejected with `--index-commits` (`reject_sharded_json_for_temporal` in `cli.py`) |
| `cidx index --clear` | CHUNKS_DB (`sharded_json` is rejected) | CHUNKS_DB |
| Server-launched `cidx index` | CHUNKS_DB (`--new-collection-layout=chunks_db` is always passed) | CHUNKS_DB |

An existing collection keeps its committed layout regardless of these choices. Check the options in this tree:

```bash
PYTHONPATH=src python3 -m code_indexer.cli index --help
```

## Write and read paths

- Writes go through `begin_indexing` -> `upsert_points` -> `end_indexing`. For CHUNKS_DB, `_upsert_points_chunks_db`
  writes rows in SQLite transactions; `end_indexing` finalises HNSW and commits the discriminator.
- HNSW files are published by writing a temporary file and renaming it over the live path, so a reader sees either
  the old or the new graph, never a partial one.
- Queries load the HNSW graph (possibly on a worker thread), take the top candidates, and hydrate them from the
  chunk store on the calling thread. For CHUNKS_DB that is at most `limit` row reads when no filter applies.
- Non-git and dirty files store the chunk text. Clean git files store the git blob reference; their content is read
  from the current file, then from the git blob, and the result is marked stale when the file changed since indexing
  (`_get_chunk_content_with_staleness`). Immutable snapshots skip the file-hash comparison (`skip_staleness_check`).
- Status and health checks never open a mutable `ChunkStore`: they use `chunk_store_has_real_data()`, which opens the
  database read-only and never creates a missing file.

## Moving a repository to CHUNKS_DB

### On one machine

`cidx index --migrate-chunks-to-sqlite` consolidates every SHARDED_JSON collection of the repository in place and
exits without indexing. The engine is `consolidate_collection_in_place()`
(`src/code_indexer/storage/shared/collection_migration.py`): scan legacy records, write `chunks.db`, verify every
record against the source, commit the discriminator, then delete the legacy files. A rerun at any point is safe.
`cidx index --index-commits` also consolidates any legacy temporal shard before writing (the daemon refuses and asks
for the explicit migration instead).

### Across a server fleet

`FleetMigrationScheduler` (`src/code_indexer/server/services/fleet_migration/`) applies the same engine to each golden
repository's base clone, one repository at a time across the whole fleet, under the repository's write lock and
never while a refresh of it is running. After a repository completes it publishes a fresh snapshot so queries move
to the consolidated copy. It is off by default (`fleet_migration_config.enabled`), and the same flag authorises the
deletion of legacy files; enable it only after every node runs a version that reads both layouts. A repository that
keeps failing is quarantined after three attempts; an optional canary gate pauses after the first repository.

## HNSW integrity

- Every build and finalize runs an integrity check and repairs orphan nodes before persisting
  (`_detect_and_repair_orphans`), using primitives that exist only in the project's hnswlib fork
  ([hnswlib custom build](../server/hnswlib-custom-build.md)). Without the fork the pass is skipped with one WARNING.
- Health reports `orphan_count` as a binary signal: 0 is healthy, anything else is an error.
- Indexes built before that check existed are repaired by the HNSW orphan sweep
  (`src/code_indexer/server/services/hnsw_orphan_sweep/`, on by default): a paced background job walks golden and
  activated repositories, repairs under the same rebuild lock the indexer uses, and invalidates the server's HNSW
  cache entry. Progress is kept in the `hnsw_orphan_sweep_state` table; statistics are at
  `GET /api/admin/hnsw-orphan-sweep/stats`.

## Temporal index location

- CLI: `.code-indexer/index/code-indexer-temporal-*` inside the repository.
- Server: a fixed path per golden repository outside its clone, `{golden_repos_dir}/.temporal/{alias}/`, decided only
  by `src/code_indexer/services/temporal/temporal_server_paths.py`. Server-launched temporal indexing (marked by the
  `CIDX_SERVER_REFRESH_CONTEXT` environment variable) writes there, and server temporal queries for a golden
  repository, its global alias or any activation of it read there. Activations never carry their own temporal data.
- The path never changes, so refreshes write in place and readers detect a new HNSW graph by its file identity
  (inode, device, size, mtime) on the next read.
- Shards written in-repository before the fixed root existed are relocated by the temporal legacy migration scheduler,
  off by default (`temporal_legacy_migration_config`).

## Server caches over this storage

- `HNSWIndexCache` (`src/code_indexer/server/cache/`) holds loaded HNSW graphs per worker, capped at 4096 MB per node
  by default and divided by the worker count. The id-index cache holds loaded id indexes per worker, limited by entry
  count (`CIDX_ID_INDEX_CACHE_MAX_ENTRIES`, default 200) with no per-worker division. Keys include the collection path,
  its layout and the activation id, so a consolidation or reactivation is a cache miss. Full-text queries use no cache
  (see [Where indexes live](#where-indexes-live)), so a reindex needs no FTS cache invalidation.
- Snapshot paths are immutable, so a refresh that publishes a new snapshot produces new cache keys; old entries are
  evicted by the cache's access TTL or its size cap.
- In a cluster with `cluster.sharding_enabled`, a node caches only the repositories it owns
  (`ShardOwnership`, rendezvous hashing); other repositories are still served, just not cached. See
  [Cluster Architecture](cluster.md#query-serving-and-caches).
