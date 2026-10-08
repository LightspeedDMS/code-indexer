# Indexing Pipeline

Maintainer reference for how `cidx index` turns a working tree into vector and full-text indexes. It covers the
semantic (per-file) pipeline end to end and names the entry point of the separate temporal (git history)
pipeline. On-disk collection layouts (`vector_*.json` versus `chunks.db`), HNSW maintenance and repair are
documented in [storage.md](storage.md); how a server refresh runs this pipeline and recovers from failures is in
[refresh-recovery.md](refresh-recovery.md).

All paths below are relative to `src/code_indexer/`.

## Contents

- [Components](#components)
- [Entry point and locks](#entry-point-and-locks)
- [Choosing a strategy](#choosing-a-strategy)
- [File discovery](#file-discovery)
- [File identity and point ids](#file-identity-and-point-ids)
- [Chunking](#chunking)
- [Embedding: per-file batching and token budget](#embedding-per-file-batching-and-token-budget)
- [Writing points and finalizing the session](#writing-points-and-finalizing-the-session)
- [Branch isolation](#branch-isolation)
- [Full-text index](#full-text-index)
- [Reconcile](#reconcile)
- [Failures, cancellation and exit codes](#failures-cancellation-and-exit-codes)
- [Temporal indexing entry point](#temporal-indexing-entry-point)

## Components

| Component | Module | Role |
|-----------|--------|------|
| `SmartIndexer` | `services/smart_indexer.py` | Strategy selection (full, incremental, resume, reconcile, branch change), progressive metadata, session lifecycle |
| `HighThroughputProcessor` | `services/high_throughput_processor.py` | Parallel hash phase, file submission, result collection, branch visibility |
| `FileChunkingManager` | `services/file_chunking_manager.py` | Per-file lifecycle: chunk, reuse cached vectors, batch to the embedder, build points, write |
| `VectorCalculationManager` | `services/vector_calculation_manager.py` | Thread pool that calls the embedding provider |
| `FileFinder` | `indexing/file_finder.py` | File discovery and the canonical exclude rules |
| `FileIdentifier` | `services/file_identifier.py` | Per-file metadata: content hash, git blob hash, branch, commit |
| `FixedSizeChunker` | `indexing/fixed_size_chunker.py` | Character-based chunking with fixed overlap |
| `ProgressiveMetadata` | `services/progressive_metadata.py` | Resume state and per-branch commit watermark |
| `FilesystemVectorStore` | `storage/filesystem_vector_store.py` | `begin_indexing` / `upsert_points` / `end_indexing` session |
| `TantivyIndexManager` | `services/tantivy_index_manager.py` | Full-text (FTS) index |

## Entry point and locks

The `index` command in `cli.py` drives the run. Before any work it takes two locks:

1. The repo-scoped, non-blocking index-mutation lock `.code-indexer/.index-mutation.lock`
   (`acquire_index_mutation_lock`, `services/chunk_migration_cli.py`). A second `cidx index` or a chunk-layout
   migration in the same repository fails immediately with "Another cidx index or migration is already running".
2. Inside `SmartIndexer.smart_index()`, a heartbeat lock in the metadata directory (`create_indexing_lock`,
   `services/indexing_lock.py`: 30 s heartbeat, 300 s staleness timeout) so a crashed process does not block the
   next run forever.

`smart_index()` also refuses to start when `hnswlib` cannot be imported.

**Providers.** `config.get_embedding_providers()` lists the configured providers; only those with an API key are
used. The first one runs the primary pass (with FTS when `--fts` is given); each further provider runs its own
`SmartIndexer` pass with FTS disabled, after a live credential check (`health_check(test_api=True)`). Each
provider keeps its own resume file, `.code-indexer/metadata-<provider>.json` (`_get_provider_metadata_path`).

**Thread count.** The vector thread count comes from `voyage_ai.parallel_requests` or `cohere.parallel_requests`
in `.code-indexer/config.json` (default 8 for both, `config.py`); there is no command-line flag for it.

## Choosing a strategy

`SmartIndexer.smart_index()` picks exactly one strategy, in this order:

| Condition | Strategy |
|-----------|----------|
| Not `--clear`, not `--reconcile`, the stored branch differs from the current branch and the collection exists | Branch change: `GitTopologyService.analyze_branch_change()` then `process_branch_changes_high_throughput()` reindexes only files that differ and updates visibility of the rest |
| The self-heal reprocess sidecar of the collection is corrupt | Forced reconcile (see [Reconcile](#reconcile)) |
| Resume state is trusted and an interrupted run can resume | `_do_resume_interrupted()` continues the stored file list |
| Resume state is not trusted (server-spawned run without a valid server seal, `services/resume_state_seal.py`) and the last run was interrupted | Reconcile instead of resume |
| `--reconcile` | Reconcile |
| `--clear` | Full index: progressive metadata cleared, collection cleared, every file processed |
| Provider, model, git availability or project id changed since the last run | Full index |
| Otherwise | Incremental index |

`cidx index` without flags therefore performs a full index on the first run (no resume timestamp) and an
incremental index afterwards.

## File discovery

### Full walk: `FileFinder.find_files()`

`os.walk()` over the codebase directory (symlinked directories are not followed). A file is indexed when all of
these hold (`FileFinder._should_include_file`):

- its extension is in `file_extensions` (`.code-indexer/config.json`);
- its relative path does not match the exclude pathspec;
- it is a text file: a configured extension, or the first 1024 bytes contain no NUL byte and decode;
- its size is at most `indexing.max_file_size` (default 1048576 bytes, `IndexingConfig` in `config.py`);
- in server context only (`config.confined_to_codebase_root`), a symlinked file must resolve inside the codebase
  root. Local CLI indexing follows such symlinks.

The exclude pathspec (`gitwildmatch`) is built once per `FileFinder`:

- every `exclude_dirs` entry and every `add_exclude_dirs` override entry, as `<dir>/**` and `**/<dir>/**`;
- fixed patterns: Python bytecode and caches, compiled binaries (`*.so`, `*.dylib`, `*.dll`), OS artifacts,
  editor temp files, `node_modules/`, `build/`, `dist/`, `target/`, `.git/`, and `.code-indexer-override.yaml`;
- the patterns of the root `.gitignore` and of `.gitignore` files one directory level below the root (deeper
  `.gitignore` files are not read).

Directories matching the pathspec are pruned during the walk unless a `force_include_patterns` override could
match something below them. When `.code-indexer-override.yaml` is present, `OverrideFilterService` makes the final
include decision from the base result (`add_extensions`, `remove_extensions`, `force_include_patterns`, and so
on).

### Incremental discovery

`_do_incremental_index()` builds the file set from two sources:

1. **Committed changes.** When the branch has a stored commit watermark that differs from `HEAD`,
   `_get_git_deltas_since_commit()` runs `git diff --name-status <watermark>..<HEAD>`. Added and modified paths
   are reindexed; deleted paths are removed from the index immediately; a rename is a delete of the old path plus
   an add of the new one, each side filtered independently.
2. **Working-tree changes.** `FileFinder.find_modified_files(resume_timestamp)` returns eligible files whose
   mtime is newer than the last index time minus a safety buffer (`_INDEX_SAFETY_BUFFER_SECONDS = 60` in
   `cli.py`).

Paths from `git diff` cannot be stat'ed when deleted, so `SmartIndexer._should_index_file()` filters them with
string rules only, but it applies the same rules as the full walk: extension, `exclude_dirs` as path components,
`FileFinder.matches_exclude_pattern()` (the same pathspec `find_files()` uses), and the same
`OverrideFilterService`. A new exclusion rule therefore applies to both discovery paths.

Files recorded as failed in the previous run, and paths waiting in the self-heal reprocess sidecar, are folded
into the set as well. Standard incremental runs do not look for files deleted from disk outside git history;
`--detect-deletions` adds that scan for non-git projects, and `--reconcile` always includes it.

## File identity and point ids

`FileIdentifier.get_file_metadata()` (`services/file_identifier.py`) records for every file:

- `file_hash`: `sha256:<hex>` of the file content (always computed);
- in a git repository: `git_hash` (`git hash-object` of the file on disk), plus the branch and `HEAD` commit, each
  fetched once per run;
- outside git: the file mtime (integer seconds) and size;
- `project_id`: the basename of the `origin` remote URL, or the directory name, lowercased with `_` replaced by
  `-`.

The point id of a chunk is the MD5 hex digest of `"{project_id}_{file_hash}_{chunk_index}"`
(`FileChunkingManager._create_vector_point`). Re-indexing identical content therefore produces identical ids.

When the store writes a point (`FilesystemVectorStore._prepare_vector_data_batch`), it asks git for the blob hash
of the path at `HEAD` (`git ls-tree HEAD`, 100 paths per call) and for uncommitted changes (`git status
--porcelain`). For a clean tracked file it stores `git_blob_hash`, drops the chunk text and sets
`indexed_with_uncommitted_changes` to false; queries read the text back from the git object. In a git repository, a
modified or untracked file stores the chunk text (`chunk_text`) and sets `indexed_with_uncommitted_changes` to true.
Outside git, the chunk text is stored and the flag is not set.

Reconcile compares content ids derived from these fields: `<path>:blob:<git_blob_hash>` for committed files and
`<path>:working_dir:<mtime>:<size>` for working-tree or non-git files (`working_dir_content_id`,
`services/smart_indexer.py`).

## Chunking

`FixedSizeChunker` (`indexing/fixed_size_chunker.py`) cuts text at fixed character positions, with no parsing:

| Model | Chunk size (characters) |
|-------|-------------------------|
| `voyage-code-3`, `voyage-code-2`, `voyage-large-2`, `voyage-3`, `voyage-3-large`, `embed-v4.0` | 4096 |
| any other model | 1000 |

Overlap is 15 percent of the chunk size (614 characters for 4096), so each chunk starts `chunk_size - overlap`
characters after the previous one. Each chunk records `line_start` and `line_end`. The `indexing.chunk_size` and
`indexing.chunk_overlap` fields in `IndexingConfig` are not read by the chunker.

For `.md`, `.html`, `.htm` and `.htmx` files the chunker also extracts image references. A chunk with images is
embedded separately through the provider's multimodal client into a separate multimodal collection.

## Embedding: per-file batching and token budget

`HighThroughputProcessor.process_files_high_throughput()` runs two phases:

1. **Hash phase.** `vector_thread_count` threads compute `FileIdentifier` metadata for every file. A file that
   vanished since discovery is skipped with a warning; any other hash error aborts the run. Before this phase the
   store's writability is checked (`preflight_chunk_store_writable`).
2. **File phase.** Each file is submitted to `FileChunkingManager`, whose pool has `vector_thread_count + 2`
   workers. `VectorCalculationManager` runs the embedding calls on `vector_thread_count` threads.

Inside `FileChunkingManager._process_file_clean_lifecycle()`, per file:

1. Chunk the file.
2. **Reuse unchanged chunks.** Load the stored `content_hash` (SHA-256 of the chunk text) and vector for each
   chunk index of this path (`get_existing_content_hashes`). A chunk whose hash is unchanged reuses the stored
   vector and is not sent to the embedder.
3. **Batch the rest by tokens.** Accumulate chunks until the next one would exceed 90 percent of the provider's
   per-request token limit (`_get_model_token_limit()`; 120000 for `voyage-code-3` in `data/voyage_models.yaml`,
   128000 for Cohere `embed-v4.0`), then submit the batch. Token counts come from `VoyageTokenizer`
   (`services/embedded_voyage_tokenizer.py`) for VoyageAI and from `len(text) // 4` for other providers. The
   provider clients split again internally with the same 90 percent rule.
4. Wait for every batch of the file. A failed batch, a count mismatch, or an empty embedding fails the whole file;
   no chunk is skipped silently.
5. Build the points and write them with ONE `upsert_points()` call for the file.

Rate-limit retries happen inside the provider clients (`services/provider_backoff.py`); the indexing path has no
wall-clock timeout on the job or on a file.

## Writing points and finalizing the session

Every strategy brackets its writes in a store session:

- `begin_indexing(collection)` loads the collection's `PathIndex` (path to point ids) and starts change tracking.
- `upsert_points()` writes the points of one file. Old points of the same path that are not in the new set are
  removed through the `PathIndex`, so a file that shrank loses its trailing chunks. On a `chunks.db` collection the
  write is one `ChunkStore.write_batch()` call.
- `end_indexing(collection)` updates or rebuilds the HNSW index and persists the indexes once per session.

When a chunk-store failure is classified as fatal (`ChunkStoreUnavailableError`), the processor cancels files not
yet started and the session is aborted with `abort_indexing()` instead of being finalized, so the commit
watermark does not advance (`SmartIndexer._finalize_or_abort_indexing_session`). After a successful full or
incremental run, `ProgressiveMetadata` records the branch's commit watermark and marks the run completed.

## Branch isolation

Points carry branch visibility in their payload. After a full index in a git repository,
`hide_files_not_in_branch_thread_safe()` hides points of files that do not exist on the current branch instead of
deleting them, so switching back can reuse them. On a branch switch, `process_branch_changes_high_throughput()`
reindexes only the files `GitTopologyService.analyze_branch_change()` reports as changed and updates visibility
metadata for the rest.

## Full-text index

`--fts` builds a Tantivy index at `.code-indexer/tantivy_index/` alongside the semantic index:

- A new index is created when none exists, on `--clear`, or when the existing index has an outdated schema
  (`schema_needs_rebuild()`; the directory is cleared first). A newly created index that is not part of a full run
  is filled immediately from every file on disk (`_populate_fts_from_all_files`).
- Per processed file, `FileChunkingManager` calls `delete_document_deferred(path)` and then `add_document()` for
  each chunk, so re-indexing a file never duplicates its rows.
- The index is committed once, in the `finally` block of `smart_index()`.
- If the Tantivy import or initialization fails, the run logs an error and continues without FTS; a commit
  failure is logged and does not fail the semantic index.

`--rebuild-fts-index` rebuilds only the FTS index from already-indexed files.

## Reconcile

`--reconcile` (`SmartIndexer._do_reconcile_with_database`) compares the disk with the store instead of trusting
timestamps:

1. Walk all eligible files with `FileFinder.find_files()`.
2. Read one snapshot of the indexed content points (`_get_indexed_files_snapshot`).
3. For each file, compare the stored content id with the disk content id (see
   [File identity and point ids](#file-identity-and-point-ids)); `HEAD` blob hashes are read in one batch
   (`_get_head_blob_hash_map`). Missing or differing files are reindexed.
4. Stored files that no longer exist, or that a filter now excludes, are hidden (git) or deleted (non-git).

A reconcile that completes without cancellation and without a file limit records that the whole store was
verified (`mark_store_verified`).

## Failures, cancellation and exit codes

- A file that fails (chunking, embedding, write) counts as failed and is retried by the next run; other files
  continue.
- `cidx index` exits 1 when files failed and none were processed ("All files failed to index").
- A fatal chunk-store failure exits with a reserved code: 86 when SQLite reports the store damaged, 87 for every
  other fatal store failure (`services/index_failure_exit_codes.py`). The server's handling of these codes is in
  [refresh-recovery.md](refresh-recovery.md).
- Ctrl+C leaves the run resumable: completed files stay indexed and progressive metadata keeps the remaining
  list.

## Temporal indexing entry point

`cidx index --index-commits` runs the temporal (git history) pipeline instead of the semantic pass and exits:
`TemporalIndexer.index_commits()` in `services/temporal/temporal_indexer.py` aggregates each commit (message plus
changed-file diffs) into one document, chunks it, and embeds it with every configured temporal embedder, sharded
by calendar quarter. Options: `--all-branches`, `--max-commits`, `--since-date`, `--diff-context`, and
`--reconcile` with `--reconcile-embedder`. The data model and query side are described in
[guides/temporal-search.md](../guides/temporal-search.md).
