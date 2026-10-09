# Shared Storage Protocol (NFS)

Rules for the storage that cluster nodes share. Operator procedure and the exact mount layout:
[CoW Storage Setup](../../server/cow-storage-setup.md#storage-layout). Index of all invariant groups:
[README](README.md).

## What is deployed

- The installer (`scripts/install-cidx-server.sh`) gives each cow-daemon node ONE NFS mount,
  `cow_daemon.mount_point` (for example `/mnt/cow-storage`), with options `_netdev,vers=3,nolock,soft,timeo=30,retrans=3`.
  `~/.cidx-server/data/golden-repos` and `~/.cidx-server/data/activated-repos` are symlinks into it; there is no
  separate golden-repos mount.
- The auto-updater (`DeploymentExecutor._ensure_cow_storage_mount_options` in
  `src/code_indexer/server/auto_update/deployment_executor.py`) rewrites an existing `/etc/fstab` entry for that mount
  point to `nfs` with `vers=3` and `nolock`. It keeps whatever `soft`/`hard` option the entry already has.
- The ONTAP join script (`scripts/cluster-join.sh`) mounts its export `hard`.
- Several source comments still describe a separate golden-repos mount as `vers=3,nolock,hard`
  (`src/code_indexer/server/services/alias_lock_store/factory.py`, `server/cache/hnsw_index_cache.py`,
  `backends/filesystem_backend.py`, `deployment_executor.py`). That layout is not what the installer creates.

## NFSv3 only

NFSv4 is not supported. It was deployed and rolled back after three distinct failures: server-side lock-state loss
under NFSv4.1 that surfaced as SQLite `disk I/O error` during refresh (`chunks.db` writes need real byte-range
locks), git pack writes failing with `Bad file descriptor`, and an NFSv4.2 state-recovery hang on one node. `nolock`
has no effect on NFSv4, which handles locking inside its own protocol state; under NFSv3 it disables the separate
NLM protocol. Do not propose NFSv4 without addressing all three failures. `vers=3`/`nolock` and `soft`/`hard` are
independent choices.

The general rule: on this deployment, coherence or locking bought through server-held protocol state (NFSv4 locks,
delegations, open state; SMB3 leases) has failed in state recovery. NFSv3's statelessness is the property relied on.

## Need less from the filesystem

- Coordination lives in PostgreSQL, not in files: the golden-repo alias lock is DB-backed by default
  (`alias_lock_config.db_backed_enabled = True`, `src/code_indexer/server/utils/config_manager.py`; store chosen by
  `server/services/alias_lock_store/factory.py`), and job coordination uses the `background_jobs` table. A SQLite lock
  file on a `nolock` mount gives each node a private lock, so node-local lock stores are placed in the node's own data
  directory, never under `golden_repos_dir`.
- Read freshness on shared files is decided by file identity (inode, device, size, mtime) rather than by trusting the
  protocol; see [Chunk Storage Invariants](chunk-storage.md#temporal-data-location).
- Any storage proposal must keep local `cp --reflink`: the CoW daemon reflinks on its own XFS or btrfs filesystem and
  the clone never crosses the network. Filesystems that replace the local filesystem (CephFS, GlusterFS) remove the
  reflink capability.

## Code must tolerate a blocking or failing mount

- A `hard` mount whose server is unreachable makes `os.stat()` and every other metadata call block in uninterruptible
  kernel retry, indefinitely. A `soft` mount returns an I/O error after its retries instead.
- Never run filesystem calls against the shared tree on the asyncio event loop; offload them
  (`anyio.to_thread.run_sync`). Never hold a process-wide lock across such a call (example:
  `HNSWIndexCache._verify_entry_freshness` stats with no lock held and rate-limits the check per key).
- Treat "the path does not exist" as indistinguishable from "the mount is broken" unless proven otherwise: destructive
  sweeps check that the root is readable first and refuse mass deletions (see
  [Golden Repository Invariants](golden-repos.md#registry-orphan-guard)).
